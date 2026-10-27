FROM golang:1.26.1-bookworm AS bridge
WORKDIR /build
COPY whatsapp-bridge/go.mod whatsapp-bridge/go.sum ./
RUN go mod download
COPY whatsapp-bridge/ ./
ARG BUILD_VERSION=hosted-dev
RUN CGO_ENABLED=1 go build -trimpath -ldflags="-s -w -X main.buildVersion=${BUILD_VERSION}" -o /whatsapp-bridge .

FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates ffmpeg libsqlite3-0 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10150 whatsapp && useradd --uid 10150 --gid 10150 --no-create-home whatsapp
WORKDIR /app
COPY deploy/requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY --from=bridge /whatsapp-bridge /app/whatsapp-bridge
COPY whatsapp-mcp-server/*.py /app/server/
COPY deploy/entrypoint.py /app/entrypoint.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 WHATSAPP_STORE_DIR=/data \
    WHATSAPP_BRIDGE_URL=http://127.0.0.1:8753/api WHATSAPP_MCP_BIND_HOST=0.0.0.0
USER 10150:10150
ENTRYPOINT ["python", "/app/entrypoint.py"]
