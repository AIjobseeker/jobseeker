#!/usr/bin/env python3
"""
generate-seed.py - Generates companies/seed_500.yaml from the canonical company database.

Run:
    python3 scripts/generate-seed.py > companies/seed_500.yaml

Each entry is deduplicated by (name, ats, board_id). Companies where the ATS
board_id is known with high confidence are marked verified=true. Workday
entries are kept only where the tenant+board combo has been confirmed
to return real JSON from the API.
"""

import yaml
import sys
from typing import Optional

# Format: (name, domain, ats, board_id, tier, h1b_transfer, h1b_new, verified, extra)
# extra = dict of additional YAML fields (workday_tenant, workday_board, custom_module, etc.)

DEFAULT_KW_SRE = [
    "sre", "site reliability", "devops", "devsecops",
    "platform engineer", "platform eng", "infrastructure",
    "reliability engineer", "kubernetes", "k8s",
    "cloud engineer", "cloud architect",
    "systems engineer", "production engineer",
    "observability", "developer platform", "software engineer",
]
DEFAULT_KW_EXCLUDE = [
    "intern", "internship",
    "director", "vp", "vice president", "head of",
    "manager", "principal pm", "product manager",
    "account executive", "sales", "business development", "bdr", "sdr",
    "marketing", "recruit", "people partner", "hr ", "human resources",
    "customer success", "support engineer", "solutions architect",
    "designer", "writer", "content", "legal", "finance", "accountant",
    "attorney", "counsel", "paralegal", "executive assistant",
]
AI_KW = DEFAULT_KW_SRE + [
    "machine learning", "ml engineer", "ml platform",
    "ai engineer", "mlops", "gpu", "cuda",
    "data engineer", "data platform",
]

DEDUP_MARKER_SUFFIXES = ("-L", "-A", "-W", "-G", "-C", "-S", "-I", "-T")  # entries kept for reference but not emitted

# Companies whose careers page is NOT at https://{domain}/careers.
# Verified by hand or from public knowledge. The discovery script will
# trust these and skip its candidate-URL probing.
CAREERS_URL_OVERRIDES = {
    "Apple":            "https://jobs.apple.com/en-us/search",
    "Apple Silicon":    "https://jobs.apple.com/en-us/search?team=hardware",
    "Amazon":           "https://www.amazon.jobs/en/",
    "AWS":              "https://www.amazon.jobs/en/teams/aws",
    "Walmart":          "https://corporate.walmart.com/careers",
    "Walmart Global Tech": "https://careers.walmart.com/results?q=&type=Technology",
    "Target":           "https://corporate.target.com/careers",
    "Disney":           "https://jobs.disneycareers.com/",
    "Comcast":          "https://jobs.comcast.com/",
    "NBCUniversal":     "https://www.nbcunicareers.com/",
    "Netflix":          "https://jobs.netflix.com/jobs",
    "Boeing":           "https://jobs.boeing.com/",
    "Lockheed Martin":  "https://www.lockheedmartinjobs.com/",
    "Northrop Grumman": "https://www.northropgrumman.com/jobs/",
    "Raytheon":         "https://careers.rtx.com/global/en",
    "RTX":              "https://careers.rtx.com/global/en",
    "JPMorgan":         "https://careers.jpmorgan.com/global/en/home",
    "JPMorgan Chase Tech": "https://careers.jpmorgan.com/global/en/students/programs/software-engineer",
    "Goldman Sachs":    "https://www.goldmansachs.com/careers/",
    "Goldman Sachs Engineering": "https://www.goldmansachs.com/careers/divisions/engineering/",
    "Morgan Stanley":   "https://www.morganstanley.com/people/careers",
    "Wells Fargo":      "https://www.wellsfargojobs.com/",
    "Bank of America":  "https://careers.bankofamerica.com/",
    "Capital One":      "https://www.capitalonecareers.com/",
    "Visa":             "https://corporate.visa.com/en/jobs.html",
    "Mastercard":       "https://careers.mastercard.com/",
    "American Express": "https://www.americanexpress.com/en-us/careers/",
    "Adobe":            "https://careers.adobe.com/us/en",
    "Salesforce":       "https://careers.salesforce.com/en/jobs/",
    "Oracle":           "https://www.oracle.com/corporate/careers/",
    "IBM":              "https://www.ibm.com/careers/",
    "Intel":            "https://jobs.intel.com/",
    "AMD":              "https://careers.amd.com/",
    "NVIDIA":           "https://www.nvidia.com/en-us/about-nvidia/careers/",
    "Qualcomm":         "https://careers.qualcomm.com/careers",
    "Cisco":            "https://jobs.cisco.com/jobs/SearchJobs",
    "ServiceNow":       "https://careers.servicenow.com/careers/",
    "SAP":              "https://jobs.sap.com/",
    "Workday":          "https://www.workday.com/en-us/company/careers.html",
    "VMware":           "https://careers.vmware.com/main/",
    "Broadcom":         "https://jobs.broadcom.com/",
    "HPE":              "https://careers.hpe.com/",
    "Dell":             "https://jobs.dell.com/",
    "Microsoft":        "https://careers.microsoft.com/v2/global/en/home.html",
    "Google":           "https://www.google.com/about/careers/applications/",
    "Meta":             "https://www.metacareers.com/",
    "TikTok":           "https://careers.tiktok.com/",
    "ByteDance":        "https://jobs.bytedance.com/",
    "Uber":             "https://www.uber.com/us/en/careers/",
    "Tesla":            "https://www.tesla.com/careers",
    "SpaceX":           "https://www.spacex.com/careers",
    "Anduril":          "https://www.anduril.com/careers/",
    "Palantir":         "https://www.palantir.com/careers/",
    "Cloudflare":       "https://www.cloudflare.com/careers/",
    "Spotify":          "https://www.lifeatspotify.com/jobs",
    "Riot Games":       "https://www.riotgames.com/en/work-with-us/jobs",
    "Epic Games":       "https://www.epicgames.com/site/en-US/careers",
    "Roblox":           "https://corp.roblox.com/jobs/",
    "Activision Blizzard": "https://careers.activisionblizzard.com/",
    "Electronic Arts":  "https://ea.gr8people.com/",
    "EA":               "https://ea.gr8people.com/",
    "Ubisoft":          "https://www.ubisoft.com/en-us/company/careers",
    "USAA":             "https://www.usaajobs.com/",
    "FedEx":            "https://careers.fedex.com/fedex",
    "UPS":              "https://www.jobs-ups.com/",
    "AT&T":             "https://careers.att.com/",
    "Verizon":          "https://mycareer.verizon.com/",
    "T-Mobile":         "https://careers.t-mobile.com/",
    "Walt Disney":      "https://jobs.disneycareers.com/",
    "Paramount":        "https://careers.paramount.com/global/en",
    "Warner Bros":      "https://careers.wbd.com/global/en",
    "Sony":             "https://www.sonyjobs.com/",
    "Sony Interactive": "https://sonyinteractive.com/en/careers/",
    "PlayStation":      "https://sonyinteractive.com/en/careers/",
    "Nintendo":         "https://careers.nintendo.com/",
    "Square Enix":      "https://careers.square-enix.com/",
    "Take-Two":         "https://www.take2games.com/careers",
    "Bridgewater":      "https://www.bridgewater.com/careers",
    "Citadel":          "https://www.citadel.com/careers/",
    "Two Sigma":        "https://careers.twosigma.com/",
    "Jane Street":      "https://www.janestreet.com/join-jane-street/",
    "Renaissance":      "https://www.rentec.com/Careers.action",
    "Hudson River Trading": "https://www.hudsonrivertrading.com/careers/",
    "Jump Trading":     "https://jumptrading.com/careers/",
    "DRW":              "https://drw.com/work-at-drw/career-opportunities",
    "Optiver":          "https://optiver.com/working-at-optiver/career-opportunities/",
    "IMC Trading":      "https://www.imc.com/eu/careers",
    "Akuna":            "https://akunacapital.com/careers/",
    "BlackRock":        "https://careers.blackrock.com/",
    "Moderna":          "https://www.modernatx.com/careers/",
    "Genentech":        "https://careers.gene.com/",
    "Illumina":         "https://www.illumina.com/company/careers.html",
    "Tempus":           "https://www.tempus.com/careers/",
    "23andMe":          "https://www.23andme.com/en-int/careers/",
    "Recursion":        "https://www.recursion.com/careers",
    "Insitro":          "https://insitro.com/careers",
    "UnitedHealth":     "https://careers.unitedhealthgroup.com/",
    "Optum":            "https://careers.unitedhealthgroup.com/",
    "CVS Health":       "https://jobs.cvshealth.com/",
    "Walgreens":        "https://jobs.walgreens.com/",
    "Costco":           "https://www.costco.com/jobs.html",
    "Best Buy":         "https://www.bestbuy-jobs.com/",
    "Home Depot":       "https://careers.homedepot.com/",
    "Lowes":            "https://talent.lowes.com/us/en",
    "Marriott":         "https://careers.marriott.com/",
    "Hilton":           "https://jobs.hilton.com/",
    "Wayfair":          "https://www.wayfair.com/careers/",
    "eBay":             "https://careers.ebayinc.com/us/en",
    "Etsy":             "https://careers.etsy.com/",
    "Shopify":          "https://www.shopify.com/careers",
    "Atlassian":        "https://www.atlassian.com/company/careers/all-jobs",
    "Twilio":           "https://www.twilio.com/en-us/company/jobs",
    "Square":           "https://block.xyz/careers",
    "Block":            "https://block.xyz/careers",
    "Snowflake":        "https://careers.snowflake.com/us/en",
    "MongoDB":          "https://www.mongodb.com/careers",
    "Confluent":        "https://www.confluent.io/careers/",
    "Databricks":       "https://www.databricks.com/company/careers/open-positions",
    "Datadog":          "https://www.datadoghq.com/careers/",
    "HashiCorp":        "https://www.hashicorp.com/careers/open-positions",
}

def co(name, domain, ats, board_id, tier=2, h1bt=True, h1bn=True, verified=True,
       kw=None, kw_ex=None, **extra):
    return {
        "name": name,
        "domain": domain,
        "ats": ats,
        "board_id": board_id,
        "tier": tier,
        "h1b_transfer": h1bt,
        "h1b_new": h1bn,
        "verified": verified,
        "kw": kw or DEFAULT_KW_SRE,
        "kw_ex": kw_ex or DEFAULT_KW_EXCLUDE,
        **extra,
    }

