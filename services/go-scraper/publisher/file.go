package publisher

import (
	"context"
	"encoding/json"
	"fmt"
	"os"

	"github.com/aijobseeker/go-scraper/internal/models"
)

// FilePublisher writes NDJSON (one job per line) to stdout or a file.
// Used for local testing without a running NATS server.
type FilePublisher struct {
	f       *os.File
	encoder *json.Encoder
}

func NewFilePublisher(path string) (*FilePublisher, error) {
	var f *os.File
	if path == "" || path == "stdout" {
		f = os.Stdout
	} else {
		var err error
		f, err = os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0644)
		if err != nil {
			return nil, fmt.Errorf("open output file: %w", err)
		}
	}
	enc := json.NewEncoder(f)
	enc.SetEscapeHTML(false)
	return &FilePublisher{f: f, encoder: enc}, nil
}

func (p *FilePublisher) Publish(_ context.Context, jobs []models.Job) error {
	for i := range jobs {
		if err := p.encoder.Encode(&jobs[i]); err != nil {
			return fmt.Errorf("encode job: %w", err)
		}
	}
	return nil
}

func (p *FilePublisher) Close() {
	if p.f != os.Stdout {
		p.f.Close()
	}
}
