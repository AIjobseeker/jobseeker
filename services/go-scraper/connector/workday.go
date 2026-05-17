package connector

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/aijobseeker/go-scraper/internal/models"
	"github.com/google/uuid"
)

// WorkdayConnector hits Workday's undocumented internal JSON API.
//
// Almost every Workday careers page calls a POST endpoint under the hood:
//   POST https://{tenant}.wd5.myworkdayjobs.com/wday/cxs/{tenant}/{board}/jobs
//
// The tenant and board are discoverable by opening the careers page in
// Chrome DevTools → Network tab → filter XHR → look for "jobs" POST request.
//
// Potential issue: Workday sometimes changes the path segment version
// (wd5 → wd1 → wd3). We try multiple variants on 404.
type WorkdayConnector struct {
	company    models.Company
	httpClient *http.Client
}

func NewWorkdayConnector(company models.Company) *WorkdayConnector {
	return &WorkdayConnector{
		company:    company,
		httpClient: &http.Client{Timeout: 30 * time.Second},
	}
}

func (w *WorkdayConnector) ATSType() models.ATSType { return models.ATSWorkday }
func (w *WorkdayConnector) CompanyName() string     { return w.company.Name }

type workdayRequest struct {
	Limit      int                      `json:"limit"`
	Offset     int                      `json:"offset"`
	SearchText string                   `json:"searchText"`
	Locations  []map[string]string      `json:"locations"`
}

func (w *WorkdayConnector) FetchJobs(ctx context.Context) ([]models.Job, error) {
	tenant := w.company.WorkdayTenant
	primary := w.company.WorkdayBoard

	// Multi-site probing. Many Workday tenants serve multiple "site" boards
	// (External_Career_Site, Campus_Career_Site, Internal_Mobility, etc.).
	// We try the primary board first, then a small set of common variants
	// per tenant. The first 200/200 response wins for each board, and we
	// dedupe across boards by externalPath (Workday's canonical path).
	candidateBoards := []string{primary}
	commonVariants := []string{
		"External_Career_Site",
		"External",
		"Campus_Career_Site",
		"Campus",
		"University_Recruiting",
		"Students",
	}
	for _, v := range commonVariants {
		if !contains(candidateBoards, v) {
			candidateBoards = append(candidateBoards, v)
		}
	}

	locations := make([]map[string]string, len(w.company.WorkdayLocationIDs))
	for i, id := range w.company.WorkdayLocationIDs {
		locations[i] = map[string]string{"id": id}
	}

	now := time.Now().UTC()
	var allJobs []models.Job
	seenPaths := make(map[string]struct{})
	successfulBoards := 0

	for _, board := range candidateBoards {
		// Try known Workday sub-domains for THIS tenant+board combination.
		baseURLs := []string{
			fmt.Sprintf("https://%s.wd5.myworkdayjobs.com/wday/cxs/%s/%s", tenant, tenant, board),
			fmt.Sprintf("https://%s.wd1.myworkdayjobs.com/wday/cxs/%s/%s", tenant, tenant, board),
			fmt.Sprintf("https://%s.wd3.myworkdayjobs.com/wday/cxs/%s/%s", tenant, tenant, board),
		}

		for _, baseURL := range baseURLs {
			jobs, err := w.fetchFromURL(ctx, baseURL+"/jobs", locations, now)
			if err != nil {
				// 404 / 401 / 403 = this board doesn't exist on this site.
				// Move on; don't propagate as a fatal error if at least one
				// board succeeds.
				if strings.Contains(err.Error(), "404") ||
					strings.Contains(err.Error(), "401") ||
					strings.Contains(err.Error(), "403") {
					break
				}
				// On context cancel, propagate so the worker can shut down.
				if ctx.Err() != nil {
					return allJobs, ctx.Err()
				}
				// Other errors (timeout, decode) — try the next baseURL variant
				continue
			}
			// Dedupe by externalPath across boards — same job posted on
			// External + Campus must NOT count twice.
			for _, j := range jobs {
				if _, dup := seenPaths[j.SourceID]; dup {
					continue
				}
				seenPaths[j.SourceID] = struct{}{}
				allJobs = append(allJobs, j)
			}
			successfulBoards++
			break // got jobs on this board variant; move to next board
		}
	}

	if successfulBoards == 0 {
		// No board variants worked for this tenant. Surface as a non-transient
		// error so the circuit breaker opens and we stop retrying for a while.
		return nil, fmt.Errorf("workday %s: no board variant returned 200 (tried %d)",
			w.company.Name, len(candidateBoards))
	}
	return allJobs, nil
}

