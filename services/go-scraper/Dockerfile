FROM golang:1.22-alpine AS builder

WORKDIR /build

# Download dependencies first (layer cache)
COPY go.mod go.sum ./
RUN go mod download

# Build
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o /go-scraper .

# ─── Runtime image ─────────────────────────────────────────────────────────────
FROM gcr.io/distroless/static-debian12:nonroot

COPY --from=builder /go-scraper /go-scraper

# companies/ is mounted at /app/companies via docker-compose volume so seed
# edits don't require an image rebuild.

ENTRYPOINT ["/go-scraper"]
CMD ["--mode", "nats", "--seed", "/app/companies/seed_500.yaml"]
