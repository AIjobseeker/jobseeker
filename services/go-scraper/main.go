// go-scraper: high-performance, fault-tolerant multi-company job scraper
//
// Architecture principles (Release It! + Concurrency in Go):
//   - Bounded worker pool via semaphore (no goroutine leaks)
//   - Per-company context timeout (slow company can't block others)
//   - Circuit breaker per company (3 failures → open for 30s)
//   - Token bucket rate limiting per ATS domain
//   - Exponential backoff retry for transient errors
//   - Graceful shutdown: drains in-flight workers before exit
//
// Usage:
//
//	go run . [flags]
//
// Flags:
//
//	--mode   nats|file|stdout  (default: stdout)
//	--output path              (file path when mode=file)
//	--seed   path              (consolidated seed YAML)
//	--dir    path              (fallback: directory of per-company YAMLs)
//	--once                     scrape once and exit
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"net"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"go.uber.org/zap"
	"golang.org/x/sync/semaphore"

	"github.com/aijobseeker/go-scraper/breaker"
	"github.com/aijobseeker/go-scraper/connector"
	"github.com/aijobseeker/go-scraper/internal/config"
	"github.com/aijobseeker/go-scraper/internal/models"
	"github.com/aijobseeker/go-scraper/loader"
	"github.com/aijobseeker/go-scraper/publisher"
	"github.com/aijobseeker/go-scraper/ratelimit"
)

const (
	maxRetries    = 2
	retryBaseWait = 2 * time.Second
)

func main() {
	modeFl := flag.String("mode", "stdout", "publisher mode: nats | file | stdout")
	outputFl := flag.String("output", "", "output file path (mode=file only)")
	seedFl := flag.String("seed", "../../companies/seed_500.yaml", "path to consolidated seed YAML")
	dirFl := flag.String("dir", "../../companies", "fallback directory of per-company YAMLs")
	onceFl := flag.Bool("once", false, "scrape once and exit")
	flag.Parse()

	log := mustLogger()
	defer log.Sync() //nolint:errcheck

	cfg := config.Load()

	// ─── Load companies ──────────────────────────────────────────────────────
	companies, err := loadCompanies(*seedFl, *dirFl, log)
	if err != nil {
		log.Fatal("load companies", zap.Error(err))
	}
	log.Info("companies loaded", zap.Int("count", len(companies)))

	// ─── Publisher ───────────────────────────────────────────────────────────
	pub, err := buildPublisher(*modeFl, *outputFl, cfg, log)
	if err != nil {
		log.Fatal("init publisher", zap.Error(err))
	}
	defer pub.Close()

	// ─── Signal handling — graceful shutdown ─────────────────────────────────
	ctx, cancel := context.WithCancel(context.Background())

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		sig := <-sigCh
		log.Info("shutdown signal — draining workers", zap.String("signal", sig.String()))
		cancel()
	}()

	// ─── Run ─────────────────────────────────────────────────────────────────
	if *onceFl {
		runScrape(ctx, companies, pub, cfg, log)
		cancel()
		return
	}

	fastTicker := time.NewTicker(time.Duration(cfg.FastIntervalSeconds) * time.Second)
	slowTicker := time.NewTicker(time.Duration(cfg.SlowIntervalSeconds) * time.Second)
	defer fastTicker.Stop()
	defer slowTicker.Stop()

	// Run immediately on startup, then on tickers
	runScrape(ctx, companies, pub, cfg, log)

	for {
		select {
		case <-ctx.Done():
			log.Info("scraper stopped cleanly")
			return
		case <-fastTicker.C:
			tier1 := filterByTier(companies, 1)
			log.Info("fast tick — tier-1 companies", zap.Int("count", len(tier1)))
			runScrape(ctx, tier1, pub, cfg, log)
		case <-slowTicker.C:
			log.Info("slow tick — all companies", zap.Int("count", len(companies)))
			runScrape(ctx, companies, pub, cfg, log)
		}
	}
}

