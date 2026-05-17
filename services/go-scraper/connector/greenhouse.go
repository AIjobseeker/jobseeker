package connector

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/aijobseeker/go-scraper/internal/models"
	"github.com/google/uuid"
)

// GreenhouseConnector hits the public Greenhouse jobs board API.
// No authentication required — Greenhouse exposes all active jobs publicly.
//
// API docs: https://developers.greenhouse.io/job-board.html
// Rate limit: very generous (~1 req/sec per board is safe)
type GreenhouseConnector struct {
	company    models.Company
	httpClient *http.Client
}

func NewGreenhouseConnector(company models.Company) *GreenhouseConnector {
	return &GreenhouseConnector{
		company: company,
		httpClient: &http.Client{
			Timeout: 20 * time.Second,
		},
	}
}

func (g *GreenhouseConnector) ATSType() models.ATSType  { return models.ATSGreenhouse }
func (g *GreenhouseConnector) CompanyName() string      { return g.company.Name }

func (g *GreenhouseConnector) FetchJobs(ctx context.Context) ([]models.Job, error) {
	url := fmt.Sprintf("https://api.greenhouse.io/v1/boards/%s/jobs?content=true", g.company.BoardID)

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, fmt.Errorf("greenhouse build request: %w", err)
	}
	req.Header.Set("User-Agent", chromeUA)

	resp, err := g.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("greenhouse fetch %s: %w", g.company.Name, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusNotFound {
		return nil, fmt.Errorf("greenhouse board %q not found (404) — verify board_id", g.company.BoardID)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("greenhouse %s: HTTP %d", g.company.Name, resp.StatusCode)
	}

	var payload struct {
		Jobs []struct {
			ID          int    `json:"id"`
			Title       string `json:"title"`
			Content     string `json:"content"`
			AbsoluteURL string `json:"absolute_url"`
			UpdatedAt   string `json:"updated_at"`
			Location    struct {
				Name string `json:"name"`
			} `json:"location"`
			Departments []struct {
				Name string `json:"name"`
			} `json:"departments"`
		} `json:"jobs"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		return nil, fmt.Errorf("greenhouse decode %s: %w", g.company.Name, err)
	}

	now := time.Now().UTC()
	var jobs []models.Job
	for _, raw := range payload.Jobs {
		descText := stripHTML(raw.Content)
		if !passesKeywordFilter(g.company, raw.Title, descText) {
			continue
		}
		dept := ""
		if len(raw.Departments) > 0 {
			dept = raw.Departments[0].Name
		}
		var postedAt *time.Time
		if t, err := time.Parse(time.RFC3339, raw.UpdatedAt); err == nil {
			postedAt = &t
		}
		jobs = append(jobs, models.Job{
			ID:                  uuid.New().String(),
			SourceID:            fmt.Sprintf("%d", raw.ID),
			Source:              models.ATSGreenhouse,
			Company:             g.company.Name,
			Title:               raw.Title,
			DescriptionHTML:     raw.Content,
			DescriptionText:     descText,
			URL:                 raw.AbsoluteURL,
			Location:            raw.Location.Name,
			Department:          dept,
			PostedAt:            postedAt,
			ScrapedAt:           now,
			NoSponsorshipPhrase: hasSponsorshipIssue(descText),
		})
	}
	return jobs, nil
}

// ─────────────────── LeverConnector ───────────────────

type LeverConnector struct {
	company    models.Company
	httpClient *http.Client
}

func NewLeverConnector(company models.Company) *LeverConnector {
	return &LeverConnector{
		company:    company,
		httpClient: &http.Client{Timeout: 20 * time.Second},
	}
}

func (l *LeverConnector) ATSType() models.ATSType { return models.ATSLever }
func (l *LeverConnector) CompanyName() string     { return l.company.Name }

func (l *LeverConnector) FetchJobs(ctx context.Context) ([]models.Job, error) {
	url := fmt.Sprintf("https://api.lever.co/v0/postings/%s?mode=json", l.company.BoardID)

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", chromeUA)

	resp, err := l.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("lever fetch %s: %w", l.company.Name, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusNotFound {
		return nil, fmt.Errorf("lever board %q not found — verify board_id", l.company.BoardID)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("lever %s: HTTP %d", l.company.Name, resp.StatusCode)
	}

	var postings []struct {
		ID          string `json:"id"`
		Text        string `json:"text"`
		Description string `json:"description"`
		Additional  string `json:"additional"`
		DescPlain   string `json:"descriptionPlain"`
		HostedURL   string `json:"hostedUrl"`
		CreatedAt   int64  `json:"createdAt"` // milliseconds since epoch
		Categories  struct {
			Location string `json:"location"`
			Team     string `json:"team"`
		} `json:"categories"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&postings); err != nil {
		return nil, fmt.Errorf("lever decode %s: %w", l.company.Name, err)
	}

	now := time.Now().UTC()
	var jobs []models.Job
	for _, raw := range postings {
		descText := raw.DescPlain
		if descText == "" {
			descText = stripHTML(raw.Description + raw.Additional)
		}
		if !passesKeywordFilter(l.company, raw.Text, descText) {
			continue
		}
		var postedAt *time.Time
		if raw.CreatedAt > 0 {
			t := time.UnixMilli(raw.CreatedAt).UTC()
			postedAt = &t
		}
		jobs = append(jobs, models.Job{
			ID:                  uuid.New().String(),
			SourceID:            raw.ID,
			Source:              models.ATSLever,
			Company:             l.company.Name,
			Title:               raw.Text,
			DescriptionHTML:     raw.Description + raw.Additional,
			DescriptionText:     descText,
			URL:                 raw.HostedURL,
			Location:            raw.Categories.Location,
			Department:          raw.Categories.Team,
			PostedAt:            postedAt,
			ScrapedAt:           now,
			NoSponsorshipPhrase: hasSponsorshipIssue(descText),
		})
	}
	return jobs, nil
}

