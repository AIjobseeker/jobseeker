// Package publisher defines the Publisher interface and concrete implementations
// for sending scraped jobs downstream (NATS JetStream or local file/stdout).
package publisher

import (
	"context"

	"github.com/aijobseeker/go-scraper/internal/models"
)

// Publisher sends scraped jobs to a downstream system.
type Publisher interface {
	Publish(ctx context.Context, jobs []models.Job) error
	Close()
}