// runScrape executes one full scrape cycle over the given companies.
// Worker pool is bounded by cfg.MaxConcurrency via semaphore.
// All workers complete (or ctx is cancelled) before returning.
func runScrape(
	ctx context.Context,
	companies []models.Company,
	pub publisher.Publisher,
	cfg *config.Config,
	log *zap.Logger,
) {
	start := time.Now()
	sem := semaphore.NewWeighted(int64(cfg.MaxConcurrency))

	var (
		wg          sync.WaitGroup
		totalJobs   atomic.Int64
		totalErrors atomic.Int64
		skipped     atomic.Int64
	)

	for i := range companies {
		company := companies[i]

		if err := sem.Acquire(ctx, 1); err != nil {
			// Root context cancelled — stop dispatching, let in-flight finish
			break
		}

		wg.Add(1)
		go func() {
			defer sem.Release(1)
			defer wg.Done()
			scrapeOne(ctx, company, pub, cfg, log, &totalJobs, &totalErrors, &skipped)
		}()
	}

	wg.Wait()

	log.Info("scrape cycle complete",
		zap.Duration("elapsed", time.Since(start)),
		zap.Int("companies", len(companies)),
		zap.Int64("jobs_found", totalJobs.Load()),
		zap.Int64("errors", totalErrors.Load()),
		zap.Int64("skipped_breaker", skipped.Load()),
	)
}

// scrapeOne scrapes one company with:
//   - circuit breaker check (skip if open)
//   - per-company deadline (HTTPTimeout, default 30s)
//   - context-aware rate limiting
//   - retry with exponential backoff for transient errors
func scrapeOne(
	ctx context.Context,
	company models.Company,
	pub publisher.Publisher,
	cfg *config.Config,
	log *zap.Logger,
	totalJobs *atomic.Int64,
	totalErrors *atomic.Int64,
	skipped *atomic.Int64,
) {
	cb := breaker.Get(company.Name)
	if !cb.Allow() {
		skipped.Add(1)
		log.Debug("circuit open — skipping",
			zap.String("company", company.Name),
			zap.String("state", cb.State()),
		)
		return
	}

	// Each company gets its own deadline — slow one can't starve others.
	companyCtx, cancel := context.WithTimeout(ctx, cfg.HTTPTimeout)
	defer cancel()

	// Context-aware rate limit wait.
	domain := atsDomain(company)
	if err := ratelimit.Global.Wait(companyCtx, domain); err != nil {
		// Parent context cancelled during rate-limit wait — not a company error.
		return
	}

	conn, connErr := connector.Factory(company)
	if connErr != nil {
		log.Warn("no connector",
			zap.String("company", company.Name),
			zap.String("ats", string(company.ATS)),
			zap.Error(connErr),
		)
		return
	}

	// ── Retry loop ───────────────────────────────────────────────────────────
	var jobs []models.Job
	var lastErr error

	for attempt := 0; attempt <= maxRetries; attempt++ {
		if attempt > 0 {
			wait := retryBaseWait * time.Duration(1<<(attempt-1)) // 2s, 4s
			log.Debug("retrying",
				zap.String("company", company.Name),
				zap.Int("attempt", attempt),
				zap.Duration("wait", wait),
			)
			select {
			case <-time.After(wait):
			case <-companyCtx.Done():
				lastErr = companyCtx.Err()
				goto done
			}
		}

		jobs, lastErr = conn.FetchJobs(companyCtx)
		if lastErr == nil {
			break
		}
		if !isTransient(lastErr) {
			break // permanent error (404, config missing, etc.) — don't retry
		}
	}

done:
	if lastErr != nil {
		cb.Failure()
		totalErrors.Add(1)
		// Permanent / known-bad outcomes go to debug — they aren't actionable
		// and we don't want them drowning the WARN stream every cycle.
		// Real WARNs are reserved for surprising failures (5xx, network errors).
		switch {
		case errors.Is(lastErr, context.Canceled),
			errors.Is(lastErr, context.DeadlineExceeded):
			log.Debug("scrape cancelled", zap.String("company", company.Name))
		case isPermanentClientError(lastErr):
			log.Debug("scrape skipped",
				zap.String("company", company.Name),
				zap.String("ats", string(company.ATS)),
				zap.Error(lastErr),
			)
		default:
			log.Warn("scrape failed",
				zap.String("company", company.Name),
				zap.String("ats", string(company.ATS)),
				zap.Error(lastErr),
			)
		}
		return
	}

	cb.Success()

	if len(jobs) == 0 {
		log.Debug("no jobs found", zap.String("company", company.Name))
		return
	}

	if err := pub.Publish(ctx, jobs); err != nil {
		log.Error("publish failed",
			zap.String("company", company.Name),
			zap.Error(err),
		)
		return
	}

	totalJobs.Add(int64(len(jobs)))
	log.Info("scraped",
		zap.String("company", company.Name),
		zap.String("ats", string(company.ATS)),
		zap.Int("jobs", len(jobs)),
	)
}

