# ─── OceanHub Railway Production Dockerfile ──────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# Copy backend requirements using root context
COPY backend/requirements.txt /app/requirements.txt

# Install dependencies via pre-compiled wheels
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir --no-deps xgboost>=2.0.0

# Copy all backend code into /app
COPY backend /app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import socket; s=socket.socket(); s.connect(('localhost',8080)); s.close()" || exit 1

CMD ["python", "server.py"]