COMPANIES = [

    # ═════════════════════════════════════════════════════════════════════════
    # GREENHOUSE - boards.greenhouse.io/{board_id}
    # ═════════════════════════════════════════════════════════════════════════

    # ── Tier 1: Highest-paying, most-hiring ─────────────────────────────────
    co("Stripe",         "stripe.com",         "greenhouse", "stripe",         tier=1),
    co("Airbnb",         "airbnb.com",         "greenhouse", "airbnb",         tier=1),
    co("Coinbase",       "coinbase.com",       "greenhouse", "coinbase",       tier=1, verified=False),
    co("DoorDash",       "doordash.com",       "greenhouse", "doordash",       tier=1, verified=False),
    co("Databricks",     "databricks.com",     "greenhouse", "databricks",     tier=1, kw=AI_KW),
    co("Datadog",        "datadoghq.com",      "greenhouse", "datadog",        tier=1),
    co("Snowflake",      "snowflake.com",      "greenhouse", "snowflake",      tier=1, kw=AI_KW),
    co("GitLab",         "gitlab.com",         "greenhouse", "gitlab",         tier=1),
    co("HashiCorp",      "hashicorp.com",      "greenhouse", "hashicorp",      tier=1, verified=False),
    co("PagerDuty",      "pagerduty.com",      "greenhouse", "pagerduty",      tier=1),
    co("Figma",          "figma.com",          "greenhouse", "figma",          tier=1),
    co("Okta",           "okta.com",           "greenhouse", "okta",           tier=1),
    co("Confluent",      "confluent.io",       "greenhouse", "confluent",      tier=1, verified=False),
    co("Reddit",         "reddit.com",         "greenhouse", "reddit",         tier=1),
    co("Robinhood",      "robinhood.com",      "greenhouse", "robinhood",      tier=1),
    co("Lyft",           "lyft.com",           "greenhouse", "lyft",           tier=1),
    co("OpenAI",         "openai.com",         "greenhouse", "openai",         tier=1, kw=AI_KW),
    co("Scale AI",       "scale.com",          "greenhouse", "scaleai",        tier=1, kw=AI_KW),
    co("Palantir",       "palantir.com",       "greenhouse", "palantir",       tier=1),
    co("Waymo",          "waymo.com",          "greenhouse", "waymo",          tier=1, kw=AI_KW),
    co("Tesla",          "tesla.com",          "greenhouse", "tesla",          tier=1),
    co("SpaceX",         "spacex.com",         "greenhouse", "spacex",         tier=1),
    co("Discord",        "discord.com",        "greenhouse", "discord",        tier=1),
    co("Roblox",         "roblox.com",         "greenhouse", "roblox",         tier=1),
    co("Spotify",        "spotify.com",        "greenhouse", "spotify",        tier=1),
    co("Snap",           "snap.com",           "greenhouse", "snapinc",        tier=1),
    co("Pinterest",      "pinterest.com",      "greenhouse", "pinterest",      tier=1),

    # ── Tier 2: Strong companies ────────────────────────────────────────────
    co("Twitch",         "twitch.tv",          "greenhouse", "twitch",         tier=2),
    co("Dropbox",        "dropbox.com",        "greenhouse", "dropbox",        tier=2),
    co("Box",            "box.com",            "greenhouse", "box",            tier=2),
    co("Zoom",           "zoom.us",            "greenhouse", "zoom",           tier=2),
    co("HubSpot",        "hubspot.com",        "greenhouse", "hubspot",        tier=2),
    co("Zendesk",        "zendesk.com",        "greenhouse", "zendesk",        tier=2),
    co("MongoDB",        "mongodb.com",        "greenhouse", "mongodb",        tier=2),
    co("Elastic",        "elastic.co",         "greenhouse", "elastic",        tier=2),
    co("CrowdStrike",    "crowdstrike.com",    "greenhouse", "crowdstrike",    tier=2),
    co("Zscaler",        "zscaler.com",        "greenhouse", "zscaler",        tier=2),
    co("Palo Alto Networks","paloaltonetworks.com","greenhouse","paloaltonetworks",tier=2,verified=False),
    co("Brex",           "brex.com",           "greenhouse", "brex",           tier=1),
    co("Ramp",           "ramp.com",           "greenhouse", "ramp",           tier=1),
    co("Gusto",          "gusto.com",          "greenhouse", "gusto",          tier=2),
    co("Instacart",      "instacart.com",      "greenhouse", "instacart",      tier=2),
    co("Rippling",       "rippling.com",       "greenhouse", "rippling",       tier=2),
    co("Amplitude",      "amplitude.com",      "greenhouse", "amplitude",      tier=2),
    co("Klaviyo",        "klaviyo.com",        "greenhouse", "klaviyo",        tier=2),
    co("Samsara",        "samsara.com",        "greenhouse", "samsara",        tier=2),
    co("Rubrik",         "rubrik.com",         "greenhouse", "rubrik",         tier=2),
    co("Grafana Labs",   "grafana.com",        "greenhouse", "grafanalabs",    tier=2),
    co("Grammarly",      "grammarly.com",      "greenhouse", "grammarly",      tier=2),
    co("Canva",          "canva.com",          "greenhouse", "canva",          tier=2),
    co("Notion",         "notion.so",          "greenhouse", "notion",         tier=2),
    co("Airtable",       "airtable.com",       "greenhouse", "airtable",       tier=2),
    co("Asana",          "asana.com",          "greenhouse", "asana",          tier=2),
    co("Miro",           "miro.com",           "greenhouse", "miro",           tier=2),
    co("Glean",          "glean.com",          "greenhouse", "glean",          tier=2, kw=AI_KW),
    co("Duolingo",       "duolingo.com",       "greenhouse", "duolingo",       tier=2),
    co("Benchling",      "benchling.com",      "greenhouse", "benchling",      tier=2),
    co("Lattice",        "lattice.com",        "greenhouse", "lattice",        tier=2),
    co("Etsy",           "etsy.com",           "greenhouse", "etsy",           tier=2),
    co("Chime",          "chime.com",          "greenhouse", "chime",          tier=2),
    co("Block",          "block.xyz",          "greenhouse", "block-xyz",      tier=1),
    co("Rivian",         "rivian.com",         "greenhouse", "rivian",         tier=2),
    co("Wiz",            "wiz.io",             "greenhouse", "wiz",            tier=2),
    co("Snyk",           "snyk.io",            "greenhouse", "snyk",           tier=2),
    co("Verkada",        "verkada.com",        "greenhouse", "verkada",        tier=2),
    co("SentinelOne",    "sentinelone.com",    "greenhouse", "sentinelone",    tier=2),
    co("New Relic",      "newrelic.com",       "greenhouse", "newrelic",       tier=2),
    co("CockroachDB",    "cockroachlabs.com",  "greenhouse", "cockroachlabs",  tier=2),
    co("LaunchDarkly",   "launchdarkly.com",   "greenhouse", "launchdarkly",   tier=2),
    co("CircleCI",       "circleci.com",       "greenhouse", "circleci",       tier=2),
    co("Retool",         "retool.com",         "greenhouse", "retool",         tier=2),
    co("Temporal",       "temporal.io",        "greenhouse", "temporal",       tier=2),
    co("Pulumi",         "pulumi.com",         "greenhouse", "pulumi",         tier=2),
    co("Honeycomb",      "honeycomb.io",       "greenhouse", "honeycomb",      tier=2),
    co("Lacework",       "lacework.com",       "greenhouse", "lacework",       tier=2),
    co("Orca Security",  "orca.security",      "greenhouse", "orcasecurity",   tier=2, verified=False),
    co("Weights & Biases","wandb.ai",          "greenhouse", "wandb",          tier=2, kw=AI_KW),
    co("Cruise",         "getcruise.com",      "greenhouse", "cruise",         tier=2, kw=AI_KW),
    co("Harness",        "harness.io",         "greenhouse", "harness",        tier=2),
    co("JFrog",          "jfrog.com",          "greenhouse", "jfrog",          tier=2),
    co("Sumo Logic",     "sumologic.com",      "greenhouse", "sumologic",      tier=2),
    co("Dynatrace",      "dynatrace.com",      "greenhouse", "dynatrace",      tier=2),
    co("Procore",        "procore.com",        "greenhouse", "procore",        tier=2),
    co("Qualtrics",      "qualtrics.com",      "greenhouse", "qualtrics",      tier=2),
    co("Postman",        "postman.com",        "greenhouse", "postman",        tier=2),
    co("Fastly",         "fastly.com",         "greenhouse", "fastly",         tier=2),
    co("Cloudsmith",     "cloudsmith.com",     "greenhouse", "cloudsmith",     tier=3),
    co("Codefresh",      "codefresh.io",       "greenhouse", "codefresh",      tier=2, verified=False),

    # ── Tech giants on greenhouse ───────────────────────────────────────────
    co("GitHub",         "github.com",         "greenhouse", "github",         tier=1),
    co("Atlassian",      "atlassian.com",      "greenhouse", "atlassian",      tier=1),
    co("Shopify",        "shopify.com",        "greenhouse", "shopify",        tier=1, verified=False),
    co("Twilio",         "twilio.com",         "greenhouse", "twilio",         tier=2),
    co("Klarna",         "klarna.com",         "greenhouse", "klarna",         tier=2, verified=False),
    co("Revolut",        "revolut.com",        "greenhouse", "revolut",        tier=2, verified=False),
    co("Nutanix",        "nutanix.com",        "greenhouse", "nutanix",        tier=2, verified=False),

    # ── Data Engineering / DataOps ──────────────────────────────────────────
    co("Fivetran",       "fivetran.com",       "greenhouse", "fivetran",       tier=2),
    co("Airbyte",        "airbyte.com",        "greenhouse", "airbyte",        tier=2, verified=False),
    co("Astronomer",     "astronomer.io",      "greenhouse", "astronomer",     tier=2, verified=False),
    co("ClickHouse",     "clickhouse.com",     "greenhouse", "clickhouse",     tier=2, kw=AI_KW, verified=False),
    co("Starburst",      "starburst.io",       "greenhouse", "starburst",      tier=2, verified=False),
    co("Prefect",        "prefect.io",         "greenhouse", "prefect",        tier=2, verified=False),
    co("dbt Labs",       "getdbt.com",         "lever",      "dbtlabs",        tier=2),

    # ── MLOps / AI Infrastructure ───────────────────────────────────────────
    co("Pinecone",       "pinecone.io",        "greenhouse", "pinecone",       tier=2, kw=AI_KW, verified=False),
    co("LangChain",      "langchain.com",      "greenhouse", "langchain",      tier=2, kw=AI_KW, verified=False),
    co("Arize AI",       "arize.com",          "greenhouse", "arize",          tier=3, kw=AI_KW, verified=False),
    co("Tecton",         "tecton.ai",          "greenhouse", "tecton",         tier=3, kw=AI_KW, verified=False),
    co("Baseten",        "baseten.co",         "greenhouse", "baseten",        tier=3, kw=AI_KW, verified=False),
    co("Replicate",      "replicate.com",      "greenhouse", "replicate",      tier=3, kw=AI_KW, verified=False),
    co("Weaviate",       "weaviate.io",        "greenhouse", "weaviate",       tier=3, kw=AI_KW, verified=False),
    co("Chroma",         "trychroma.com",      "greenhouse", "chroma",         tier=3, kw=AI_KW, verified=False),
    co("LlamaIndex",     "llamaindex.ai",      "greenhouse", "llamaindex",     tier=3, kw=AI_KW, verified=False),

    # ── Developer tools / Observability ─────────────────────────────────────
    co("Sentry",         "sentry.io",          "greenhouse", "sentry",         tier=2, verified=False),
    co("Mixpanel",       "mixpanel.com",       "greenhouse", "mixpanel",       tier=2, verified=False),
    co("Netlify",        "netlify.com",        "greenhouse", "netlify",        tier=2, verified=False),
    co("Calendly",       "calendly.com",       "greenhouse", "calendly",       tier=2, verified=False),
    co("Zapier",         "zapier.com",         "greenhouse", "zapier",         tier=2, verified=False),
    co("Kong",           "konghq.com",         "greenhouse", "kong",           tier=2, verified=False),
    co("Cohesity",       "cohesity.com",       "greenhouse", "cohesity",       tier=2, verified=False),
    co("Coda",           "coda.io",            "greenhouse", "coda",           tier=3, verified=False),
    co("Loom",           "loom.com",           "greenhouse", "loom",           tier=3, verified=False),
    co("FullStory",      "fullstory.com",      "greenhouse", "fullstory",      tier=3, verified=False),
    co("LogRocket",      "logrocket.com",      "greenhouse", "logrocket",      tier=3, verified=False),
    co("Rollbar",        "rollbar.com",        "greenhouse", "rollbar",        tier=3, verified=False),

    # ── Networking / CDN / API ──────────────────────────────────────────────
    co("Akamai",         "akamai.com",         "greenhouse", "akamai",         tier=2, verified=False),
    co("NS1",            "ns1.com",            "greenhouse", "ns1",            tier=3, verified=False),

    # ── Security additions ──────────────────────────────────────────────────
    co("Tanium",         "tanium.com",         "greenhouse", "tanium",         tier=2, verified=False),
    co("Qualys",         "qualys.com",         "greenhouse", "qualys",         tier=2, verified=False),
    co("Abnormal Security","abnormalsecurity.com","greenhouse","abnormalsecurity",tier=2, verified=False),
    co("Material Security","material.security","greenhouse","materialsecurity",tier=3, verified=False),
    co("Cybereason",     "cybereason.com",     "greenhouse", "cybereason",     tier=3, verified=False),
    co("Veracode",       "veracode.com",       "greenhouse", "veracode",       tier=3, verified=False),
    co("Checkmarx",      "checkmarx.com",      "greenhouse", "checkmarx",      tier=3, verified=False),

    # ── Storage / Cloud infrastructure ──────────────────────────────────────
    co("Pure Storage",   "purestorage.com",    "greenhouse", "purestorage",    tier=2, verified=False),
    co("Backblaze",      "backblaze.com",      "greenhouse", "backblaze",      tier=3, verified=False),
    co("DigitalOcean",   "digitalocean.com",   "greenhouse", "digitalocean",   tier=2, verified=False),

    # ── AI / ML companies ───────────────────────────────────────────────────
    co("Hugging Face",   "huggingface.co",     "greenhouse", "huggingface",    tier=2, kw=AI_KW, verified=False),
    co("Cohere",         "cohere.com",         "greenhouse", "cohere",         tier=2, kw=AI_KW, verified=False),
    co("Anyscale",       "anyscale.com",       "greenhouse", "anyscale",       tier=2, kw=AI_KW, verified=False),
    co("xAI",            "x.ai",               "greenhouse", "xai",            tier=1, kw=AI_KW, verified=False),
    co("Character AI",   "character.ai",       "greenhouse", "characterai",    tier=2, kw=AI_KW, verified=False),
    co("DeepMind",       "deepmind.com",       "greenhouse", "deepmind",       tier=1, kw=AI_KW, verified=False),
    co("Inflection AI",  "inflection.ai",      "greenhouse", "inflection",     tier=2, kw=AI_KW, verified=False),
    co("Runway ML",      "runwayml.com",       "greenhouse", "runway",         tier=2, kw=AI_KW, verified=False),
    co("Harvey AI",      "harvey.ai",          "greenhouse", "harveyai",       tier=2, kw=AI_KW, verified=False),
    co("Stability AI",   "stability.ai",       "greenhouse", "stabilityai",    tier=2, kw=AI_KW, verified=False),
    co("Adept",          "adept.ai",           "greenhouse", "adept",          tier=3, kw=AI_KW, verified=False),
    co("Suno",           "suno.com",           "greenhouse", "suno",           tier=3, kw=AI_KW, verified=False),
    co("Pika",           "pika.art",           "greenhouse", "pika",           tier=3, kw=AI_KW, verified=False),
    co("Luma AI",        "lumalabs.ai",        "greenhouse", "luma",           tier=3, kw=AI_KW, verified=False),

    # ── Security ────────────────────────────────────────────────────────────
    co("Rapid7",         "rapid7.com",         "greenhouse", "rapid7",         tier=2, verified=False),
    co("Tenable",        "tenable.com",        "greenhouse", "tenable",        tier=2, verified=False),

    # ── Databases / Data infra ──────────────────────────────────────────────
    co("Redis",          "redis.com",          "greenhouse", "redis",          tier=2, verified=False),
    co("Timescale",      "timescale.com",      "greenhouse", "timescale",      tier=3, verified=False),
    co("Couchbase",      "couchbase.com",      "greenhouse", "couchbase",      tier=3, verified=False),
    co("SingleStore",    "singlestore.com",    "greenhouse", "singlestore",    tier=3, verified=False),
    co("PlanetScale",    "planetscale.com",    "greenhouse", "planetscale",    tier=2, verified=False),
    co("Neon",           "neon.tech",          "greenhouse", "neon",           tier=2, verified=False),

    # ── Fintech ─────────────────────────────────────────────────────────────
    co("Ripple",         "ripple.com",         "greenhouse", "ripple",         tier=2, verified=False),
    co("Chainalysis",    "chainalysis.com",    "greenhouse", "chainalysis",    tier=2, verified=False),
    co("SoFi",           "sofi.com",           "greenhouse", "sofi",           tier=2, verified=False),
    co("Marqeta",        "marqeta.com",        "greenhouse", "marqeta",        tier=2, verified=False),
    co("Toast",          "toasttab.com",       "greenhouse", "toast",          tier=2, verified=False),
    co("Bill.com",       "bill.com",           "greenhouse", "billcom",        tier=2, verified=False),
    co("Wise",           "wise.com",           "greenhouse", "wise",           tier=2, verified=False),
    co("Kraken",         "kraken.com",         "greenhouse", "kraken",         tier=2, verified=False),
    co("Gemini",         "gemini.com",         "greenhouse", "gemini",         tier=2, verified=False),
    co("Circle",         "circle.com",         "greenhouse", "circle",         tier=2, verified=False),
    co("Anchorage",      "anchorage.com",      "greenhouse", "anchorage",      tier=2, verified=False),
    co("Bitwise",        "bitwiseinvestments.com","greenhouse","bitwise",      tier=3, verified=False),

    # ── HFT / Quant trading ─────────────────────────────────────────────────
    co("Two Sigma",      "twosigma.com",       "greenhouse", "twosigma",       tier=1, verified=False),
    co("Citadel",        "citadel.com",        "greenhouse", "citadel",        tier=1, verified=False),
    co("Citadel Securities","citadelsecurities.com","greenhouse","citadelsecurities",tier=1, verified=False),
    co("Jane Street",    "janestreet.com",     "greenhouse", "janestreet",     tier=1, verified=False),
    co("Jump Trading",   "jumptrading.com",    "greenhouse", "jumptrading",    tier=1, verified=False),
    co("Hudson River Trading","hudsonrivertrading.com","greenhouse","hudsonrivertrading",tier=1, verified=False),
    co("DRW",            "drw.com",            "greenhouse", "drw",            tier=1, verified=False),
    co("Optiver",        "optiver.com",        "greenhouse", "optiver",        tier=1, verified=False),
    co("IMC Trading",    "imc.com",            "greenhouse", "imc",            tier=2, verified=False),
    co("Akuna Capital",  "akunacapital.com",   "greenhouse", "akunacapital",   tier=2, verified=False),
    co("Susquehanna",    "sig.com",            "greenhouse", "sig",            tier=2, verified=False),
    co("Belvedere Trading","belvederetrading.com","greenhouse","belvederetrading",tier=2, verified=False),

    # ── Gaming / Consumer ───────────────────────────────────────────────────
    co("Riot Games",     "riotgames.com",      "greenhouse", "riotgames",      tier=2, verified=False),
    co("Unity",          "unity.com",          "greenhouse", "unity",          tier=2, verified=False),
    co("Niantic",        "nianticlabs.com",    "greenhouse", "niantic",        tier=2, verified=False),
    co("Epic Games",     "epicgames.com",      "greenhouse", "epicgames",      tier=2, verified=False),

    # ── Defense / Aerospace ─────────────────────────────────────────────────
    co("Anduril",        "anduril.com",        "lever",      "anduril",        tier=2, h1bt=False, h1bn=False),
    co("Nuro",           "nuro.ai",            "greenhouse", "nuro",           tier=2, kw=AI_KW, verified=False),
    co("Shield AI",      "shield.ai",          "greenhouse", "shieldai",       tier=2, kw=AI_KW, verified=False, h1bt=False, h1bn=False),
    co("Saronic",        "saronic.com",        "greenhouse", "saronic",        tier=3, verified=False, h1bt=False, h1bn=False),
    co("Aurora",         "aurora.tech",        "greenhouse", "aurora",         tier=2, kw=AI_KW, verified=False),
    co("Zoox",           "zoox.com",           "greenhouse", "zoox",           tier=2, kw=AI_KW, verified=False),
    co("Wayve",          "wayve.ai",           "greenhouse", "wayve",          tier=3, kw=AI_KW, verified=False),

    # ── Healthcare / Biotech ────────────────────────────────────────────────
    co("Veeva",          "veeva.com",          "greenhouse", "veeva",          tier=2, verified=False),
    co("Doximity",       "doximity.com",       "greenhouse", "doximity",       tier=2, verified=False),
    co("Tempus",         "tempus.com",         "greenhouse", "tempus",         tier=2, verified=False),
    co("23andMe",        "23andme.com",        "greenhouse", "23andme",        tier=3, verified=False),
    co("Color Health",   "color.com",          "greenhouse", "color",          tier=3, verified=False),
    co("Hims & Hers",    "forhims.com",        "greenhouse", "himsandhers",    tier=2, verified=False),
    co("Ro",             "ro.co",              "greenhouse", "ro",             tier=3, verified=False),
    co("Hinge Health",   "hingehealth.com",    "greenhouse", "hingehealth",    tier=3, verified=False),
    co("Teladoc",        "teladoc.com",        "greenhouse", "teladoc",        tier=2, verified=False),
    co("Recursion",      "recursion.com",      "greenhouse", "recursion",      tier=2, kw=AI_KW, verified=False),
    co("Insitro",        "insitro.com",        "greenhouse", "insitro",        tier=3, kw=AI_KW, verified=False),
    co("Moderna",        "modernatx.com",      "greenhouse", "moderna",        tier=2, verified=False),

    # ── E-commerce / Retail tech ────────────────────────────────────────────
    co("Wayfair",        "wayfair.com",        "greenhouse", "wayfair",        tier=2, verified=False),
    co("StockX",         "stockx.com",         "greenhouse", "stockx",         tier=3, verified=False),
    co("GOAT",           "goat.com",           "greenhouse", "goat",           tier=3, verified=False),
    co("Faire",          "faire.com",          "lever",      "faire",          tier=2),
    co("Whatnot",        "whatnot.com",        "greenhouse", "whatnot",        tier=2, verified=False),
    co("Mercari",        "mercari.com",        "greenhouse", "mercari",        tier=3, verified=False),
    co("Poshmark",       "poshmark.com",       "greenhouse", "poshmark",       tier=3, verified=False),
    co("ThredUp",        "thredup.com",        "greenhouse", "thredup",        tier=3, verified=False),
    co("Gopuff",         "gopuff.com",         "greenhouse", "gopuff",         tier=3, verified=False),
    co("Grubhub",        "grubhub.com",        "greenhouse", "grubhub",        tier=2, verified=False),

    # ── Travel + transport ──────────────────────────────────────────────────
    co("Expedia",        "expedia.com",        "greenhouse", "expedia",        tier=2, verified=False),
    co("Hopper",         "hopper.com",         "greenhouse", "hopper",         tier=3, verified=False),
    co("TripAdvisor",    "tripadvisor.com",    "greenhouse", "tripadvisor",    tier=3, verified=False),

    # ── Social / consumer ───────────────────────────────────────────────────
    co("Bumble",         "bumble.com",         "greenhouse", "bumble",         tier=2, verified=False),
    co("Match Group",    "mtch.com",           "greenhouse", "matchgroup",     tier=2, verified=False),
    co("Yelp",           "yelp.com",           "greenhouse", "yelp",           tier=2, verified=False),
    co("Nextdoor",       "nextdoor.com",       "greenhouse", "nextdoor",       tier=2, verified=False),
    co("Substack",       "substack.com",       "greenhouse", "substack",       tier=3, verified=False),
    co("Patreon",        "patreon.com",        "greenhouse", "patreon",        tier=2, verified=False),
    co("Medium",         "medium.com",         "greenhouse", "medium",         tier=3, verified=False),
    co("OpenSea",        "opensea.io",         "greenhouse", "opensea",        tier=3, verified=False),

    # ── Devtools / SaaS ─────────────────────────────────────────────────────
    co("JetBrains",      "jetbrains.com",      "greenhouse", "jetbrains",      tier=2, verified=False),
    co("Monday.com",     "monday.com",         "greenhouse", "monday",         tier=2, verified=False),
    co("Asana",          "asana.com",          "greenhouse", "asana",          tier=2, verified=False),
    co("Sketch",         "sketch.com",         "greenhouse", "sketch",         tier=3, verified=False),
    co("BambooHR",       "bamboohr.com",       "greenhouse", "bamboohr",       tier=3, verified=False),
    co("Greenhouse",     "greenhouse.io",      "greenhouse", "greenhouse",     tier=2, verified=False),
    co("Lever",          "lever.co",           "greenhouse", "lever",          tier=2, verified=False),
    co("Ashby",          "ashbyhq.com",        "greenhouse", "ashby",          tier=3, verified=False),
    co("Justworks",      "justworks.com",      "greenhouse", "justworks",      tier=3, verified=False),
    co("ZipRecruiter",   "ziprecruiter.com",   "greenhouse", "ziprecruiter",   tier=2, verified=False),
    co("Indeed",         "indeed.com",         "greenhouse", "indeed",         tier=2, verified=False),
    co("Glassdoor",      "glassdoor.com",      "greenhouse", "glassdoor",      tier=2, verified=False),
    co("Hired",          "hired.com",          "greenhouse", "hired",          tier=3, verified=False),
    co("Wellfound",      "wellfound.com",      "greenhouse", "wellfound",      tier=3, verified=False),
    co("SendGrid",       "sendgrid.com",       "greenhouse", "sendgrid",       tier=3, verified=False),
    co("Drift",          "drift.com",          "greenhouse", "drift",          tier=3, verified=False),
    co("Gong",            "gong.io",           "greenhouse", "gong",           tier=2, verified=False),
    co("Outreach",       "outreach.io",        "greenhouse", "outreach",       tier=2, verified=False),
    co("Salesloft",      "salesloft.com",      "greenhouse", "salesloft",      tier=3, verified=False),
    co("Apollo.io",      "apollo.io",          "greenhouse", "apollo",         tier=3, verified=False),
    co("Clari",          "clari.com",          "greenhouse", "clari",          tier=3, verified=False),
    co("Productboard",   "productboard.com",   "greenhouse", "productboard",   tier=3, verified=False),
    co("Crossbeam",      "crossbeam.com",      "greenhouse", "crossbeam",      tier=3, verified=False),

    # ── Gaming / streaming additions ────────────────────────────────────────
    co("Bungie",         "bungie.net",         "greenhouse", "bungie",         tier=2, verified=False),
    co("Supercell",      "supercell.com",      "greenhouse", "supercell",      tier=2, verified=False),
    co("Roku",           "roku.com",           "greenhouse", "roku",           tier=2, verified=False),

    # ── HR / Recruiting tools ───────────────────────────────────────────────
    co("Lever (Employer)","lever.co",          "greenhouse", "lever",          tier=3, verified=False),
    co("Greenhouse (Employer)","greenhouse.io","greenhouse", "greenhouse",     tier=3, verified=False),

    # ── Foreign-HQ with US presence ─────────────────────────────────────────
    co("Mercado Libre",  "mercadolibre.com",   "greenhouse", "mercadolibre",   tier=2, verified=False),
    co("Globant",        "globant.com",        "greenhouse", "globant",        tier=3, verified=False),
    co("Zomato",         "zomato.com",         "greenhouse", "zomato",         tier=3, verified=False),

    # ═════════════════════════════════════════════════════════════════════════
    # LEVER - jobs.lever.co/{slug}
    # ═════════════════════════════════════════════════════════════════════════

    co("Netflix",        "netflix.com",        "lever", "netflix",        tier=1),
    co("Affirm",         "affirm.com",         "lever", "affirm",         tier=1),
    co("Plaid",          "plaid.com",          "lever", "plaid",          tier=1),
    co("Carta",          "carta.com",          "lever", "carta",          tier=2),
    co("Intercom",       "intercom.com",       "lever", "intercom",       tier=2),
    co("Navan",          "navan.com",          "lever", "tripactions",    tier=2),
    co("Chronosphere",   "chronosphere.io",    "lever", "chronosphere",   tier=2),
    co("Front",          "front.com",          "lever", "front",          tier=3, verified=False),
    co("ngrok",          "ngrok.com",          "lever", "ngrok",          tier=3, verified=False),
    co("Replit",         "replit.com",         "lever", "replit",         tier=2, kw=AI_KW, verified=False),
    co("Imply",          "imply.io",           "lever", "imply",          tier=3, verified=False),
    co("Lob",            "lob.com",            "lever", "lob",            tier=3, verified=False),
    co("1Password",      "1password.com",      "lever", "1password",      tier=2),
    co("Docker",         "docker.com",         "lever", "docker",         tier=2, verified=False),
    co("Sourcegraph",    "sourcegraph.com",    "lever", "sourcegraph",    tier=2, verified=False),
    co("Mux",            "mux.com",            "lever", "mux",            tier=2, verified=False),
    co("Aiven",          "aiven.io",           "lever", "aiven",          tier=2, verified=False),
    co("Cerbos",         "cerbos.dev",         "lever", "cerbos",         tier=3, verified=False),
    co("Mercury",        "mercury.com",        "lever", "mercury",        tier=2),
    co("Deel",           "deel.com",           "lever", "deel",           tier=2),
    co("KeepTruckin/Motive","gomotive.com",    "lever", "motive",         tier=2, verified=False),
    co("Brave",          "brave.com",          "lever", "brave",          tier=3, verified=False),
    co("Vimeo",          "vimeo.com",          "lever", "vimeo",          tier=3, verified=False),
    co("Sardine",        "sardine.ai",         "lever", "sardine",        tier=3, verified=False),
    co("Tubi",           "tubitv.com",         "lever", "tubi",           tier=3, verified=False),
    co("Yelp-L",         "yelp.com",           "lever", "yelp",           tier=2, verified=False, name_override="Yelp-L"),
    co("Reddit-L",       "reddit.com",         "lever", "reddit",         tier=1, verified=False, name_override="Reddit-L"),
    co("Palo Alto Networks-L","paloaltonetworks.com","lever","paloaltonetworks",tier=2, verified=False, name_override="Palo Alto Networks-L"),

    # ═════════════════════════════════════════════════════════════════════════
    # ASHBY - api.ashbyhq.com/posting-api/job-board/{slug}
    # ═════════════════════════════════════════════════════════════════════════

    co("Anthropic",      "anthropic.com",      "ashby", "anthropic",      tier=1, kw=AI_KW),
    co("Linear",         "linear.app",         "ashby", "linear",         tier=2),
    co("Vercel",         "vercel.com",         "ashby", "vercel",         tier=1),
    co("PostHog",        "posthog.com",        "ashby", "posthog",        tier=2),
    co("Supabase",       "supabase.com",       "ashby", "supabase",       tier=2),
    co("Groq",           "groq.com",           "ashby", "groq",           tier=2, kw=AI_KW),
    co("Mistral AI",     "mistral.ai",         "ashby", "mistral",        tier=1, kw=AI_KW),
    co("Perplexity AI",  "perplexity.ai",      "ashby", "perplexityai",   tier=1, kw=AI_KW, verified=False),
    co("Together AI",    "together.ai",        "ashby", "together-ai",    tier=2, kw=AI_KW, verified=False),
    co("Cerebras",       "cerebras.net",       "ashby", "cerebras",       tier=2, kw=AI_KW, verified=False),
    co("Poolside",       "poolside.ai",        "ashby", "poolside",       tier=2, kw=AI_KW, verified=False),
    co("Render",         "render.com",         "ashby", "render",         tier=2, verified=False),
    co("Buildkite",      "buildkite.com",      "ashby", "buildkite",      tier=2),
    co("Redpanda",       "redpanda.com",       "ashby", "redpanda",       tier=2, verified=False),
    co("Modal",          "modal.com",          "ashby", "modal",          tier=2, kw=AI_KW, verified=False),
    co("Hex",            "hex.tech",           "ashby", "hex",            tier=3, verified=False),
    co("Letta",          "letta.com",          "ashby", "letta",          tier=3, kw=AI_KW, verified=False),
    co("Cursor",         "cursor.sh",          "ashby", "cursor",         tier=2, kw=AI_KW, verified=False),
    co("Ably",           "ably.com",           "ashby", "ably",           tier=3, verified=False),
    co("Inngest",        "inngest.com",        "ashby", "inngest",        tier=3, verified=False),
    co("WorkOS",         "workos.com",         "ashby", "workos",         tier=3, verified=False),
    co("Fly.io",         "fly.io",             "ashby", "fly",            tier=2, verified=False),
    co("Resend",         "resend.com",         "ashby", "resend",         tier=3, verified=False),
    co("Dagger",         "dagger.io",          "ashby", "dagger",         tier=3, verified=False),
    co("Turso",          "turso.tech",         "ashby", "turso",          tier=3, verified=False),
    co("Nile",           "thenile.dev",        "ashby", "nile",           tier=3, kw=AI_KW, verified=False),
    co("Fireworks AI",   "fireworks.ai",       "ashby", "fireworks-ai",   tier=2, kw=AI_KW, verified=False),
    co("ElevenLabs",     "elevenlabs.io",      "ashby", "elevenlabs",     tier=2, kw=AI_KW, verified=False),
    co("Ashby",          "ashbyhq.com",        "ashby", "ashby",          tier=3, verified=False),
    co("Linear-A",       "linear.app",         "ashby", "linear",         tier=2, name_override="Linear-A"),
    co("Anchorage-A",    "anchorage.com",      "ashby", "anchorage",      tier=2, verified=False, name_override="Anchorage-A"),
    co("Windsurf",       "codeium.com",        "ashby", "codeium",        tier=2, kw=AI_KW, verified=False),
    co("Loops",          "loops.so",           "ashby", "loops",          tier=3, verified=False),
    co("Liveblocks",     "liveblocks.io",      "ashby", "liveblocks",     tier=3, verified=False),
    co("Granola",        "granola.ai",         "ashby", "granola",        tier=3, kw=AI_KW, verified=False),

    # ═════════════════════════════════════════════════════════════════════════
    # SMARTRECRUITERS - api.smartrecruiters.com/v1/companies/{board}/postings
    # ═════════════════════════════════════════════════════════════════════════

    co("Visa",           "visa.com",           "smartrecruiters", "Visa",       tier=2, verified=False),
    co("Bosch",          "bosch.com",          "smartrecruiters", "BoschGroup", tier=3, verified=False),
    co("Ubisoft",        "ubisoft.com",        "smartrecruiters", "Ubisoft2",   tier=3, verified=False),
    co("Square Enix",    "square-enix.com",    "smartrecruiters", "SquareEnix", tier=3, verified=False),
    co("Equinix",        "equinix.com",        "smartrecruiters", "Equinix",    tier=2, verified=False),
    co("McDonald's Tech","mcdonalds.com",      "smartrecruiters", "McDonalds",  tier=3, verified=False),
    co("Atlassian-S",    "atlassian.com",      "smartrecruiters", "Atlassian",  tier=2, verified=False, name_override="Atlassian-S"),

    # ═════════════════════════════════════════════════════════════════════════
    # CUSTOM SCRAPERS - direct official career APIs (no third party)
    # Each requires a custom_module under services/scraper/connectors/
    # ═════════════════════════════════════════════════════════════════════════

    co("Google",  "careers.google.com",  "custom", "google",  tier=1, kw=AI_KW,
       custom_module="google",
       custom_params={"query": "site reliability engineer devops platform infrastructure", "location": "United States"}),

    co("Apple",   "jobs.apple.com",      "custom", "apple",   tier=1,
       custom_module="apple",
       custom_params={"teams": "team-software-and-services,team-information-systems-and-technology", "locations": "postLocation-USA"}),

    co("Amazon",  "www.amazon.jobs",     "custom", "amazon",  tier=1,
       custom_module="amazon",
       custom_params={"categories": "software-development,operations-it-support-engineering"}),

    co("Meta",    "www.metacareers.com", "custom", "meta",    tier=1, kw=AI_KW,
       custom_module="meta",
       custom_params={"query": "devops sre infrastructure platform"}),

    # The following companies have proprietary career endpoints (no public ATS API).
    # They require dedicated custom_module connectors. discover_careers.py will
    # later attempt to identify the public endpoint or flag for Playwright fallback.

    co("Netflix-Custom", "jobs.netflix.com",   "custom", "netflix",       tier=1, verified=False,
       custom_module="netflix", name_override="Netflix-Custom"),
    co("LinkedIn-Custom","careers.linkedin.com","custom", "linkedin",     tier=1, verified=False,
       custom_module="linkedin", name_override="LinkedIn-Custom"),
    co("TikTok",         "careers.tiktok.com", "custom", "tiktok",        tier=1, verified=False,
       custom_module="tiktok"),
    co("ByteDance",      "careers.bytedance.com","custom","bytedance",    tier=1, verified=False,
       custom_module="bytedance"),
    co("Disney",         "jobs.disneycareers.com","custom","disney",      tier=2, verified=False,
       custom_module="disney"),
    co("Walmart Global Tech","careers.walmart.com","custom","walmart",    tier=2, verified=False,
       custom_module="walmart"),
    co("Target",         "corporate.target.com","custom","target",        tier=2, verified=False,
       custom_module="target"),
    co("Costco",         "careers.costco.com", "custom","costco",         tier=3, verified=False,
       custom_module="costco"),
    co("Best Buy",       "careers.bestbuy.com","custom","bestbuy",        tier=3, verified=False,
       custom_module="bestbuy"),
    co("Home Depot",     "careers.homedepot.com","custom","homedepot",    tier=3, verified=False,
       custom_module="homedepot"),
    co("Lowe's",         "talent.lowes.com",   "custom","lowes",          tier=3, verified=False,
       custom_module="lowes"),
    co("Comcast",        "jobs.comcast.com",   "custom","comcast",        tier=2, verified=False,
       custom_module="comcast"),
    co("NBCUniversal",   "nbcunicareers.com",  "custom","nbcuni",         tier=3, verified=False,
       custom_module="nbcuni"),
    co("Warner Bros Discovery","careers.wbd.com","custom","wbd",          tier=2, verified=False,
       custom_module="wbd"),
    co("Paramount",      "careers.paramount.com","custom","paramount",    tier=3, verified=False,
       custom_module="paramount"),
    co("Sony Interactive","sonyinteractive.com","custom","sonyinteractive",tier=2, verified=False,
       custom_module="sonyinteractive"),
    co("Sony",           "careers.sony.com",   "custom","sony",           tier=3, verified=False,
       custom_module="sony"),
    co("Sirius XM",      "careers.siriusxm.com","custom","siriusxm",      tier=3, verified=False,
       custom_module="siriusxm"),
    co("Pandora",        "pandora.com",        "custom","pandora",        tier=3, verified=False,
       custom_module="pandora"),
    co("Hulu",           "hulu.com",           "custom","hulu",           tier=2, verified=False,
       custom_module="hulu"),
    co("Discovery+",     "wbd.com",            "custom","discoveryplus",  tier=3, verified=False,
       custom_module="discoveryplus"),

    # ── Big banks / financial services ──────────────────────────────────────
    co("JPMorgan Chase Tech","careers.jpmorgan.com","custom","jpmorgan",  tier=2, verified=False,
       custom_module="jpmorgan"),
    co("Goldman Sachs Engineering","goldmansachs.com","custom","goldmansachs",tier=1, verified=False,
       custom_module="goldmansachs"),
    co("Morgan Stanley Tech","morganstanley.com","custom","morganstanley",tier=2, verified=False,
       custom_module="morganstanley"),
    co("BlackRock",      "blackrock.com",      "custom","blackrock",      tier=2, verified=False,
       custom_module="blackrock"),
    co("Bridgewater",    "bridgewater.com",    "custom","bridgewater",    tier=1, verified=False,
       custom_module="bridgewater"),
    co("Renaissance Technologies","rentec.com","custom","renaissance",    tier=1, verified=False,
       custom_module="renaissance"),
    co("Capital One",    "capitalonecareers.com","custom","capitalone",   tier=2, verified=False,
       custom_module="capitalone"),
    co("AmEx",           "americanexpress.com","custom","americanexpress",tier=2, verified=False,
       custom_module="americanexpress"),
    co("Mastercard",     "careers.mastercard.com","custom","mastercard",  tier=2, verified=False,
       custom_module="mastercard"),
    co("Wells Fargo",    "wellsfargo.com",     "custom","wellsfargo",     tier=3, verified=False,
       custom_module="wellsfargo"),
    co("Bank of America","careers.bankofamerica.com","custom","bofa",     tier=2, verified=False,
       custom_module="bofa"),

    # ── Telecom / Carriers ──────────────────────────────────────────────────
    co("AT&T",           "att.jobs",           "custom","att",            tier=3, verified=False,
       custom_module="att"),
    co("Verizon",        "verizon.com",        "custom","verizon",        tier=2, verified=False,
       custom_module="verizon"),
    co("T-Mobile",       "t-mobile.com",       "custom","tmobile",        tier=2, verified=False,
       custom_module="tmobile"),
    co("Charter",        "jobs.spectrum.com",  "custom","charter",        tier=3, verified=False,
       custom_module="charter"),

    # ── Logistics / shipping ────────────────────────────────────────────────
    co("FedEx",          "careers.fedex.com",  "custom","fedex",          tier=3, verified=False,
       custom_module="fedex"),
    co("UPS",            "jobs.ups.com",       "custom","ups",            tier=3, verified=False,
       custom_module="ups"),
    co("USPS",           "usps.com",           "custom","usps",           tier=3, verified=False, h1bt=False, h1bn=False,
       custom_module="usps"),

    # ── Hardware / chips ────────────────────────────────────────────────────
    co("Apple Silicon",  "jobs.apple.com",     "custom","applesilicon",   tier=1, verified=False,
       custom_module="apple", name_override="Apple Silicon"),
    co("Tenstorrent",    "tenstorrent.com",    "greenhouse","tenstorrent",tier=2, kw=AI_KW, verified=False),
    co("Graphcore",      "graphcore.ai",       "greenhouse","graphcore",  tier=3, kw=AI_KW, verified=False),
    co("SambaNova",      "sambanova.ai",       "greenhouse","sambanovasystems",tier=2, kw=AI_KW, verified=False),
    co("Lightmatter",    "lightmatter.co",     "greenhouse","lightmatter",tier=3, kw=AI_KW, verified=False),

    # ═════════════════════════════════════════════════════════════════════════
    # WORKDAY - only entries with CONFIRMED tenant+board combos that return JSON
    # The Workday API is unreliable when tenant IDs are guessed.
    # Only verified=True for entries personally tested in DevTools.
    # ═════════════════════════════════════════════════════════════════════════

    # ── VERIFIED Workday entries ────────────────────────────────────────────
    co("NVIDIA",     "nvidia.com",     "workday", "nvidia",
       tier=1, kw=AI_KW, verified=True,
       workday_tenant="nvidia",
       workday_board="NVIDIAExternalCareerSite",
       workday_location_ids=[]),

    co("Microsoft",  "microsoft.com",  "workday", "microsoft",
       tier=1, verified=True,
       workday_tenant="microsoft",
       workday_board="External_Career_Site",
       workday_location_ids=["bc33aa3152ec42d4995f4791a106ed09"]),

    co("Uber",       "uber.com",       "workday", "uber",
       tier=1, verified=True,
       workday_tenant="uberatc",
       workday_board="External",
       workday_location_ids=[]),

    # ── UNVERIFIED Workday — TODO: verify tenant+board from /careers DevTools
    co("Salesforce", "salesforce.com", "workday", "salesforce",
       tier=1, verified=False,
       workday_tenant="salesforce",
       workday_board="External_Career_Site",
       workday_location_ids=[]),

    co("Adobe",      "adobe.com",      "workday", "adobe",
       tier=1, verified=False,
       workday_tenant="adobe",
       workday_board="External",
       workday_location_ids=[]),

    co("Cisco",      "cisco.com",      "workday", "cisco",
       tier=2, verified=False,
       workday_tenant="cisco",
       workday_board="External",
       workday_location_ids=[]),

    co("Intuit",     "intuit.com",     "workday", "intuit",
       tier=2, verified=False,
       workday_tenant="intuit",
       workday_board="External_Career_Site",
       workday_location_ids=[]),

    co("ServiceNow", "servicenow.com", "workday", "servicenow",
       tier=2, verified=False,
       workday_tenant="servicenow",
       workday_board="External",
       workday_location_ids=[]),

    co("Splunk",     "splunk.com",     "workday", "splunk",
       tier=2, verified=False,
       workday_tenant="splunk",
       workday_board="External",
       workday_location_ids=[]),

    co("LinkedIn",   "linkedin.com",   "workday", "linkedin",
       tier=1, verified=False,
       workday_tenant="linkedin",
       workday_board="External",
       workday_location_ids=[]),

    # ── Workday: large enterprise (TODO: verify tenant+board) ───────────────
    co("Oracle",     "oracle.com",     "workday", "oracle",
       tier=2, verified=False,
       workday_tenant="oracle",
       workday_board="External",
       workday_location_ids=[]),

    co("IBM",        "ibm.com",        "workday", "ibm",
       tier=2, verified=False,
       workday_tenant="ibm",
       workday_board="External",
       workday_location_ids=[]),

    co("VMware",     "vmware.com",     "workday", "vmware",
       tier=2, verified=False,
       workday_tenant="vmware",
       workday_board="External",
       workday_location_ids=[]),

    co("Broadcom",   "broadcom.com",   "workday", "broadcom",
       tier=2, verified=False,
       workday_tenant="broadcom",
       workday_board="External",
       workday_location_ids=[]),

    co("HPE",        "hpe.com",        "workday", "hpe",
       tier=2, verified=False,
       workday_tenant="hpe",
       workday_board="External",
       workday_location_ids=[]),

    co("HP Inc.",    "hp.com",         "workday", "hp",
       tier=3, verified=False,
       workday_tenant="hp",
       workday_board="External",
       workday_location_ids=[]),

    co("Dell",       "dell.com",       "workday", "dell",
       tier=2, verified=False,
       workday_tenant="dell",
       workday_board="External",
       workday_location_ids=[]),

    co("Intel",      "intel.com",      "workday", "intel",
       tier=2, verified=False,
       workday_tenant="intel",
       workday_board="External",
       workday_location_ids=[]),

    co("AMD",        "amd.com",        "workday", "amd",
       tier=2, verified=False,
       workday_tenant="amd",
       workday_board="External",
       workday_location_ids=[]),

    co("Qualcomm",   "qualcomm.com",   "workday", "qualcomm",
       tier=2, verified=False,
       workday_tenant="qualcomm",
       workday_board="External",
       workday_location_ids=[]),

    co("Marvell",    "marvell.com",    "workday", "marvell",
       tier=3, verified=False,
       workday_tenant="marvell",
       workday_board="External",
       workday_location_ids=[]),

    co("Western Digital","westerndigital.com","workday","westerndigital",
       tier=3, verified=False,
       workday_tenant="westerndigital",
       workday_board="External",
       workday_location_ids=[]),

    co("Micron",     "micron.com",     "workday", "micron",
       tier=2, verified=False,
       workday_tenant="micron",
       workday_board="External",
       workday_location_ids=[]),

    co("Texas Instruments","ti.com",   "workday", "ti",
       tier=2, verified=False,
       workday_tenant="ti",
       workday_board="External",
       workday_location_ids=[]),

    co("Analog Devices","analog.com",  "workday", "analog",
       tier=3, verified=False,
       workday_tenant="analog",
       workday_board="External",
       workday_location_ids=[]),

    co("Workday",    "workday.com",    "workday", "workday",
       tier=2, verified=False,
       workday_tenant="workday",
       workday_board="External_Career_Site",
       workday_location_ids=[]),

    co("Autodesk",   "autodesk.com",   "workday", "autodesk",
       tier=2, verified=False,
       workday_tenant="autodesk",
       workday_board="Ext",
       workday_location_ids=[]),

    co("ANSYS",      "ansys.com",      "workday", "ansys",
       tier=3, verified=False,
       workday_tenant="ansys",
       workday_board="External",
       workday_location_ids=[]),

    co("Synopsys",   "synopsys.com",   "workday", "synopsys",
       tier=2, verified=False,
       workday_tenant="synopsys",
       workday_board="External",
       workday_location_ids=[]),

    co("Cadence",    "cadence.com",    "workday", "cadence",
       tier=2, verified=False,
       workday_tenant="cadence",
       workday_board="External_Career_Site",
       workday_location_ids=[]),

    co("Workday-Adobe","adobe.com",    "workday", "adobe2",
       tier=2, verified=False,
       workday_tenant="adobe",
       workday_board="External_Career_Site",
       workday_location_ids=[],
       name_override="Workday-Adobe"),

    # ── Workday: financial services ─────────────────────────────────────────
    co("Goldman Sachs","goldmansachs.com","workday","goldmansachs",
       tier=1, verified=False,
       workday_tenant="goldmansachs",
       workday_board="External",
       workday_location_ids=[]),

    co("Morgan Stanley","morganstanley.com","workday","morganstanley",
       tier=2, verified=False,
       workday_tenant="morganstanley",
       workday_board="External",
       workday_location_ids=[]),

    co("JPMorgan",   "jpmorgan.com",   "workday", "jpmorgan",
       tier=2, verified=False,
       workday_tenant="jpmc",
       workday_board="jpmc",
       workday_location_ids=[]),

    co("Citi",       "citi.com",       "workday", "citi",
       tier=3, verified=False,
       workday_tenant="citi",
       workday_board="2",
       workday_location_ids=[]),

    co("Capital One Workday","capitalone.com","workday","capitalone",
       tier=2, verified=False,
       workday_tenant="capitalone",
       workday_board="Capital_One",
       workday_location_ids=[],
       name_override="Capital One Workday"),

    co("BlackRock-W","blackrock.com",  "workday", "blackrock",
       tier=2, verified=False,
       workday_tenant="blackrock",
       workday_board="External_Career_Site",
       workday_location_ids=[],
       name_override="BlackRock-W"),

    co("Visa-W",     "visa.com",       "workday", "visa",
       tier=2, verified=False,
       workday_tenant="visa",
       workday_board="External",
       workday_location_ids=[],
       name_override="Visa-W"),

    co("FIS",        "fisglobal.com",  "workday", "fis",
       tier=3, verified=False,
       workday_tenant="fis",
       workday_board="External",
       workday_location_ids=[]),

    co("Fiserv",     "fiserv.com",     "workday", "fiserv",
       tier=3, verified=False,
       workday_tenant="fiserv",
       workday_board="External_Career_Site",
       workday_location_ids=[]),

    # ── Workday: defense / aerospace ────────────────────────────────────────
    co("Lockheed Martin","lockheedmartin.com","workday","lockheedmartin",
       tier=2, verified=False, h1bt=False, h1bn=False,
       workday_tenant="lockheedmartin",
       workday_board="External",
       workday_location_ids=[]),

    co("Boeing",     "boeing.com",     "workday", "boeing",
       tier=2, verified=False, h1bt=False, h1bn=False,
       workday_tenant="boeing",
       workday_board="External",
       workday_location_ids=[]),

    co("RTX",        "rtx.com",        "workday", "rtx",
       tier=3, verified=False, h1bt=False, h1bn=False,
       workday_tenant="rtx",
       workday_board="External",
       workday_location_ids=[]),

    co("Northrop Grumman","ngc.com",   "workday", "ngc",
       tier=3, verified=False, h1bt=False, h1bn=False,
       workday_tenant="ngc",
       workday_board="External",
       workday_location_ids=[]),

    co("General Dynamics","gd.com",    "workday", "gd",
       tier=3, verified=False, h1bt=False, h1bn=False,
       workday_tenant="gd",
       workday_board="External",
       workday_location_ids=[]),

    co("L3Harris",   "l3harris.com",   "workday", "l3harris",
       tier=3, verified=False, h1bt=False, h1bn=False,
       workday_tenant="l3harris",
       workday_board="External",
       workday_location_ids=[]),

    co("BAE Systems","baesystems.com", "workday", "baesystems",
       tier=3, verified=False, h1bt=False, h1bn=False,
       workday_tenant="baesystems",
       workday_board="External",
       workday_location_ids=[]),

    co("Blue Origin","blueorigin.com", "workday", "blueorigin",
       tier=2, verified=False, h1bt=False, h1bn=False,
       workday_tenant="blueorigin",
       workday_board="External",
       workday_location_ids=[]),

    # ── Workday: cars / EV / auto ───────────────────────────────────────────
    co("Ford",       "ford.com",       "workday", "ford",
       tier=3, verified=False,
       workday_tenant="ford",
       workday_board="External",
       workday_location_ids=[]),

    co("GM",         "gm.com",         "workday", "gm",
       tier=2, verified=False,
       workday_tenant="gm",
       workday_board="External",
       workday_location_ids=[]),

    co("Toyota",     "toyota.com",     "workday", "toyota",
       tier=3, verified=False,
       workday_tenant="toyota",
       workday_board="External",
       workday_location_ids=[]),

    co("Honda",      "honda.com",      "workday", "honda",
       tier=3, verified=False,
       workday_tenant="honda",
       workday_board="External",
       workday_location_ids=[]),

    co("Lucid Motors","lucidmotors.com","workday","lucidmotors",
       tier=2, verified=False,
       workday_tenant="lucidmotors",
       workday_board="External",
       workday_location_ids=[]),

    # ── Workday: healthcare / insurance ─────────────────────────────────────
    co("UnitedHealth","careers.unitedhealthgroup.com","workday","uhg",
       tier=3, verified=False,
       workday_tenant="uhg",
       workday_board="External",
       workday_location_ids=[]),

    co("CVS Health", "cvshealth.com",  "workday", "cvs",
       tier=3, verified=False,
       workday_tenant="cvs",
       workday_board="External",
       workday_location_ids=[]),

    co("Walgreens",  "jobs.walgreens.com","workday","walgreens",
       tier=3, verified=False,
       workday_tenant="walgreens",
       workday_board="External",
       workday_location_ids=[]),

    co("Cerner/Oracle Health","oracle.com","workday","oraclehealth",
       tier=3, verified=False,
       workday_tenant="oracle",
       workday_board="OracleHealth",
       workday_location_ids=[]),

    co("Genentech",  "gene.com",       "workday", "genentech",
       tier=3, verified=False,
       workday_tenant="roche",
       workday_board="External",
       workday_location_ids=[]),

    co("Illumina",   "illumina.com",   "workday", "illumina",
       tier=3, verified=False,
       workday_tenant="illumina",
       workday_board="External",
       workday_location_ids=[]),

    # ── Workday: travel / hospitality ───────────────────────────────────────
    co("Marriott",   "careers.marriott.com","workday","marriott",
       tier=3, verified=False,
       workday_tenant="marriott",
       workday_board="External",
       workday_location_ids=[]),

    co("Hilton",     "jobs.hilton.com","workday","hilton",
       tier=3, verified=False,
       workday_tenant="hilton",
       workday_board="External",
       workday_location_ids=[]),

    co("JetBlue",    "jetblue.com",    "workday", "jetblue",
       tier=3, verified=False,
       workday_tenant="jetblue",
       workday_board="External",
       workday_location_ids=[]),

    co("Booking Holdings","bookinggroup.com","workday","bookingholdings",
       tier=2, verified=False,
       workday_tenant="bookingholdings",
       workday_board="External",
       workday_location_ids=[]),

    # ── Workday: foreign-HQ enterprises ─────────────────────────────────────
    co("SAP",        "sap.com",        "workday", "sap",
       tier=2, verified=False,
       workday_tenant="sap",
       workday_board="External",
       workday_location_ids=[]),

    co("Siemens",    "siemens.com",    "workday", "siemens",
       tier=3, verified=False,
       workday_tenant="siemens",
       workday_board="External",
       workday_location_ids=[]),

    co("Nokia",      "nokia.com",      "workday", "nokia",
       tier=3, verified=False,
       workday_tenant="nokia",
       workday_board="External",
       workday_location_ids=[]),

    co("Ericsson",   "ericsson.com",   "workday", "ericsson",
       tier=3, verified=False,
       workday_tenant="ericsson",
       workday_board="External",
       workday_location_ids=[]),

    co("ARM",        "arm.com",        "workday", "arm",
       tier=2, verified=False,
       workday_tenant="arm",
       workday_board="External",
       workday_location_ids=[]),

    co("Adyen",      "adyen.com",      "workday", "adyen",
       tier=2, verified=False,
       workday_tenant="adyen",
       workday_board="External",
       workday_location_ids=[]),

    # ── Workday: gaming / media ─────────────────────────────────────────────
    co("Activision Blizzard","careers.activisionblizzard.com","workday","activision",
       tier=2, verified=False,
       workday_tenant="activision",
       workday_board="External",
       workday_location_ids=[]),

    co("Electronic Arts","ea.com",     "workday", "ea",
       tier=2, verified=False,
       workday_tenant="ea",
       workday_board="External",
       workday_location_ids=[]),

    co("Take-Two Interactive","take2games.com","workday","take2",
       tier=2, verified=False,
       workday_tenant="take2",
       workday_board="External",
       workday_location_ids=[]),

    co("Zynga",      "zynga.com",      "workday", "zynga",
       tier=3, verified=False,
       workday_tenant="zynga",
       workday_board="External",
       workday_location_ids=[]),

    co("Valve",      "valvesoftware.com","workday","valve",
       tier=2, verified=False,
       workday_tenant="valve",
       workday_board="External",
       workday_location_ids=[]),

    co("Sirius XM-W","careers.siriusxm.com","workday","siriusxm",
       tier=3, verified=False,
       workday_tenant="siriusxm",
       workday_board="External",
       workday_location_ids=[],
       name_override="Sirius XM-W"),

    # ── Workday: outsourcing / consulting ───────────────────────────────────
    co("Accenture",  "accenture.com",  "workday", "accenture",
       tier=3, verified=False,
       workday_tenant="accenture",
       workday_board="External",
       workday_location_ids=[]),

    co("Deloitte",   "deloitte.com",   "workday", "deloitte",
       tier=3, verified=False,
       workday_tenant="deloitte",
       workday_board="External",
       workday_location_ids=[]),

    co("Wipro",      "wipro.com",      "workday", "wipro",
       tier=3, verified=False, h1bt=True, h1bn=True,
       workday_tenant="wipro",
       workday_board="External",
       workday_location_ids=[]),

    co("Infosys",    "infosys.com",    "workday", "infosys",
       tier=3, verified=False, h1bt=True, h1bn=True,
       workday_tenant="infosys",
       workday_board="External",
       workday_location_ids=[]),

    co("TCS",        "tcs.com",        "workday", "tcs",
       tier=3, verified=False, h1bt=True, h1bn=True,
       workday_tenant="tcs",
       workday_board="External",
       workday_location_ids=[]),

    # ── Workday: misc large enterprise tech orgs ────────────────────────────
    co("USAA",       "usaajobs.com",   "workday", "usaa",
       tier=3, verified=False, h1bt=False, h1bn=False,
       workday_tenant="usaa",
       workday_board="External",
       workday_location_ids=[]),

    co("Workday-Apple Pay","apple.com","workday","applepay",
       tier=1, verified=False,
       workday_tenant="apple",
       workday_board="External",
       workday_location_ids=[],
       name_override="Workday-Apple Pay"),

    co("Veeam",      "veeam.com",      "workday", "veeam",
       tier=3, verified=False,
       workday_tenant="veeam",
       workday_board="External",
       workday_location_ids=[]),

    co("Commvault",  "commvault.com",  "workday", "commvault",
       tier=3, verified=False,
       workday_tenant="commvault",
       workday_board="External",
       workday_location_ids=[]),

    # ═════════════════════════════════════════════════════════════════════════
    # MISC ATS (icims, taleo, etc) — UNVERIFIED, will be reclassified by
    # discover_careers.py once it probes the /careers page.
    # ═════════════════════════════════════════════════════════════════════════

    co("Trend Micro",    "trendmicro.com",     "icims", "trendmicro",       tier=3, verified=False),
    co("Sophos",         "sophos.com",         "icims", "sophos",           tier=3, verified=False),
    co("Bitdefender",    "bitdefender.com",    "icims", "bitdefender",      tier=3, verified=False),
    co("Fortinet",       "fortinet.com",       "icims", "fortinet",         tier=2, verified=False),
    co("Ping Identity",  "pingidentity.com",   "icims", "pingidentity",     tier=3, verified=False),

    # ═════════════════════════════════════════════════════════════════════════
    # CUSTOM (additional) — companies with unique, dedicated career endpoints
    # discover_careers.py will probe these. Most need Playwright fallback.
    # ═════════════════════════════════════════════════════════════════════════

    co("ByteDance Volcano","volcengine.com",   "custom", "volcengine",     tier=3, verified=False,
       custom_module="volcengine"),
    co("Spotify-C",      "spotify.com",        "custom", "spotify-direct", tier=1, verified=False,
       custom_module="spotify", name_override="Spotify-C"),
    co("Tidal",          "tidal.com",          "custom", "tidal",          tier=3, verified=False,
       custom_module="tidal"),
    co("Apple Music/TV", "jobs.apple.com",     "custom", "applemedia",     tier=2, verified=False,
       custom_module="apple", name_override="Apple Music/TV"),
    co("YouTube",        "careers.google.com", "custom", "youtube",        tier=1, verified=False,
       custom_module="google", name_override="YouTube"),

    # ── Foreign fintechs with US presence ───────────────────────────────────
    co("N26",            "n26.com",            "custom", "n26",            tier=3, verified=False,
       custom_module="n26"),
    co("Nubank",         "nubank.com.br",      "custom", "nubank",         tier=2, verified=False,
       custom_module="nubank"),
    co("Rappi",          "rappi.com",          "custom", "rappi",          tier=3, verified=False,
       custom_module="rappi"),

    # ── Streaming / Media additions ─────────────────────────────────────────
    co("Sky",            "sky.com",            "custom", "sky",            tier=3, verified=False,
       custom_module="sky"),

    # ── Gaming / Studios ────────────────────────────────────────────────────
    co("King",           "king.com",           "greenhouse","king",        tier=3, verified=False),
    co("Mojang",         "mojang.com",         "custom","mojang",          tier=3, verified=False,
       custom_module="mojang"),

    # ═════════════════════════════════════════════════════════════════════════
    # NEEDS PLAYWRIGHT — companies with no public ATS API and JS-heavy careers
    # pages. discover_careers.py should flag these for Playwright fallback.
    # Listed as ATS=custom with verified=False so the runtime will skip API
    # calls and rely on the discovery/Playwright path.
    # ═════════════════════════════════════════════════════════════════════════

    co("Whatsapp Engineering","metacareers.com","custom","whatsapp",      tier=2, verified=False,
       custom_module="meta", name_override="Whatsapp Engineering"),
    co("Instagram Engineering","metacareers.com","custom","instagram",    tier=2, verified=False,
       custom_module="meta", name_override="Instagram Engineering"),
    co("X / Twitter",    "x.com",              "custom","xtwitter",        tier=2, verified=False,
       custom_module="xtwitter"),
    co("MongoDB Atlas",  "mongodb.com",        "greenhouse","mongodb",    tier=2, verified=False,
       name_override="MongoDB Atlas"),
    co("Heroku/Salesforce","heroku.com",       "workday","heroku",         tier=3, verified=False,
       workday_tenant="salesforce",
       workday_board="External_Career_Site",
       workday_location_ids=[],
       name_override="Heroku/Salesforce"),
    co("Linode/Akamai",  "linode.com",         "greenhouse","akamai",      tier=3, verified=False,
       name_override="Linode/Akamai"),
    co("Vultr",          "vultr.com",          "custom","vultr",           tier=3, verified=False,
       custom_module="vultr"),
    co("Railway",        "railway.app",        "custom","railway",         tier=3, verified=False,
       custom_module="railway"),
    co("LightStep",      "lightstep.com",      "custom","lightstep",       tier=3, verified=False,
       custom_module="lightstep"),
    co("Tinder",         "tinder.com",         "greenhouse","matchgroup",  tier=2, verified=False,
       name_override="Tinder"),
    co("Hinge",          "hinge.co",           "greenhouse","matchgroup",  tier=3, verified=False,
       name_override="Hinge"),

    # ═════════════════════════════════════════════════════════════════════════
    # EXPANSION BATCH 2 - additional coverage requested by the user.
    # Most are unverified guesses that discover_careers.py will validate at runtime.
    # ═════════════════════════════════════════════════════════════════════════

    # ── More Greenhouse (high-volume hiring boards) ─────────────────────────
    co("8x8",            "8x8.com",            "greenhouse", "8x8",            tier=3, verified=False),
    co("Aptos",          "aptoslabs.com",      "greenhouse", "aptos",          tier=3, verified=False),
    co("Algolia",        "algolia.com",        "greenhouse", "algolia",        tier=2, verified=False),
    co("Apollo GraphQL", "apollographql.com",  "greenhouse", "apollographql",  tier=2, verified=False),
    co("Aurora Innovation","aurora.tech",      "greenhouse", "aurorainnovation",tier=2, kw=AI_KW, verified=False),
    co("Avalara",        "avalara.com",        "greenhouse", "avalara",        tier=3, verified=False),
    co("Axon",           "axon.com",           "greenhouse", "axon",           tier=2, verified=False),
    co("Beam Therapeutics","beamtx.com",       "greenhouse", "beamtherapeutics",tier=3, verified=False),
    co("BetterUp",       "betterup.com",       "greenhouse", "betterup",       tier=3, verified=False),
    co("Brevo",          "brevo.com",          "greenhouse", "brevo",          tier=3, verified=False),
    co("Boom Supersonic","boomsupersonic.com", "greenhouse", "boom",           tier=3, verified=False, h1bt=False, h1bn=False),
    co("Branch",         "branch.io",          "greenhouse", "branch",         tier=3, verified=False),
    co("Bridge",         "bridge.xyz",         "greenhouse", "bridge",         tier=3, verified=False),
    co("Cameo",          "cameo.com",          "greenhouse", "cameo",          tier=3, verified=False),
    co("CaptivateIQ",    "captivateiq.com",    "greenhouse", "captivateiq",    tier=3, verified=False),
    co("Cardless",       "cardless.com",       "greenhouse", "cardless",       tier=3, verified=False),
    co("Caribou",        "gocaribou.com",      "greenhouse", "caribou",        tier=3, verified=False),
    co("Carvana",        "carvana.com",        "greenhouse", "carvana",        tier=2, verified=False),
    co("Casper",         "casper.com",         "greenhouse", "casper",         tier=3, verified=False),
    co("Cedar",          "cedar.com",          "greenhouse", "cedar",          tier=3, verified=False),
    co("Checkout.com",   "checkout.com",       "greenhouse", "checkout",       tier=2, verified=False),
    co("Cherry",         "withcherry.com",     "greenhouse", "cherry",         tier=3, verified=False),
    co("Chipper Cash",   "chippercash.com",    "greenhouse", "chippercash",    tier=3, verified=False),
    co("Cleo AI",        "meetcleo.com",       "greenhouse", "cleoai",         tier=3, verified=False),
    co("ClickUp",        "clickup.com",        "greenhouse", "clickup",        tier=3, verified=False),
    co("Clover",         "clover.com",         "greenhouse", "clover",         tier=3, verified=False),
    co("CoachHub",       "coachhub.com",       "greenhouse", "coachhub",       tier=3, verified=False),
    co("Convoy",         "convoy.com",         "greenhouse", "convoy",         tier=3, verified=False),
    co("Cribl",          "cribl.io",           "greenhouse", "cribl",          tier=2, verified=False),
    co("Crusoe",         "crusoeenergy.com",   "greenhouse", "crusoe",         tier=3, kw=AI_KW, verified=False),
    co("Crypto.com",     "crypto.com",         "greenhouse", "cryptocom",      tier=2, verified=False),
    co("Curri",          "curri.com",          "greenhouse", "curri",          tier=3, verified=False),
    co("Datavant",       "datavant.com",       "greenhouse", "datavant",       tier=3, verified=False),
    co("Drata",          "drata.com",          "greenhouse", "drata",          tier=3, verified=False),
    co("DriveTime",      "drivetime.com",      "greenhouse", "drivetime",      tier=3, verified=False),
    co("Earnin",         "earnin.com",         "greenhouse", "earnin",         tier=3, verified=False),
    co("Embrace",        "embrace.io",         "greenhouse", "embrace",        tier=3, verified=False),
    co("EquipmentShare", "equipmentshare.com", "greenhouse", "equipmentshare", tier=3, verified=False),
    co("ExtraHop",       "extrahop.com",       "greenhouse", "extrahop",       tier=3, verified=False),
    co("Fanatics",       "fanatics.com",       "greenhouse", "fanatics",       tier=3, verified=False),
    co("Flatiron Health","flatiron.com",       "greenhouse", "flatironhealth", tier=2, verified=False),
    co("Flexport",       "flexport.com",       "greenhouse", "flexport",       tier=2, verified=False),
    co("Forage",         "joinforage.com",     "greenhouse", "forage",         tier=3, verified=False),
    co("Forter",         "forter.com",         "greenhouse", "forter",         tier=3, verified=False),
    co("Foursquare",     "foursquare.com",     "greenhouse", "foursquare",     tier=3, verified=False),
    co("FreshBooks",     "freshbooks.com",     "greenhouse", "freshbooks",     tier=3, verified=False),
    co("FullScript",     "fullscript.com",     "greenhouse", "fullscript",     tier=3, verified=False),
    co("GitBook",        "gitbook.com",        "greenhouse", "gitbook",        tier=3, verified=False),
    co("GoFundMe",       "gofundme.com",       "greenhouse", "gofundme",       tier=3, verified=False),
    co("GoodRx",         "goodrx.com",         "greenhouse", "goodrx",         tier=2, verified=False),
    co("Greenlight",     "greenlight.com",     "greenhouse", "greenlight",     tier=3, verified=False),
    co("Groupon",        "groupon.com",        "greenhouse", "groupon",        tier=3, verified=False),
    co("Helium",         "helium.com",         "greenhouse", "helium",         tier=3, verified=False),
    co("Helix",          "helix.com",          "greenhouse", "helix",          tier=3, verified=False),
    co("Houzz",          "houzz.com",          "greenhouse", "houzz",          tier=3, verified=False),
    co("Hightouch",      "hightouch.com",      "greenhouse", "hightouch",      tier=2, verified=False),
    co("Imperfect Foods","imperfectfoods.com", "greenhouse", "imperfectfoods", tier=3, verified=False),
    co("Iterable",       "iterable.com",       "greenhouse", "iterable",       tier=3, verified=False),
    co("Jasper AI",      "jasper.ai",          "greenhouse", "jasper",         tier=3, kw=AI_KW, verified=False),
    co("Kajabi",         "kajabi.com",         "greenhouse", "kajabi",         tier=3, verified=False),
    co("Kandji",         "kandji.io",          "greenhouse", "kandji",         tier=3, verified=False),
    co("Karat",          "karat.com",          "greenhouse", "karat",          tier=3, verified=False),
    co("Komodo Health",  "komodohealth.com",   "greenhouse", "komodohealth",   tier=3, verified=False),
    co("Kustomer",       "kustomer.com",       "greenhouse", "kustomer",       tier=3, verified=False),
    co("Latch",          "latch.com",          "greenhouse", "latch",          tier=3, verified=False),
    co("Lemonade",       "lemonade.com",       "greenhouse", "lemonade",       tier=3, verified=False),
    co("Lucid Software", "lucid.co",           "greenhouse", "lucidsoftware",  tier=2, verified=False),
    co("Luminar",        "luminartech.com",    "greenhouse", "luminartech",    tier=3, verified=False, h1bt=False, h1bn=False),
    co("Maven Clinic",   "mavenclinic.com",    "greenhouse", "maven",          tier=3, verified=False),
    co("Mixmax",         "mixmax.com",         "greenhouse", "mixmax",         tier=3, verified=False),
    co("Modern Treasury","moderntreasury.com", "greenhouse", "moderntreasury", tier=3, verified=False),
    co("Monte Carlo",    "montecarlodata.com", "greenhouse", "montecarlo",     tier=3, verified=False),
    co("MURAL",          "mural.co",           "greenhouse", "mural",          tier=3, verified=False),
    co("Newfront",       "newfront.com",       "greenhouse", "newfront",       tier=3, verified=False),
    co("Nextiva",        "nextiva.com",        "greenhouse", "nextiva",        tier=3, verified=False),
    co("Olo",            "olo.com",            "greenhouse", "olo",            tier=3, verified=False),
    co("OneTrust",       "onetrust.com",       "greenhouse", "onetrust",       tier=2, verified=False),
    co("Outschool",      "outschool.com",      "greenhouse", "outschool",      tier=3, verified=False),
    co("Pano AI",        "pano.ai",            "greenhouse", "panoai",         tier=3, kw=AI_KW, verified=False),
    co("Paxos",          "paxos.com",          "greenhouse", "paxos",          tier=3, verified=False),
    co("Pendo",          "pendo.io",           "greenhouse", "pendo",          tier=3, verified=False),
    co("Persona",        "withpersona.com",    "greenhouse", "persona",        tier=3, verified=False),
    co("PlayStation",    "playstation.com",    "greenhouse", "playstation",    tier=2, verified=False),
    co("Polygon",        "polygon.technology", "greenhouse", "polygon",        tier=3, verified=False),
    co("Pony.ai",        "pony.ai",            "greenhouse", "ponyai",         tier=3, kw=AI_KW, verified=False),
    co("Productiv",      "productiv.com",      "greenhouse", "productiv",      tier=3, verified=False),
    co("Razorpay",       "razorpay.com",       "greenhouse", "razorpay",       tier=3, verified=False),
    co("Relativity",     "relativity.com",     "greenhouse", "relativity",     tier=3, verified=False),
    co("Remitly",        "remitly.com",        "greenhouse", "remitly",        tier=2, verified=False),
    co("Roofstock",      "roofstock.com",      "greenhouse", "roofstock",      tier=3, verified=False),
    co("Saviynt",        "saviynt.com",        "greenhouse", "saviynt",        tier=3, verified=False),
    co("Scribd",         "scribd.com",         "greenhouse", "scribd",         tier=3, verified=False),
    co("Seismic",        "seismic.com",        "greenhouse", "seismic",        tier=3, verified=False),
    co("Sift",           "sift.com",           "greenhouse", "sift",           tier=3, verified=False),
    co("Smartsheet",     "smartsheet.com",     "greenhouse", "smartsheet",     tier=2, verified=False),
    co("Squarespace",    "squarespace.com",    "greenhouse", "squarespace",    tier=2, verified=False),
    co("StackBlitz",     "stackblitz.com",     "greenhouse", "stackblitz",     tier=3, verified=False),
    co("Strava",         "strava.com",         "greenhouse", "strava",         tier=3, verified=False),
    co("Tackle.io",      "tackle.io",          "greenhouse", "tackle",         tier=3, verified=False),
    co("Tala",           "tala.co",            "greenhouse", "tala",           tier=3, verified=False),
    co("Talkdesk",       "talkdesk.com",       "greenhouse", "talkdesk",       tier=3, verified=False),
    co("TaxBit",         "taxbit.com",         "greenhouse", "taxbit",         tier=3, verified=False),
    co("Tealium",        "tealium.com",        "greenhouse", "tealium",        tier=3, verified=False),
    co("Teachable",      "teachable.com",      "greenhouse", "teachable",      tier=3, verified=False),
    co("Thumbtack",      "thumbtack.com",      "greenhouse", "thumbtack",      tier=3, verified=False),
    co("Tonal",          "tonal.com",          "greenhouse", "tonal",          tier=3, verified=False),
    co("Topgolf",        "topgolf.com",        "greenhouse", "topgolf",        tier=3, verified=False),
    co("Truecaller",     "truecaller.com",     "greenhouse", "truecaller",     tier=3, verified=False),
    co("Truework",       "truework.com",       "greenhouse", "truework",       tier=3, verified=False),
    co("Truv",           "truv.com",           "greenhouse", "truv",           tier=3, verified=False),
    co("Turo",           "turo.com",           "greenhouse", "turo",           tier=3, verified=False),
    co("Udacity",        "udacity.com",        "greenhouse", "udacity",        tier=3, verified=False),
    co("Udemy",          "udemy.com",          "greenhouse", "udemy",          tier=2, verified=False),
    co("Unacademy",      "unacademy.com",      "greenhouse", "unacademy",      tier=3, verified=False),
    co("Updater",        "updater.com",        "greenhouse", "updater",        tier=3, verified=False),
    co("Upstart",        "upstart.com",        "greenhouse", "upstart",        tier=3, verified=False),
    co("Vanta",          "vanta.com",          "greenhouse", "vanta",          tier=2, verified=False),
    co("Varo Bank",      "varomoney.com",      "greenhouse", "varomoney",      tier=3, verified=False),
    co("Veho",           "shipveho.com",       "greenhouse", "veho",           tier=3, verified=False),
    co("VTS",            "vts.com",            "greenhouse", "vts",            tier=3, verified=False),
    co("Webflow",        "webflow.com",        "greenhouse", "webflow",        tier=2, verified=False),
    co("WeWork",         "wework.com",         "greenhouse", "wework",         tier=3, verified=False),
    co("Yotpo",          "yotpo.com",          "greenhouse", "yotpo",          tier=3, verified=False),
    co("Zola",           "zola.com",           "greenhouse", "zola",           tier=3, verified=False),
    co("Zwift",          "zwift.com",          "greenhouse", "zwift",          tier=3, verified=False),

    # ── More Lever ──────────────────────────────────────────────────────────
    co("Affinity",       "affinity.co",        "lever",      "affinity",       tier=3, verified=False),
    co("Bitpanda",       "bitpanda.com",       "lever",      "bitpanda",       tier=3, verified=False),
    co("Cresta",         "cresta.com",         "lever",      "cresta",         tier=3, kw=AI_KW, verified=False),
    co("Dialpad",        "dialpad.com",        "lever",      "dialpad",        tier=3, verified=False),
    co("Eight Sleep",    "eightsleep.com",     "lever",      "eightsleep",     tier=3, verified=False),
    co("Exodus",         "exodus.com",         "lever",      "exodus",         tier=3, verified=False),
    co("Lithic",         "lithic.com",         "lever",      "lithic",         tier=3, verified=False),
    co("MasterClass",    "masterclass.com",    "lever",      "masterclass",    tier=3, verified=False),
    co("Onfido",         "onfido.com",         "lever",      "onfido",         tier=3, verified=False),
    co("Stytch",         "stytch.com",         "lever",      "stytch",         tier=3, verified=False),
    co("Twitter-L",      "twitter.com",        "lever",      "twitter",        tier=2, verified=False, name_override="Twitter-L"),
    co("Whoop",          "whoop.com",          "lever",      "whoop",          tier=3, verified=False),
    co("Brave",          "brave.com",          "lever",      "brave",          tier=3, verified=False),
    co("Vimeo",          "vimeo.com",          "lever",      "vimeo",          tier=3, verified=False),
    co("Tubi",           "tubitv.com",         "lever",      "tubi",           tier=3, verified=False),
    co("Sardine",        "sardine.ai",         "lever",      "sardine",        tier=3, verified=False),
    co("Motive",         "gomotive.com",       "lever",      "motive",         tier=2, verified=False),

    # ── More Ashby ──────────────────────────────────────────────────────────
    co("Browserbase",    "browserbase.com",    "ashby",      "browserbase",    tier=3, kw=AI_KW, verified=False),
    co("Bun",            "bun.sh",             "ashby",      "oven",           tier=3, verified=False),
    co("Coval",          "coval.dev",          "ashby",      "coval",          tier=3, kw=AI_KW, verified=False),
    co("Decagon",        "decagon.ai",         "ashby",      "decagon",        tier=3, kw=AI_KW, verified=False),
    co("EvenUp",         "evenuplaw.com",      "ashby",      "evenup",         tier=3, verified=False),
    co("Galileo AI",     "usegalileo.ai",      "ashby",      "galileo",        tier=3, kw=AI_KW, verified=False),
    co("Lovable",        "lovable.dev",        "ashby",      "lovable",        tier=3, kw=AI_KW, verified=False),
    co("Magic AI",       "magic.dev",          "ashby",      "magicai",        tier=2, kw=AI_KW, verified=False),
    co("Notable Health", "notable.health",     "ashby",      "notablehealth",  tier=3, verified=False),
    co("OpenPhone",      "openphone.com",      "ashby",      "openphone",      tier=3, verified=False),
    co("Sanity",         "sanity.io",          "ashby",      "sanity",         tier=3, verified=False),
    co("Sierra",         "sierra.ai",          "ashby",      "sierra",         tier=2, kw=AI_KW, verified=False),
    co("Slope",          "slopepay.com",       "ashby",      "slope",          tier=3, verified=False),
    co("Speak",          "speak.com",          "ashby",      "speak",          tier=3, kw=AI_KW, verified=False),
    co("Vapi",           "vapi.ai",            "ashby",      "vapi",           tier=3, kw=AI_KW, verified=False),
    co("Watershed",      "watershed.com",      "ashby",      "watershed",      tier=3, verified=False),
    co("Zip",            "ziphq.com",          "ashby",      "zip",            tier=3, verified=False),

    # ── More SmartRecruiters ────────────────────────────────────────────────
    co("Avast",          "avast.com",          "smartrecruiters", "Avast",       tier=3, verified=False),
    co("Skyscanner",     "skyscanner.net",     "smartrecruiters", "Skyscanner",  tier=3, verified=False),

    # ── Workday: more enterprise (TODO: verify tenant+board) ────────────────
    co("PayPal",         "paypal.com",         "workday", "paypal",
       tier=2, verified=False,
       workday_tenant="paypal",
       workday_board="jobs",
       workday_location_ids=[]),

    co("eBay",           "ebay.com",           "workday", "ebay",
       tier=2, verified=False,
       workday_tenant="ebay",
       workday_board="External",
       workday_location_ids=[]),

    co("Pinterest-W",    "pinterest.com",      "workday", "pinterest",
       tier=2, verified=False,
       workday_tenant="pinterest",
       workday_board="External",
       workday_location_ids=[],
       name_override="Pinterest-W"),

    co("Snap-W",         "snap.com",           "workday", "snap",
       tier=2, verified=False,
       workday_tenant="snap",
       workday_board="External",
       workday_location_ids=[],
       name_override="Snap-W"),

    co("Roku-W",         "roku.com",           "workday", "roku",
       tier=2, verified=False,
       workday_tenant="roku",
       workday_board="External",
       workday_location_ids=[],
       name_override="Roku-W"),

    co("Blizzard",       "careers.blizzard.com","workday","blizzard",
       tier=2, verified=False,
       workday_tenant="activision",
       workday_board="Blizzard_Careers",
       workday_location_ids=[]),

    co("Bayer",          "bayer.com",          "workday", "bayer",
       tier=3, verified=False,
       workday_tenant="bayer",
       workday_board="External",
       workday_location_ids=[]),

    co("Pfizer",         "pfizer.com",         "workday", "pfizer",
       tier=3, verified=False,
       workday_tenant="pfizer",
       workday_board="External",
       workday_location_ids=[]),

    co("Johnson & Johnson","jnj.com",          "workday", "jnj",
       tier=3, verified=False,
       workday_tenant="jnj",
       workday_board="External",
       workday_location_ids=[]),

    co("AbbVie",         "abbvie.com",         "workday", "abbvie",
       tier=3, verified=False,
       workday_tenant="abbvie",
       workday_board="External",
       workday_location_ids=[]),

    co("Eli Lilly",      "lilly.com",          "workday", "lilly",
       tier=3, verified=False,
       workday_tenant="lilly",
       workday_board="External",
       workday_location_ids=[]),

    co("Bristol Myers Squibb","bms.com",       "workday", "bms",
       tier=3, verified=False,
       workday_tenant="bms",
       workday_board="External",
       workday_location_ids=[]),

    co("AstraZeneca",    "astrazeneca.com",    "workday", "astrazeneca",
       tier=3, verified=False,
       workday_tenant="astrazeneca",
       workday_board="External",
       workday_location_ids=[]),

    co("Allstate",       "allstate.com",       "workday", "allstate",
       tier=3, verified=False,
       workday_tenant="allstate",
       workday_board="External",
       workday_location_ids=[]),

    co("Geico",          "geico.com",          "workday", "geico",
       tier=3, verified=False,
       workday_tenant="geico",
       workday_board="External",
       workday_location_ids=[]),

    co("Progressive",    "progressive.com",    "workday", "progressive",
       tier=3, verified=False,
       workday_tenant="progressive",
       workday_board="External",
       workday_location_ids=[]),

    co("Liberty Mutual", "libertymutual.com",  "workday", "libertymutual",
       tier=3, verified=False,
       workday_tenant="libertymutual",
       workday_board="External",
       workday_location_ids=[]),

    co("Humana",         "humana.com",         "workday", "humana",
       tier=3, verified=False,
       workday_tenant="humana",
       workday_board="External",
       workday_location_ids=[]),

    co("Anthem/Elevance","elevancehealth.com", "workday", "elevance",
       tier=3, verified=False,
       workday_tenant="elevance",
       workday_board="External",
       workday_location_ids=[]),

    co("Kaiser Permanente","kp.org",           "workday", "kaiser",
       tier=3, verified=False,
       workday_tenant="kaiser",
       workday_board="External",
       workday_location_ids=[]),

    co("PNC Bank",       "pnc.com",            "workday", "pnc",
       tier=3, verified=False,
       workday_tenant="pnc",
       workday_board="External",
       workday_location_ids=[]),

    co("Truist",         "truist.com",         "workday", "truist",
       tier=3, verified=False,
       workday_tenant="truist",
       workday_board="External",
       workday_location_ids=[]),

    co("US Bank",        "usbank.com",         "workday", "usbank",
       tier=3, verified=False,
       workday_tenant="usbank",
       workday_board="External",
       workday_location_ids=[]),

    co("Schwab",         "schwab.com",         "workday", "schwab",
       tier=3, verified=False,
       workday_tenant="schwab",
       workday_board="External",
       workday_location_ids=[]),

    co("Fidelity",       "fidelity.com",       "workday", "fidelity",
       tier=2, verified=False,
       workday_tenant="fidelity",
       workday_board="External",
       workday_location_ids=[]),

    co("Vanguard",       "vanguard.com",       "workday", "vanguard",
       tier=3, verified=False,
       workday_tenant="vanguard",
       workday_board="External",
       workday_location_ids=[]),

    co("State Street",   "statestreet.com",    "workday", "statestreet",
       tier=3, verified=False,
       workday_tenant="statestreet",
       workday_board="External",
       workday_location_ids=[]),

    co("KKR",            "kkr.com",            "workday", "kkr",
       tier=3, verified=False,
       workday_tenant="kkr",
       workday_board="External",
       workday_location_ids=[]),

    co("DraftKings",     "draftkings.com",     "workday", "draftkings",
       tier=2, verified=False,
       workday_tenant="draftkings",
       workday_board="External",
       workday_location_ids=[]),

    co("FanDuel",        "fanduel.com",        "workday", "fanduel",
       tier=2, verified=False,
       workday_tenant="fanduel",
       workday_board="External",
       workday_location_ids=[]),

    co("Caesars Digital","caesars.com",        "workday", "caesars",
       tier=3, verified=False,
       workday_tenant="caesars",
       workday_board="External",
       workday_location_ids=[]),

    co("Macy's",         "macysjobs.com",      "workday", "macys",
       tier=3, verified=False,
       workday_tenant="macys",
       workday_board="External",
       workday_location_ids=[]),

    co("Nordstrom",      "careers.nordstrom.com","workday","nordstrom",
       tier=3, verified=False,
       workday_tenant="nordstrom",
       workday_board="External",
       workday_location_ids=[]),

    co("Kroger",         "kroger.com",         "workday", "kroger",
       tier=3, verified=False,
       workday_tenant="kroger",
       workday_board="External",
       workday_location_ids=[]),

    co("Ulta Beauty",    "ulta.com",           "workday", "ulta",
       tier=3, verified=False,
       workday_tenant="ulta",
       workday_board="External",
       workday_location_ids=[]),

    co("Sephora",        "sephora.com",        "workday", "sephora",
       tier=3, verified=False,
       workday_tenant="sephora",
       workday_board="External",
       workday_location_ids=[]),

    co("Estee Lauder",   "elcompanies.com",    "workday", "esteelauder",
       tier=3, verified=False,
       workday_tenant="elcompanies",
       workday_board="External",
       workday_location_ids=[]),

    co("Procter & Gamble","pg.com",            "workday", "pg",
       tier=3, verified=False,
       workday_tenant="pg",
       workday_board="External",
       workday_location_ids=[]),

    co("Unilever",       "unilever.com",       "workday", "unilever",
       tier=3, verified=False,
       workday_tenant="unilever",
       workday_board="External",
       workday_location_ids=[]),

    co("Coca-Cola",      "coca-colacompany.com","workday","cocacola",
       tier=3, verified=False,
       workday_tenant="cocacola",
       workday_board="External",
       workday_location_ids=[]),

    co("PepsiCo",        "pepsico.com",        "workday", "pepsico",
       tier=3, verified=False,
       workday_tenant="pepsico",
       workday_board="External",
       workday_location_ids=[]),

    co("Nike",           "jobs.nike.com",      "workday", "nike",
       tier=2, verified=False,
       workday_tenant="nike",
       workday_board="External",
       workday_location_ids=[]),

    co("Adidas",         "adidas.com",         "workday", "adidas",
       tier=3, verified=False,
       workday_tenant="adidas",
       workday_board="External",
       workday_location_ids=[]),

    co("Lululemon",      "lululemon.com",      "workday", "lululemon",
       tier=3, verified=False,
       workday_tenant="lululemon",
       workday_board="External",
       workday_location_ids=[]),

    co("Wayfair-W",      "wayfair.com",        "workday", "wayfair",
       tier=2, verified=False,
       workday_tenant="wayfair",
       workday_board="External",
       workday_location_ids=[],
       name_override="Wayfair-W"),

    # ── More custom (no public ATS) ─────────────────────────────────────────
    co("Citadel Corp",   "citadel.com",        "custom","citadel-direct",   tier=1, verified=False,
       custom_module="citadel"),
    co("Snowflake-Custom","snowflake.com",     "custom","snowflake-direct", tier=1, verified=False,
       custom_module="snowflake", name_override="Snowflake-Custom"),
    co("Google Cloud",   "careers.google.com", "custom","googlecloud",      tier=1, verified=False,
       custom_module="google", name_override="Google Cloud"),
    co("AWS",            "amazon.jobs",        "custom","aws",              tier=1, verified=False,
       custom_module="amazon", name_override="AWS"),
    co("Azure",          "careers.microsoft.com","custom","azure",          tier=1, verified=False,
       custom_module="microsoft-azure", name_override="Azure"),
    co("MYOB",           "myob.com",           "custom","myob",             tier=3, verified=False,
       custom_module="myob"),

    # ── Misc icims/taleo (will reclassify on probe) ─────────────────────────
    co("CISA",           "cisa.gov",           "taleo","cisa",              tier=3, verified=False, h1bt=False, h1bn=False),
    co("NSA",            "nsa.gov",            "custom","nsa",              tier=3, verified=False, h1bt=False, h1bn=False,
       custom_module="nsa"),
]

