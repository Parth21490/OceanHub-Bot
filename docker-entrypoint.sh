#!/bin/sh
set -e

# Wait for backend service to be healthy
echo "Waiting for backend service to be ready..."
max_attempts=30
attempt=1

while [ $attempt -le $max_attempts ]; do
  if nc -z backend 8080 2>/dev/null; then
    echo "✓ Backend service is ready!"
    break
  fi
  
  echo "[$attempt/$max_attempts] Backend not ready yet... waiting"
  sleep 1
  attempt=$((attempt + 1))
done

if [ $attempt -gt $max_attempts ]; then
  echo "⚠ Warning: Backend service didn't respond after ${max_attempts}s, continuing anyway..."
fi

# Start nginx
echo "Starting Nginx..."
exec nginx -g "daemon off;"
