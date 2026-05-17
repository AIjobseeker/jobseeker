// Package ratelimit provides per-domain token bucket rate limiting.
//
// Design: Each domain gets its own rate.Limiter. Limiters are stored in a
// sync.Map (concurrent-safe). First access creates the limiter lazily.
//
// Why per-domain? Different ATS platforms have different tolerance:
//   - Greenhouse:       60 req/min = 1 req/sec
//   - Lever:            30 req/min = 0.5 req/sec
//   - Workday:          10 req/min (strict) = 0.17 req/sec
//   - Custom (Apple etc): 20 req/min = 0.33 req/sec
package ratelimit

import (
	"context"
	"sync"

	"golang.org/x/time/rate"
)

// Limits per domain (requests per second).
var domainLimits = map[string]rate.Limit{
	"api.greenhouse.io":       rate.Limit(1.0),  // 1 req/sec
	"api.lever.co":            rate.Limit(0.5),  // 1 per 2 sec
	"api.ashbyhq.com":         rate.Limit(0.5),
	"api.smartrecruiters.com": rate.Limit(0.5),
	"myworkdayjobs.com":       rate.Limit(0.15), // 1 per ~7 sec
	"jobs.apple.com":          rate.Limit(0.3),
	"careers.google.com":      rate.Limit(0.3),
	"www.amazon.jobs":         rate.Limit(0.3),
	"www.metacareers.com":     rate.Limit(0.2),
}

const defaultLimit = rate.Limit(0.5) // fallback: 1 per 2 sec
const defaultBurst = 3

// Registry holds per-domain rate limiters.
type Registry struct {
	mu       sync.Mutex
	limiters sync.Map // domain string → *rate.Limiter
}

// Global registry — one instance shared by all connectors.
var Global = &Registry{}

// Wait blocks until a token is available for the given domain.
// Returns context.Err() immediately if ctx is already done.
// Safe for concurrent use by multiple goroutines.
func (r *Registry) Wait(ctx context.Context, domain string) error {
	limiter := r.getOrCreate(domain)
	reservation := limiter.Reserve()
	d := reservation.Delay()
	if d <= 0 {
		return nil
	}
	select {
	case <-makeTimer(d):
		return nil
	case <-ctx.Done():
		// Return the reservation so the token is not wasted.
		reservation.Cancel()
		return ctx.Err()
	}
}

func (r *Registry) getOrCreate(domain string) *rate.Limiter {
	if v, ok := r.limiters.Load(domain); ok {
		return v.(*rate.Limiter)
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	// Double-check after acquiring lock
	if v, ok := r.limiters.Load(domain); ok {
		return v.(*rate.Limiter)
	}
	limit := defaultLimit
	for pattern, l := range domainLimits {
		if len(domain) >= len(pattern) && domain[len(domain)-len(pattern):] == pattern {
			limit = l
			break
		}
	}
	limiter := rate.NewLimiter(limit, defaultBurst)
	r.limiters.Store(domain, limiter)
	return limiter
}
