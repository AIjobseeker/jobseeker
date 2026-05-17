package config

import (
	"os"
	"strconv"
	"time"
)

// Config holds all runtime configuration sourced from environment variables.
// All fields have sensible defaults so the scraper works out-of-the-box
// with docker-compose.
type Config struct {
	// NATS
	NATSUrl     string
	NATSSubject string

	// Temporal (optional — scraper can run standalone publishing to NATS only)
	TemporalHost      string
	TemporalNamespace string
	TemporalTaskQueue string

	// Redis (for deduplication)
	RedisURL string

	// Company seed file
	SeedFile string

	// Scraping behaviour
	// Fast tier: ATS REST APIs (Greenhouse, Lever, Ashby, SmartRecruiters)
	FastIntervalSeconds int
	// Slow tier: Workday, custom scrapers
	SlowIntervalSeconds int
	// Max goroutines per scrape cycle
	MaxConcurrency int
	// HTTP request timeout
	HTTPTimeout time.Duration
	// User-Agent header
	UserAgent string

	// Output: "nats" | "stdout" | "file"
	OutputMode string
	OutputFile string

	// Logging
	LogLevel string // "debug" | "info" | "warn" | "error"

	// Local LLM (optional — set to use Ollama instead of Anthropic for matching)
	LocalLLMEnabled  bool
	LocalLLMBaseURL  string // e.g. http://192.168.1.100:11434
	LocalLLMModel    string // e.g. qwen2.5:7b
	AnthropicAPIKey  string
}

func Load() *Config {
	c := &Config{
		NATSUrl:             getEnv("NATS_URL", "nats://localhost:4222"),
		NATSSubject:         getEnv("NATS_JOBS_SUBJECT", "jobs.raw"),
		TemporalHost:        getEnv("TEMPORAL_HOST", "localhost:7233"),
		TemporalNamespace:   getEnv("TEMPORAL_NAMESPACE", "default"),
		TemporalTaskQueue:   getEnv("TEMPORAL_TASK_QUEUE", "jobseeker-queue"),
		RedisURL:            getEnv("REDIS_URL", "redis://localhost:6379/0"),
		SeedFile:            getEnv("SEED_FILE", "/app/companies/seed_500.yaml"),
		FastIntervalSeconds: getEnvInt("SCRAPE_INTERVAL_FAST", 120),  // 2 min
		SlowIntervalSeconds: getEnvInt("SCRAPE_INTERVAL_SLOW", 600),  // 10 min
		MaxConcurrency:      getEnvInt("SCRAPER_CONCURRENCY", 50),
		HTTPTimeout:         time.Duration(getEnvInt("HTTP_TIMEOUT_SEC", 20)) * time.Second,
		UserAgent: getEnv("USER_AGENT",
			"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "+
				"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
		OutputMode:      getEnv("OUTPUT_MODE", "nats"),
		OutputFile:      getEnv("OUTPUT_FILE", "/tmp/jobs.jsonl"),
		LogLevel:        getEnv("LOG_LEVEL", "info"),
		LocalLLMEnabled: getEnvBool("LOCAL_LLM_ENABLED", false),
		LocalLLMBaseURL: getEnv("LOCAL_LLM_BASE_URL", "http://localhost:11434"),
		LocalLLMModel:   getEnv("LOCAL_LLM_MODEL", "qwen2.5:7b"),
		AnthropicAPIKey: getEnv("ANTHROPIC_API_KEY", ""),
	}
	return c
}

func getEnv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func getEnvInt(key string, fallback int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return fallback
}

func getEnvBool(key string, fallback bool) bool {
	v := os.Getenv(key)
	if v == "" {
		return fallback
	}
	b, err := strconv.ParseBool(v)
	if err != nil {
		return fallback
	}
	return b
}
