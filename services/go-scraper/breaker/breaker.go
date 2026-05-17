// Package breaker wraps connectors with a per-company circuit breaker.
//
// Pattern: Release It! (Nygard) — circuit breaker prevents cascade failures
// when a company's ATS is down, flaky, or rate-limiting us.
//
// States:
//   - Closed:    requests pass through normally
//   - Open:      requests fail fast (after 3 consecutive failures)
//   - Half-open: one probe request allowed (after 30s cooldown)
//
// This is critical for 500-company scraping: one dead company should not
// block or slow down the entire scrape cycle.
package breaker

import (
	"fmt"
	"sync"
	"time"
)

type State int

const (
	Closed   State = iota // normal operation
	Open                  // failing fast
	HalfOpen              // testing recovery
)

type Breaker struct {
	mu           sync.Mutex
	name         string
	state        State
	failures     int
	successes    int
	maxFailures  int
	cooldown     time.Duration
	lastFailure  time.Time
}

// Registry of circuit breakers keyed by company name.
var (
	registry   sync.Map
	registryMu sync.Mutex
)

// Get returns the circuit breaker for a company, creating it if needed.
func Get(companyName string) *Breaker {
	if v, ok := registry.Load(companyName); ok {
		return v.(*Breaker)
	}
	registryMu.Lock()
	defer registryMu.Unlock()
	if v, ok := registry.Load(companyName); ok {
		return v.(*Breaker)
	}
	b := &Breaker{
		name:        companyName,
		state:       Closed,
		maxFailures: 3,
		cooldown:    30 * time.Second,
	}
	registry.Store(companyName, b)
	return b
}

// Allow returns true if the request should proceed.
// Caller must call Success or Failure after the request completes.
func (b *Breaker) Allow() bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	switch b.state {
	case Closed:
		return true
	case Open:
		if time.Since(b.lastFailure) > b.cooldown {
			b.state = HalfOpen
			return true
		}
		return false
	case HalfOpen:
		return true
	}
	return false
}

func (b *Breaker) Success() {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.failures = 0
	b.state = Closed
}

func (b *Breaker) Failure() {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.failures++
	b.lastFailure = time.Now()
	if b.failures >= b.maxFailures || b.state == HalfOpen {
		b.state = Open
	}
}

func (b *Breaker) State() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	switch b.state {
	case Closed:
		return "closed"
	case Open:
		return fmt.Sprintf("open (cooldown: %.0fs remaining)",
			b.cooldown.Seconds()-time.Since(b.lastFailure).Seconds())
	case HalfOpen:
		return "half-open"
	}
	return "unknown"
}
