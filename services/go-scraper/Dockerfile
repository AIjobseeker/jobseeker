FROM docker-upstream.apple.com/library/golang:1.22-alpine AS builder

WORKDIR /build

# Download dependencies first (layer cache)
COPY go.mod go.sum ./
RUN go mod download

# Build
COPY . .
# go mod tidy ensures go.sum has every transitive dep — protects against
# the local go.sum being incomplete (which can happen when `go mod tidy`
# is run on a network-restricted machine and silently fails).
RUN go mod tidy
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o /go-scraper .

# ─── Runtime image ─────────────────────────────────────────────────────────────
FROM gcr.io/distroless/static-debian12:nonroot

COPY --from=builder /go-scraper /go-scraper

# companies/ is mounted at /app/companies via docker-compose volume so seed
# edits don't require an image rebuild.

ENTRYPOINT ["/go-scraper"]
CMD ["--mode", "nats", "--seed", "/app/companies/seed_500.yaml"]
