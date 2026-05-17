package publisher

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/aijobseeker/go-scraper/internal/models"
	"github.com/nats-io/nats.go"
)

const (
	streamName = "JOBS"
	subjectRaw = "jobs.raw"
)

type NATSPublisher struct {
	nc *nats.Conn
	js nats.JetStreamContext
}

// NewNATSPublisher connects to NATS and ensures the JOBS stream exists.
func NewNATSPublisher(url string) (*NATSPublisher, error) {
	nc, err := nats.Connect(url,
		nats.ReconnectWait(2*time.Second),
		nats.MaxReconnects(-1),
		nats.DisconnectErrHandler(func(_ *nats.Conn, err error) {
			// log in main; publisher is unaware of logger
		}),
	)
	if err != nil {
		return nil, fmt.Errorf("nats connect %s: %w", url, err)
	}

	js, err := nc.JetStream()
	if err != nil {
		nc.Close()
		return nil, fmt.Errorf("nats jetstream: %w", err)
	}

	if _, err := js.StreamInfo(streamName); err != nil {
		_, err = js.AddStream(&nats.StreamConfig{
			Name:       streamName,
			Subjects:   []string{"jobs.>"},
			Storage:    nats.FileStorage,
			Retention:  nats.LimitsPolicy,
			MaxAge:     7 * 24 * time.Hour,
			MaxMsgSize: 512 * 1024, // 512 KB per message
			Replicas:   1,
		})
		if err != nil {
			nc.Close()
			return nil, fmt.Errorf("nats add stream: %w", err)
		}
	}

	return &NATSPublisher{nc: nc, js: js}, nil
}

// Publish marshals each job as JSON and publishes to jobs.raw subject.
func (p *NATSPublisher) Publish(_ context.Context, jobs []models.Job) error {
	for i := range jobs {
		data, err := json.Marshal(&jobs[i])
		if err != nil {
			return fmt.Errorf("marshal job %s: %w", jobs[i].ID, err)
		}
		if _, err := p.js.Publish(subjectRaw, data); err != nil {
			return fmt.Errorf("nats publish job %s: %w", jobs[i].ID, err)
		}
	}
	return nil
}

func (p *NATSPublisher) Close() {
	_ = p.nc.Drain()
}