// isPermanentClientError returns true for errors that mean "this company's
// board is gone or unreachable in a permanent way" — 401/403/404, missing
// config, etc. These shouldn't surface as WARN every cycle.
func isPermanentClientError(err error) bool {
	if err == nil {
		return false
	}
	msg := err.Error()
	for _, marker := range []string{
		"404", "403", "401",
		"not found", "Forbidden", "Unauthorized",
		"board_id required", "tenant+board required",
		"unknown custom module",
	} {
		if strings.Contains(msg, marker) {
			return true
		}
	}
	return false
}

// isTransient returns true for errors that are worth retrying:
// network timeouts, temporary DNS failures, HTTP 429/503/5xx.
func isTransient(err error) bool {
	if err == nil {
		return false
	}
	// Context errors are not transient — they mean deadline or cancel.
	if errors.Is(err, context.DeadlineExceeded) || errors.Is(err, context.Canceled) {
		return false
	}
	msg := err.Error()
	// HTTP status errors we should retry
	if strings.Contains(msg, "429") || strings.Contains(msg, "503") ||
		strings.Contains(msg, "502") || strings.Contains(msg, "500") {
		return true
	}
	// Permanent errors — don't retry
	if strings.Contains(msg, "404") || strings.Contains(msg, "403") ||
		strings.Contains(msg, "401") || strings.Contains(msg, "board_id required") ||
		strings.Contains(msg, "tenant+board required") || strings.Contains(msg, "unknown custom module") {
		return false
	}
	// Network-level transient errors
	var netErr net.Error
	if errors.As(err, &netErr) && netErr.Timeout() {
		return true
	}
	if strings.Contains(msg, "connection reset") || strings.Contains(msg, "EOF") ||
		strings.Contains(msg, "connection refused") {
		return true
	}
	return false
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

func loadCompanies(seedPath, dirPath string, log *zap.Logger) ([]models.Company, error) {
	if _, err := os.Stat(seedPath); err == nil {
		log.Info("loading from seed file", zap.String("path", seedPath))
		return loader.LoadSeedFile(seedPath)
	}
	absDir, _ := filepath.Abs(dirPath)
	if _, err := os.Stat(absDir); err != nil {
		return nil, fmt.Errorf("neither seed file %q nor directory %q found", seedPath, absDir)
	}
	log.Info("loading from directory", zap.String("path", absDir))
	return loader.LoadDirectory(absDir)
}

func buildPublisher(mode, output string, cfg *config.Config, log *zap.Logger) (publisher.Publisher, error) {
	switch mode {
	case "nats":
		log.Info("publisher: NATS JetStream", zap.String("url", cfg.NATSUrl))
		return publisher.NewNATSPublisher(cfg.NATSUrl)
	case "file":
		log.Info("publisher: file", zap.String("path", output))
		return publisher.NewFilePublisher(output)
	default:
		log.Info("publisher: stdout")
		return publisher.NewFilePublisher("stdout")
	}
}

func filterByTier(companies []models.Company, tier int) []models.Company {
	var out []models.Company
	for _, c := range companies {
		if c.Tier <= tier {
			out = append(out, c)
		}
	}
	return out
}

// atsDomain returns the rate-limit domain key for a company.
func atsDomain(c models.Company) string {
	switch c.ATS {
	case models.ATSGreenhouse:
		return "api.greenhouse.io"
	case models.ATSLever:
		return "api.lever.co"
	case models.ATSAshby:
		return "api.ashbyhq.com"
	case models.ATSSmartRecruiters:
		return "api.smartrecruiters.com"
	case models.ATSWorkday:
		if c.Domain != "" {
			return c.Domain
		}
		return "myworkdayjobs.com"
	case models.ATSCustom:
		if c.Domain != "" {
			return c.Domain
		}
		return c.Name
	default:
		if c.Domain != "" {
			return c.Domain
		}
		return c.Name
	}
}

func mustLogger() *zap.Logger {
	var log *zap.Logger
	var err error
	if os.Getenv("ENV") == "production" {
		log, err = zap.NewProduction()
	} else {
		cfg := zap.NewDevelopmentConfig()
		cfg.DisableCaller = true
		log, err = cfg.Build()
	}
	if err != nil {
		fmt.Fprintf(os.Stderr, "init logger: %v\n", err)
		os.Exit(1)
	}
	return log
}
