# ── Stage 1: Build React Frontend ──────────────────────────────────────────
FROM node:20-alpine AS frontend-builder
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci || npm install
COPY . .
RUN npm run build

# ── Stage 2: Production Python Backend + Static File Server ────────────────
FROM python:3.12-slim AS runner
WORKDIR /app

# Copy backend requirements using root context
COPY backend/requirements.txt /app/requirements.txt

# Install dependencies via pre-compiled wheels
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir --no-deps xgboost>=2.0.0

# Copy all backend code into /app
COPY backend /app

# Copy compiled React frontend from Stage 1 into /app/dist
COPY --from=frontend-builder /app/out/renderer /app/dist

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import socket; s=socket.socket(); s.connect(('localhost',8000)); s.close()" || exit 1

CMD ["python", "server.py"]
