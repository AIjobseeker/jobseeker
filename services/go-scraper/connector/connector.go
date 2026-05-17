// Package connector defines the Connector interface and all ATS-specific
// implementations. Each connector is responsible for one ATS platform.
//
// Design principles:
//   - Interface segregation: Connector is minimal — one method, one concern.
//   - No shared mutable state: all connectors are stateless value types.
//   - Explicit errors: never swallow errors; callers decide how to handle them.
//   - Context propagation: every network call accepts a context for cancellation.
package connector

import (
	"context"
	"fmt"
	"strings"

	"github.com/aijobseeker/go-scraper/internal/models"
)

// Connector fetches all current open jobs for one company.
// Implementations must be safe for concurrent use.
type Connector interface {
	FetchJobs(ctx context.Context) ([]models.Job, error)
	ATSType() models.ATSType
	CompanyName() string
}

// Factory returns the right Connector for a given company config.
// Returns an error if the ATS type is unsupported or config is incomplete.
//
// Potential deadlock note: factory is called once per company per scrape cycle
// from goroutines — it's stateless so no lock needed here.
func Factory(company models.Company) (Connector, error) {
	switch company.ATS {
	case models.ATSGreenhouse:
		if company.BoardID == "" {
			return nil, fmt.Errorf("greenhouse: board_id required for %s", company.Name)
		}
		return NewGreenhouseConnector(company), nil

	case models.ATSLever:
		if company.BoardID == "" {
			return nil, fmt.Errorf("lever: board_id required for %s", company.Name)
		}
		return NewLeverConnector(company), nil

	case models.ATSAshby:
		if company.BoardID == "" {
			return nil, fmt.Errorf("ashby: board_id required for %s", company.Name)
		}
		return NewAshbyConnector(company), nil

	case models.ATSWorkday:
		if company.WorkdayTenant == "" || company.WorkdayBoard == "" {
			return nil, fmt.Errorf("workday: tenant+board required for %s", company.Name)
		}
		return NewWorkdayConnector(company), nil

	case models.ATSSmartRecruiters:
		if company.BoardID == "" {
			return nil, fmt.Errorf("smartrecruiters: board_id required for %s", company.Name)
		}
		return NewSmartRecruitersConnector(company), nil

	case models.ATSCustom:
		return NewCustomConnector(company), nil

	default:
		return nil, fmt.Errorf("unsupported ATS type %q for %s", company.ATS, company.Name)
	}
}

// passesKeywordFilter decides whether a job posting matches what we're
// looking for. Logic is intentionally simple — pure substring match on the
// title only (description is too noisy: a sales role at an "engineering-led"
// company would otherwise pass).
//
// Rules:
//   1. If title contains any exclude keyword, reject (excludes always win).
//   2. If no include keywords are configured, accept.
//   3. Otherwise, title must contain at least one include keyword.
//
// This is NOT semantic matching — there is no AI/ML here. Tighten the
// per-company keyword list in the seed YAML if you want fewer false
// positives, or loosen it for broader coverage.
func passesKeywordFilter(company models.Company, title, _description string) bool {
	t := strings.ToLower(title)

	for _, kw := range company.KeywordsExclude {
		if strings.Contains(t, strings.ToLower(kw)) {
			return false
		}
	}
	if len(company.KeywordsInclude) == 0 {
		return true
	}
	for _, kw := range company.KeywordsInclude {
		if strings.Contains(t, strings.ToLower(kw)) {
			return true
		}
	}
	return false
}

// hasSponsorshipIssue scans the description for common "no sponsorship" phrases.
// Returns true if the posting likely does NOT support visa sponsorship.
func hasSponsorshipIssue(text string) bool {
	phrases := []string{
		"no visa sponsorship",
		"not able to sponsor",
		"cannot sponsor",
		"sponsorship is not available",
		"authorization to work in the us",
		"must be authorized to work",
		"us citizenship required",
		"must already be authorized",
	}
	lower := strings.ToLower(text)
	for _, p := range phrases {
		if strings.Contains(lower, p) {
			return true
		}
	}
	return false
}