func contains(haystack []string, needle string) bool {
	for _, h := range haystack {
		if h == needle {
			return true
		}
	}
	return false
}

func (w *WorkdayConnector) fetchFromURL(
	ctx context.Context,
	url string,
	locations []map[string]string,
	now time.Time,
) ([]models.Job, error) {
	const pageSize = 20
	var allJobs []models.Job
	offset := 0

	for {
		reqBody := workdayRequest{
			Limit:     pageSize,
			Offset:    offset,
			Locations: locations,
		}
		body, _ := json.Marshal(reqBody)

		req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
		if err != nil {
			return nil, err
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("User-Agent", chromeUA)
		req.Header.Set("Accept", "application/json")

		resp, err := w.httpClient.Do(req)
		if err != nil {
			return nil, fmt.Errorf("workday fetch %s: %w", w.company.Name, err)
		}

		if resp.StatusCode == http.StatusNotFound {
			resp.Body.Close()
			return nil, fmt.Errorf("workday 404 for %s", url)
		}
		if resp.StatusCode != http.StatusOK {
			resp.Body.Close()
			return nil, fmt.Errorf("workday %s: HTTP %d", w.company.Name, resp.StatusCode)
		}

		var payload struct {
			JobPostings []struct {
				Title         string `json:"title"`
				ExternalPath  string `json:"externalPath"`
				PostedOn      string `json:"postedOn"`
				LocationsText string `json:"locationsText"`
				BulletFields  []string `json:"bulletFields"`
			} `json:"jobPostings"`
			Total int `json:"total"`
		}
		if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
			resp.Body.Close()
			return nil, err
		}
		resp.Body.Close()

		for _, raw := range payload.JobPostings {
			if !passesKeywordFilter(w.company, raw.Title, raw.Title) {
				continue
			}
			// Build canonical URL from external path
			jobURL := fmt.Sprintf("https://%s.wd5.myworkdayjobs.com/en-US/%s%s",
				w.company.WorkdayTenant, w.company.WorkdayBoard, raw.ExternalPath)

			var postedAt *time.Time
			if raw.PostedOn != "" {
				// Workday uses "Posted X Days Ago" — skip parsing, set nil
				// Or sometimes ISO: try both
				if t, err := time.Parse(time.RFC3339, raw.PostedOn); err == nil {
					postedAt = &t
				}
			}

			sourceID := raw.ExternalPath
			if len(raw.BulletFields) > 0 {
				sourceID = raw.BulletFields[0]
			}

			allJobs = append(allJobs, models.Job{
				ID:        uuid.New().String(),
				SourceID:  sourceID,
				Source:    models.ATSWorkday,
				Company:   w.company.Name,
				Title:     raw.Title,
				URL:       jobURL,
				Location:  raw.LocationsText,
				PostedAt:  postedAt,
				ScrapedAt: now,
			})
		}

		offset += pageSize
		if offset >= payload.Total || len(payload.JobPostings) == 0 {
			break
		}
		// Safety cap: max 500 jobs per company per run
		if offset > 500 {
			break
		}
	}
	return allJobs, nil
}

// ─────────────────── CustomConnector ───────────────────
// Handles Apple, Google, Amazon — each with their own undiscovered REST API.

type CustomConnector struct {
	company    models.Company
	httpClient *http.Client
}

func NewCustomConnector(company models.Company) *CustomConnector {
	return &CustomConnector{
		company:    company,
		httpClient: &http.Client{Timeout: 25 * time.Second},
	}
}

func (c *CustomConnector) ATSType() models.ATSType { return models.ATSCustom }
func (c *CustomConnector) CompanyName() string     { return c.company.Name }

func (c *CustomConnector) FetchJobs(ctx context.Context) ([]models.Job, error) {
	switch c.company.CustomModule {
	case "apple":
		return c.fetchApple(ctx)
	case "google":
		return c.fetchGoogle(ctx)
	case "amazon":
		return c.fetchAmazon(ctx)
	case "meta":
		return c.fetchMeta(ctx)
	default:
		return nil, fmt.Errorf("unknown custom module: %q for %s", c.company.CustomModule, c.company.Name)
	}
}

