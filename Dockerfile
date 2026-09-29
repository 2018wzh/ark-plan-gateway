FROM node:22-alpine AS web
WORKDIR /build/web
COPY web/package*.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.13-slim
WORKDIR /app
RUN useradd -r -u 10001 gateway && mkdir -p /app/data && chown gateway:gateway /app/data
COPY pyproject.toml ./
COPY gateway/ ./gateway/
COPY --from=web /build/gateway/static ./gateway/static/
RUN pip install --no-cache-dir .
USER gateway
EXPOSE 8000
CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