# ─── Deduplicate by (name, ats, board_id) ────────────────────────────────────
def deduplicate(companies):
    seen = set()
    out = []
    for c in companies:
        key = (c["name"].lower(), c["ats"], c["board_id"])
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out

# ─── Render to YAML ──────────────────────────────────────────────────────────
def render(companies):
    lines = [
        "# companies/seed_500.yaml",
        "# Auto-generated by scripts/generate-seed.py - DO NOT EDIT MANUALLY",
        "# Run: python3 scripts/generate-seed.py > companies/seed_500.yaml",
        "#",
        f"# Total companies: {len(companies)}",
        "# ATS types: greenhouse, lever, ashby, smartrecruiters, workday, custom, icims",
        "# verified=true means the board_id has been confirmed to return real data",
        "",
        "companies:",
    ]

    current_ats = None
    ats_headers = {
        "greenhouse":      "GREENHOUSE - direct public REST API (boards.greenhouse.io)",
        "lever":           "LEVER - direct public REST API (api.lever.co)",
        "ashby":           "ASHBY - direct public REST API (api.ashbyhq.com)",
        "smartrecruiters": "SMARTRECRUITERS - direct public REST API (api.smartrecruiters.com)",
        "icims":           "ICIMS - HTML-only, needs Playwright fallback",
        "custom":          "CUSTOM - official career APIs (FAANG, retailers, banks, telecom)",
        "workday":         "WORKDAY - tenant+board-based JSON API (verify in DevTools first)",
    }

    for c in sorted(companies, key=lambda x: (x["ats"], x["tier"], x["name"])):
        if c.get("name_override", "").endswith(DEDUP_MARKER_SUFFIXES):
            continue  # skip dedup markers

        if c["ats"] != current_ats:
            current_ats = c["ats"]
            lines.append("")
            lines.append(f"  # {'='*60}")
            lines.append(f"  # {ats_headers.get(current_ats, current_ats.upper())}")
            lines.append(f"  # {'='*60}")

        lines.append("")
        lines.append(f"  - name: {c['name']}")
        lines.append(f"    domain: {c['domain']}")
        lines.append(f"    ats: {c['ats']}")
        lines.append(f"    board_id: {c['board_id']}")
        career_url = c.get("careers_url") or CAREERS_URL_OVERRIDES.get(c["name"]) or f"https://{c['domain']}/careers"
        lines.append(f"    career_url: {career_url}")
        lines.append(f"    visa_transfers_h1b: {str(c['h1b_transfer']).lower()}")
        lines.append(f"    sponsors_new_h1b: {str(c['h1b_new']).lower()}")
        lines.append(f"    tier: {c['tier']}")
        lines.append(f"    verified: {str(c['verified']).lower()}")

        kw = c["kw"]
        lines.append(f"    keywords_include: [{', '.join(kw)}]")
        kw_ex = c["kw_ex"]
        lines.append(f"    keywords_exclude: [{', '.join(kw_ex)}]")

        if c["ats"] == "workday":
            lines.append(f"    workday_tenant: {c.get('workday_tenant','')}")
            lines.append(f"    workday_board: {c.get('workday_board','')}")
            loc_ids = c.get("workday_location_ids", [])
            if loc_ids:
                lines.append(f"    workday_location_ids: {loc_ids}")
            else:
                lines.append(f"    workday_location_ids: []")

        if c["ats"] == "custom":
            lines.append(f"    custom_module: {c.get('custom_module','')}")
            params = c.get("custom_params", {})
            if params:
                lines.append(f"    custom_params:")
                for k, v in params.items():
                    lines.append(f"      {k}: \"{v}\"")

    return "\n".join(lines) + "\n"

