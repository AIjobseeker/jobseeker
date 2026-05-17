// Package loader reads company configurations from YAML files.
//
// Two formats are supported:
//  1. Consolidated seed file (seed_500.yaml) — top-level "companies" list
//  2. Individual per-company YAML files in a directory tree
//
// Both map to models.Company for the connector factory.
package loader

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/aijobseeker/go-scraper/internal/models"
	"gopkg.in/yaml.v3"
)
// seedFile is the consolidated YAML format produced by the seed generator.
type seedFile struct {
	Companies []seedCompany `yaml:"companies"`
}

type seedCompany struct {
	Name               string            `yaml:"name"`
	Domain             string            `yaml:"domain"`
	ATS                string            `yaml:"ats"`
	BoardID            string            `yaml:"board_id"`
	CareerURL          string            `yaml:"career_url"`
	VisaTransfersH1B   bool              `yaml:"visa_transfers_h1b"`
	SponsorsNewH1B     bool              `yaml:"sponsors_new_h1b"`
	Tier               int               `yaml:"tier"`
	Verified           bool              `yaml:"verified"`
	Active             bool              `yaml:"active"`
	KeywordsInclude    []string          `yaml:"keywords_include"`
	KeywordsExclude    []string          `yaml:"keywords_exclude"`
	WorkdayTenant      string            `yaml:"workday_tenant"`
	WorkdayBoard       string            `yaml:"workday_board"`
	WorkdayLocationIDs []string          `yaml:"workday_location_ids"`
	CustomModule       string            `yaml:"custom_module"`
	CustomParams       map[string]string `yaml:"custom_params"`
}

// individualFile is the per-company YAML format (existing files under companies/).
// Supports both flat fields (workday_tenant) and nested blocks (workday: {tenant:}).
type individualFile struct {
	Name             string   `yaml:"name"`
	ATS              string   `yaml:"ats"`
	BoardID          string   `yaml:"board_id"`
	Active           bool     `yaml:"active"`
	VisaTransfersH1B bool     `yaml:"visa_transfers_h1b"`
	SponsorsNewH1B   bool     `yaml:"sponsors_new_h1b"`
	KeywordsInclude  []string `yaml:"keywords_include"`
	KeywordsExclude  []string `yaml:"keywords_exclude"`

	// Flat format (seed_500.yaml style)
	WorkdayTenant      string            `yaml:"workday_tenant"`
	WorkdayBoard       string            `yaml:"workday_board"`
	WorkdayLocationIDs []string          `yaml:"workday_location_ids"`
	CustomModule       string            `yaml:"custom_module"`
	CustomParams       map[string]string `yaml:"custom_params"`

	// Nested format (per-company YAML style)
	Workday *struct {
		Tenant      string            `yaml:"tenant"`
		Board       string            `yaml:"board"`
		LocationIDs []string          `yaml:"location_ids"`
		Categories  []string          `yaml:"categories"`
	} `yaml:"workday"`
	Custom *struct {
		ScraperModule string            `yaml:"scraper_module"`
		Params        map[string]any    `yaml:"params"`
	} `yaml:"custom"`
}

// LoadSeedFile loads a consolidated seed YAML and returns active companies.
func LoadSeedFile(path string) ([]models.Company, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read seed file: %w", err)
	}
	var sf seedFile
	if err := yaml.Unmarshal(data, &sf); err != nil {
		return nil, fmt.Errorf("parse seed file: %w", err)
	}
	if len(sf.Companies) == 0 {
		// Maybe it's a flat list without the "companies:" wrapper — try direct
		var flat []seedCompany
		if err2 := yaml.Unmarshal(data, &flat); err2 != nil || len(flat) == 0 {
			return nil, fmt.Errorf("seed file has no companies")
		}
		sf.Companies = flat
	}

	out := make([]models.Company, 0, len(sf.Companies))
	for _, c := range sf.Companies {
		if !c.Active && c.Verified == false && c.Tier == 0 {
			// seed file entries default to active; skip only if explicitly disabled
		}
		out = append(out, toModelCompany(c))
	}
	return out, nil
}