func (c *CustomConnector) fetchApple(ctx context.Context) ([]models.Job, error) {
	url := "https://jobs.apple.com/api/role/search"
	now := time.Now().UTC()
	var allJobs []models.Job
	page := 1

	// Comprehensive Apple team filter — covers all SWE / IT / SRE-adjacent
	// orgs. We pull broad here and let the scorer's niche-density rules
	// filter at scoring time. Better than narrow scrape-time filtering
	// where we'd miss SRE jobs hidden in unexpected teams.
	defaultTeams := strings.Join([]string{
		"team-software-and-services",
		"team-information-systems-and-technology",
		"team-machine-learning-and-ai",
		"team-hardware",
		"team-services",
		"team-corporate-functions",
	}, ",")

	for {
		teams := defaultTeams
		if t, ok := c.company.CustomParams["teams"]; ok && t != "" {
			teams = t
		}
		locations := "postLocation-USA"
		if loc, ok := c.company.CustomParams["locations"]; ok && loc != "" {
			locations = loc
		}

		payload := map[string]any{
			"filters": map[string]any{
				"postingpostLocation": []string{locations},
				"team":                strings.Split(teams, ","),
			},
			"page":   page,
			"locale": "en-us",
			"query":  "",
		}
		body, _ := json.Marshal(payload)

		req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
		if err != nil {
			return nil, err
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("User-Agent", chromeUA)

		resp, err := c.httpClient.Do(req)
		if err != nil {
			return nil, fmt.Errorf("apple fetch: %w", err)
		}

		if resp.StatusCode != http.StatusOK {
			resp.Body.Close()
			return nil, fmt.Errorf("apple fetch: HTTP %d", resp.StatusCode)
		}
		ct := resp.Header.Get("Content-Type")
		if !strings.Contains(ct, "json") {
			resp.Body.Close()
			return nil, fmt.Errorf("apple fetch: non-JSON response (%s) — likely blocked or captcha", ct)
		}

		var data struct {
			SearchResults []struct {
				PositionID           string `json:"positionId"`
				PostingTitle         string `json:"postingTitle"`
				TransformedPostTitle string `json:"transformedPostingTitle"`
				HomeOffice           string `json:"homeOffice"`
				Team                 struct {
					TeamName string `json:"teamName"`
				} `json:"team"`
				PostingDate string `json:"postingDate"`
			} `json:"searchResults"`
			TotalPages int `json:"totalPages"`
		}
		if err := json.NewDecoder(resp.Body).Decode(&data); err != nil {
			resp.Body.Close()
			return nil, fmt.Errorf("apple decode: %w", err)
		}
		resp.Body.Close()

		for _, raw := range data.SearchResults {
			if !passesKeywordFilter(c.company, raw.PostingTitle, raw.Team.TeamName) {
				continue
			}
			jobURL := fmt.Sprintf("https://jobs.apple.com/en-us/details/%s/%s",
				raw.PositionID, raw.TransformedPostTitle)
			allJobs = append(allJobs, models.Job{
				ID:        uuid.New().String(),
				SourceID:  raw.PositionID,
				Source:    models.ATSCustom,
				Company:   "Apple",
				Title:     raw.PostingTitle,
				URL:       jobURL,
				Location:  raw.HomeOffice,
				Department: raw.Team.TeamName,
				ScrapedAt: now,
			})
		}

		if page >= data.TotalPages {
			break
		}
		page++
	}
	return allJobs, nil
}

func (c *CustomConnector) fetchGoogle(ctx context.Context) ([]models.Job, error) {
	url := "https://careers.google.com/api/v3/search/"
	now := time.Now().UTC()
	var allJobs []models.Job
	page := 1
	const maxPages = 20 // safety cap

	// Default: no query — pull every engineering job, let scorer filter.
	// Override via custom_params.query in seed if we need narrower scope.
	query := ""
	if q, ok := c.company.CustomParams["query"]; ok {
		query = q
	}
	location := "United States"
	if loc, ok := c.company.CustomParams["location"]; ok && loc != "" {
		location = loc
	}

	for {
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
		if err != nil {
			return nil, err
		}
		q := req.URL.Query()
		if query != "" {
			q.Set("q", query)
		}
		q.Set("location", location)
		q.Set("page", fmt.Sprintf("%d", page))
		q.Set("num", "100") // Google allows up to 100/page
		// Engineering jobs only — Google exposes a category filter
		q.Add("category", "Software Engineering")
		q.Add("category", "Site Reliability Engineering")
		q.Add("category", "Network Engineering")
		q.Add("category", "Data Center & Hardware Engineering")
		req.URL.RawQuery = q.Encode()
		req.Header.Set("User-Agent", chromeUA)

		resp, err := c.httpClient.Do(req)
		if err != nil {
			return nil, fmt.Errorf("google fetch: %w", err)
		}

		var data struct {
			Jobs []struct {
				ID          string   `json:"id"`
				Title       string   `json:"title"`
				Description string   `json:"description"`
				ApplyURL    string   `json:"apply_url"`
				Locations   []string `json:"locations"`
				Date        string   `json:"date"`
			} `json:"jobs"`
			NextPage bool `json:"next_page"`
		}
		if err := json.NewDecoder(resp.Body).Decode(&data); err != nil {
			resp.Body.Close()
			break
		}
		resp.Body.Close()

		for _, raw := range data.Jobs {
			descText := stripHTML(raw.Description)
			if !passesKeywordFilter(c.company, raw.Title, descText) {
				continue
			}
			allJobs = append(allJobs, models.Job{
				ID:              uuid.New().String(),
				SourceID:        raw.ID,
				Source:          models.ATSCustom,
				Company:         "Google",
				Title:           raw.Title,
				DescriptionText: descText,
				URL:             raw.ApplyURL,
				Location:        strings.Join(raw.Locations, ", "),
				ScrapedAt:       now,
			})
		}
		if !data.NextPage || page >= maxPages {
			break
		}
		page++
	}
	return allJobs, nil
}

func (c *CustomConnector) fetchAmazon(ctx context.Context) ([]models.Job, error) {
	url := "https://www.amazon.jobs/en/search.json"
	now := time.Now().UTC()
	var allJobs []models.Job
	offset := 0
	const pageSize = 100 // Amazon allows up to 100/page; was 20

	// Comprehensive Amazon category set covering all SRE-relevant orgs.
	// Was: just software-development + operations-it-support-engineering.
	defaultCategories := []string{
		"software-development",
		"operations-it-support-engineering",
		"systems-quality-and-security-engineering",
		"solutions-architect",
		"data-engineering",
		"machine-learning-science",
	}
	categories := defaultCategories
	if csv, ok := c.company.CustomParams["categories"]; ok && csv != "" {
		categories = strings.Split(csv, ",")
	}

	for {
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
		if err != nil {
			return nil, err
		}
		q := req.URL.Query()
		q.Add("normalized_location[]", "United States")
		for _, cat := range categories {
			q.Add("category[]", strings.TrimSpace(cat))
		}
		q.Set("result_limit", fmt.Sprintf("%d", pageSize))
		q.Set("offset", fmt.Sprintf("%d", offset))
		req.URL.RawQuery = q.Encode()
		req.Header.Set("User-Agent", chromeUA)

		resp, err := c.httpClient.Do(req)
		if err != nil {
			return nil, fmt.Errorf("amazon fetch: %w", err)
		}

		var data struct {
			Jobs []struct {
				ID       int    `json:"id_icims"`
				Title    string `json:"title"`
				JobPath  string `json:"job_path"`
				Location string `json:"normalized_location"`
				Category string `json:"category"`
			} `json:"jobs"`
			Hits int `json:"hits"`
		}
		if err := json.NewDecoder(resp.Body).Decode(&data); err != nil {
			resp.Body.Close()
			break
		}
		resp.Body.Close()

		for _, raw := range data.Jobs {
			if !passesKeywordFilter(c.company, raw.Title, raw.Category) {
				continue
			}
			allJobs = append(allJobs, models.Job{
				ID:        uuid.New().String(),
				SourceID:  fmt.Sprintf("%d", raw.ID),
				Source:    models.ATSCustom,
				Company:   "Amazon",
				Title:     raw.Title,
				URL:       "https://www.amazon.jobs" + raw.JobPath,
				Location:  raw.Location,
				ScrapedAt: now,
			})
		}

		offset += pageSize
		if offset >= data.Hits || offset > 400 {
			break
		}
	}
	return allJobs, nil
}

// fetchMeta scrapes the Meta careers site.
//
// Meta exposes job listings via /v1/careers-graph/api/v1/careers (JSON over POST)
// for filtered searches. Pagination uses `page` + `results_per_page`.
//
// Implementation notes:
//   - Older code used a placeholder GraphQL query that returned 0 jobs
//     (the parser was a `_ = data` stub). This version actually parses.
//   - We pull broad — divisions=engineering-tech, no q-filter — and let the
//     scorer's niche-density rules pick SRE-relevant roles downstream.
//   - The endpoint is rate-sensitive; rely on the global per-domain limiter
//     (metacareers.com gets 0.2 req/sec by default) to stay polite.
func (c *CustomConnector) fetchMeta(ctx context.Context) ([]models.Job, error) {
	endpoint := "https://www.metacareers.com/graphql"
	now := time.Now().UTC()
	var allJobs []models.Job
	const maxPages = 10
	const pageSize = 50

	// Optional override via custom_params.divisions / custom_params.offices.
	// Defaults pull all engineering-tech roles (most operational SRE roles
	// live here at Meta).
	divisions := []string{"engineering-tech"}
	if csv, ok := c.company.CustomParams["divisions"]; ok && csv != "" {
		divisions = strings.Split(csv, ",")
	}
	// "offices" filter restricts location. Default: all US offices + remote.
	offices := []string{
		"remote",
		"menlo-park-ca",
		"new-york-ny",
		"seattle-wa",
		"sunnyvale-ca",
		"san-francisco-ca",
		"bellevue-wa",
		"redmond-wa",
		"burlingame-ca",
		"fremont-ca",
		"los-angeles-ca",
		"chicago-il",
		"austin-tx",
		"boston-ma",
		"washington-dc",
	}
	if csv, ok := c.company.CustomParams["offices"]; ok && csv != "" {
		offices = strings.Split(csv, ",")
	}

	query := `query CareersJobSearchResultsQuery($search_input: JobSearchInput!) {
		job_search(search_input: $search_input) {
			results {
				id
				title
				locations
				teams
				description
				url
			}
		}
	}`

	for page := 1; page <= maxPages; page++ {
		body := map[string]any{
			"query": query,
			"variables": map[string]any{
				"search_input": map[string]any{
					"q":                "",
					"divisions":        divisions,
					"offices":          offices,
					"results_per_page": pageSize,
					"page":             page,
				},
			},
		}
		raw, _ := json.Marshal(body)
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(raw))
		if err != nil {
			return nil, err
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("User-Agent", chromeUA)
		req.Header.Set("X-FB-Friendly-Name", "CareersJobSearchResultsQuery")

		resp, err := c.httpClient.Do(req)
		if err != nil {
			return nil, fmt.Errorf("meta fetch: %w", err)
		}
		if resp.StatusCode != http.StatusOK {
			resp.Body.Close()
			return nil, fmt.Errorf("meta fetch: HTTP %d", resp.StatusCode)
		}

		var data struct {
			Data struct {
				JobSearch struct {
					Results []struct {
						ID          string   `json:"id"`
						Title       string   `json:"title"`
						Locations   []string `json:"locations"`
						Teams       []string `json:"teams"`
						Description string   `json:"description"`
						URL         string   `json:"url"`
					} `json:"results"`
				} `json:"job_search"`
			} `json:"data"`
			Errors []struct {
				Message string `json:"message"`
			} `json:"errors"`
		}
		if err := json.NewDecoder(resp.Body).Decode(&data); err != nil {
			resp.Body.Close()
			return nil, fmt.Errorf("meta decode: %w", err)
		}
		resp.Body.Close()

		if len(data.Errors) > 0 {
			return nil, fmt.Errorf("meta graphql error: %s", data.Errors[0].Message)
		}

		results := data.Data.JobSearch.Results
		if len(results) == 0 {
			break
		}

		for _, raw := range results {
			descText := stripHTML(raw.Description)
			if !passesKeywordFilter(c.company, raw.Title, descText) {
				continue
			}
			loc := strings.Join(raw.Locations, ", ")
			team := ""
			if len(raw.Teams) > 0 {
				team = raw.Teams[0]
			}
			jobURL := raw.URL
			if jobURL == "" && raw.ID != "" {
				jobURL = fmt.Sprintf("https://www.metacareers.com/jobs/%s/", raw.ID)
			}
			allJobs = append(allJobs, models.Job{
				ID:              uuid.New().String(),
				SourceID:        raw.ID,
				Source:          models.ATSCustom,
				Company:         "Meta",
				Title:           raw.Title,
				DescriptionText: descText,
				URL:             jobURL,
				Location:        loc,
				Department:      team,
				ScrapedAt:       now,
			})
		}

		if len(results) < pageSize {
			break
		}
	}

	return allJobs, nil
}
