# ---- Build stage ----
FROM golang:1.25 AS builder

WORKDIR /src

# Download dependencies first to leverage Docker layer caching
COPY go.mod go.sum ./
RUN go mod download

# Copy the rest of the source and build a static binary.
# Prompts are embedded via go:embed, so the binary is self-contained.
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /bin/sail-researcher .

# ---- Runtime stage ----
FROM gcr.io/distroless/static-debian12:nonroot

COPY --from=builder /bin/sail-researcher /sail-researcher

# ADK REST API is served on 8081 (see config.DefaultPort)
ENV PORT=8081
ENV ENV=production
EXPOSE 8081

ENTRYPOINT ["/sail-researcher"]
