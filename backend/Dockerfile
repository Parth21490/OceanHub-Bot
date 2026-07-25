# ─── OceanHub Backend — Dockerfile ───────────────────────────────────────────
# Python 3.12 slim for a lightweight, production-ready image
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies needed by some Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first (layer caching — only re-installs when requirements change)
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir --no-deps xgboost>=2.0.0

# Copy the entire backend source code
COPY . .

# Expose the WebSocket port
EXPOSE 8080

# Health check — verify the process is running
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import socket; s=socket.socket(); s.connect(('localhost',8080)); s.close()" || exit 1

# Start the server
CMD ["python", "server.py"]