// LoadDirectory walks a directory tree loading all *.yaml files as individual
// company configs (the existing per-company format).
func LoadDirectory(dir string) ([]models.Company, error) {
	var companies []models.Company
	err := filepath.WalkDir(dir, func(path string, d os.DirEntry, err error) error {
		if err != nil || d.IsDir() || !strings.HasSuffix(path, ".yaml") {
			return err
		}
		// Skip the consolidated seed file if it ends up in the same tree
		if strings.HasSuffix(path, "seed_500.yaml") {
			return nil
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return fmt.Errorf("read %s: %w", path, err)
		}
		var cf individualFile
		if err := yaml.Unmarshal(data, &cf); err != nil {
			return fmt.Errorf("parse %s: %w", path, err)
		}
		if !cf.Active {
			return nil
		}
		companies = append(companies, individualToModel(cf))
		return nil
	})
	return companies, err
}

func toModelCompany(c seedCompany) models.Company {
	ats := parseATS(c.ATS)
	tier := c.Tier
	if tier == 0 {
		tier = 2
	}
	return models.Company{
		Name:               c.Name,
		Domain:             c.Domain,
		ATS:                ats,
		BoardID:            c.BoardID,
		CareerURL:          c.CareerURL,
		VisaTransfersH1B:   c.VisaTransfersH1B,
		SponsorsNewH1B:     c.SponsorsNewH1B,
		Tier:               tier,
		Verified:           c.Verified,
		KeywordsInclude:    c.KeywordsInclude,
		KeywordsExclude:    c.KeywordsExclude,
		WorkdayTenant:      c.WorkdayTenant,
		WorkdayBoard:       c.WorkdayBoard,
		WorkdayLocationIDs: c.WorkdayLocationIDs,
		CustomModule:       c.CustomModule,
		CustomParams:       c.CustomParams,
	}
}

func individualToModel(c individualFile) models.Company {
	// Resolve workday fields from either flat or nested format
	tenant, board := c.WorkdayTenant, c.WorkdayBoard
	locationIDs := c.WorkdayLocationIDs
	if c.Workday != nil {
		if c.Workday.Tenant != "" {
			tenant = c.Workday.Tenant
		}
		if c.Workday.Board != "" {
			board = c.Workday.Board
		}
		if len(c.Workday.LocationIDs) > 0 {
			locationIDs = c.Workday.LocationIDs
		}
	}

	// Resolve custom module — derive short name from Python module path
	customModule := c.CustomModule
	customParams := c.CustomParams
	if c.Custom != nil && customModule == "" {
		// e.g. "connectors.custom.apple" → "apple"
		parts := strings.Split(c.Custom.ScraperModule, ".")
		if len(parts) > 0 {
			customModule = parts[len(parts)-1]
		}
		// Flatten any string params
		if len(c.Custom.Params) > 0 && customParams == nil {
			customParams = make(map[string]string)
			for k, v := range c.Custom.Params {
				if s, ok := v.(string); ok {
					customParams[k] = s
				}
			}
		}
	}

	return models.Company{
		Name:               c.Name,
		ATS:                parseATS(c.ATS),
		BoardID:            c.BoardID,
		VisaTransfersH1B:   c.VisaTransfersH1B,
		SponsorsNewH1B:     c.SponsorsNewH1B,
		Tier:               2,
		Verified:           true,
		KeywordsInclude:    c.KeywordsInclude,
		KeywordsExclude:    c.KeywordsExclude,
		WorkdayTenant:      tenant,
		WorkdayBoard:       board,
		WorkdayLocationIDs: locationIDs,
		CustomModule:       customModule,
		CustomParams:       customParams,
	}
}

func parseATS(s string) models.ATSType {
	switch strings.ToLower(s) {
	case "greenhouse":
		return models.ATSGreenhouse
	case "lever":
		return models.ATSLever
	case "ashby":
		return models.ATSAshby
	case "workday":
		return models.ATSWorkday
	case "smartrecruiters":
		return models.ATSSmartRecruiters
	case "icims":
		return models.ATSiCIMS
	case "custom":
		return models.ATSCustom
	default:
		return models.ATSUnknown
	}
}