// ─────────────────── AshbyConnector ───────────────────

type AshbyConnector struct {
	company    models.Company
	httpClient *http.Client
}

func NewAshbyConnector(company models.Company) *AshbyConnector {
	return &AshbyConnector{
		company:    company,
		httpClient: &http.Client{Timeout: 20 * time.Second},
	}
}

func (a *AshbyConnector) ATSType() models.ATSType { return models.ATSAshby }
func (a *AshbyConnector) CompanyName() string     { return a.company.Name }

func (a *AshbyConnector) FetchJobs(ctx context.Context) ([]models.Job, error) {
	url := fmt.Sprintf("https://api.ashbyhq.com/posting-api/job-board/%s/jobs", a.company.BoardID)

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", chromeUA)

	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("ashby fetch %s: %w", a.company.Name, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("ashby %s: HTTP %d", a.company.Name, resp.StatusCode)
	}

	var payload struct {
		Jobs []struct {
			ID              string `json:"id"`
			Title           string `json:"title"`
			DescriptionHTML string `json:"descriptionHtml"`
			JobURL          string `json:"jobUrl"`
			Location        string `json:"location"`
			Department      string `json:"department"`
			IsRemote        bool   `json:"isRemote"`
			PublishedAt     string `json:"publishedAt"`
		} `json:"jobs"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		return nil, fmt.Errorf("ashby decode %s: %w", a.company.Name, err)
	}

	now := time.Now().UTC()
	var jobs []models.Job
	for _, raw := range payload.Jobs {
		descText := stripHTML(raw.DescriptionHTML)
		if !passesKeywordFilter(a.company, raw.Title, descText) {
			continue
		}
		var postedAt *time.Time
		if raw.PublishedAt != "" {
			if t, err := time.Parse(time.RFC3339, strings.Replace(raw.PublishedAt, "Z", "+00:00", 1)); err == nil {
				postedAt = &t
			}
		}
		loc := raw.Location
		if raw.IsRemote && loc == "" {
			loc = "Remote"
		}
		jobs = append(jobs, models.Job{
			ID:                  uuid.New().String(),
			SourceID:            raw.ID,
			Source:              models.ATSAshby,
			Company:             a.company.Name,
			Title:               raw.Title,
			DescriptionHTML:     raw.DescriptionHTML,
			DescriptionText:     descText,
			URL:                 raw.JobURL,
			Location:            loc,
			Department:          raw.Department,
			Remote:              raw.IsRemote,
			PostedAt:            postedAt,
			ScrapedAt:           now,
			NoSponsorshipPhrase: hasSponsorshipIssue(descText),
		})
	}
	return jobs, nil
}

// ─────────────────── SmartRecruitersConnector ───────────────────

type SmartRecruitersConnector struct {
	company    models.Company
	httpClient *http.Client
}

func NewSmartRecruitersConnector(company models.Company) *SmartRecruitersConnector {
	return &SmartRecruitersConnector{
		company:    company,
		httpClient: &http.Client{Timeout: 25 * time.Second},
	}
}

func (s *SmartRecruitersConnector) ATSType() models.ATSType { return models.ATSSmartRecruiters }
func (s *SmartRecruitersConnector) CompanyName() string     { return s.company.Name }

func (s *SmartRecruitersConnector) FetchJobs(ctx context.Context) ([]models.Job, error) {
	// SmartRecruiters has a public API for job listings
	baseURL := fmt.Sprintf("https://api.smartrecruiters.com/v1/companies/%s/postings", s.company.BoardID)
	now := time.Now().UTC()
	var allJobs []models.Job
	offset := 0
	limit := 100

	for {
		url := fmt.Sprintf("%s?limit=%d&offset=%d", baseURL, limit, offset)
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
		if err != nil {
			return nil, err
		}
		req.Header.Set("User-Agent", chromeUA)

		resp, err := s.httpClient.Do(req)
		if err != nil {
			return nil, fmt.Errorf("smartrecruiters fetch %s: %w", s.company.Name, err)
		}

		if resp.StatusCode == http.StatusNotFound {
			resp.Body.Close()
			return nil, fmt.Errorf("smartrecruiters: company %q not found", s.company.BoardID)
		}
		if resp.StatusCode != http.StatusOK {
			resp.Body.Close()
			return nil, fmt.Errorf("smartrecruiters %s: HTTP %d", s.company.Name, resp.StatusCode)
		}

		var payload struct {
			Content []struct {
				ID       string `json:"id"`
				Name     string `json:"name"`
				Location struct {
					City    string `json:"city"`
					Country string `json:"country"`
					Remote  bool   `json:"remote"`
				} `json:"location"`
				Department struct {
					Label string `json:"label"`
				} `json:"department"`
				ReleasedDate string `json:"releasedDate"`
				RefNumber    string `json:"refNumber"`
			} `json:"content"`
			TotalFound int `json:"totalFound"`
		}
		if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
			resp.Body.Close()
			return nil, err
		}
		resp.Body.Close()

		for _, raw := range payload.Content {
			title := raw.Name
			loc := strings.TrimSpace(raw.Location.City + ", " + raw.Location.Country)
			if raw.Location.Remote {
				loc = "Remote"
			}
			descText := title // SmartRecruiters list API doesn't return full JD
			if !passesKeywordFilter(s.company, title, descText) {
				continue
			}
			jobURL := fmt.Sprintf("https://jobs.smartrecruiters.com/%s/%s", s.company.BoardID, raw.ID)
			var postedAt *time.Time
			if raw.ReleasedDate != "" {
				if t, err := time.Parse("2006-01-02", raw.ReleasedDate); err == nil {
					postedAt = &t
				}
			}
			allJobs = append(allJobs, models.Job{
				ID:        uuid.New().String(),
				SourceID:  raw.ID,
				Source:    models.ATSSmartRecruiters,
				Company:   s.company.Name,
				Title:     title,
				URL:       jobURL,
				Location:  loc,
				Department: raw.Department.Label,
				Remote:    raw.Location.Remote,
				PostedAt:  postedAt,
				ScrapedAt: now,
			})
		}

		offset += limit
		if offset >= payload.TotalFound || len(payload.Content) == 0 {
			break
		}
	}
	return allJobs, nil
}

// ─────────────────── Helpers ───────────────────

const chromeUA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) " +
	"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

// stripHTML removes HTML tags and collapses whitespace.
// Uses a simple state-machine approach — avoids importing html/template for this.
func stripHTML(html string) string {
	var b strings.Builder
	inTag := false
	for i := 0; i < len(html); i++ {
		c := html[i]
		if c == '<' {
			inTag = true
			b.WriteByte(' ')
			continue
		}
		if c == '>' {
			inTag = false
			continue
		}
		if !inTag {
			b.WriteByte(c)
		}
	}
	// collapse whitespace
	parts := strings.Fields(b.String())
	return strings.Join(parts, " ")
}