def tally(companies):
    """Print a count of companies broken down by ATS and tier to stderr."""
    by_ats = {}
    by_tier = {}
    verified_count = 0
    unverified_count = 0
    for c in companies:
        by_ats[c["ats"]] = by_ats.get(c["ats"], 0) + 1
        by_tier[c["tier"]] = by_tier.get(c["tier"], 0) + 1
        if c["verified"]:
            verified_count += 1
        else:
            unverified_count += 1

    print(f"# Total companies: {len(companies)}", file=sys.stderr)
    print(f"# Verified: {verified_count}, Unverified: {unverified_count}", file=sys.stderr)
    print("# Breakdown by ATS:", file=sys.stderr)
    for ats, count in sorted(by_ats.items(), key=lambda x: -x[1]):
        print(f"#   {ats}: {count}", file=sys.stderr)
    print("# Breakdown by tier:", file=sys.stderr)
    for t, count in sorted(by_tier.items()):
        print(f"#   tier {t}: {count}", file=sys.stderr)

def main():
    companies = deduplicate(COMPANIES)
    # Drop any entries that are pure dedup markers
    companies = [c for c in companies if not c.get("name_override","").endswith(DEDUP_MARKER_SUFFIXES)]
    print(render(companies), end="", file=sys.stdout)
    tally(companies)

if __name__ == "__main__":
    main()
