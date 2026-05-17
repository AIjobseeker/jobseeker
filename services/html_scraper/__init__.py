"""HTML scraper service for companies without a public ATS API.

Two-tier fetcher (httpx then Playwright) feeding a strict Ollama prompt that
extracts a JSON list of jobs. Output is normalised to the same JobPost shape
the Go scraper publishes on NATS subject jobs.raw.
"""
