# Build stage
FROM node:22-alpine AS builder

WORKDIR /app

# Copy package files
COPY package.json package-lock.json ./

# Install dependencies
RUN npm ci

# Copy source code
COPY src ./src
COPY public ./public
COPY vite.config.js index.html ./
COPY tailwind.config.js tsconfig.json ./
COPY electron.vite.config.mjs ./

# Build the app (Vite)
RUN npm run build

# Production stage - serve with Nginx
FROM nginx:alpine

# Install netcat for health checks
RUN apk add --no-cache netcat-openbsd

# Copy built renderer files to Nginx
COPY --from=builder /app/out/renderer /usr/share/nginx/html

# Copy Nginx config
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Copy entrypoint script
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

EXPOSE 3000

ENTRYPOINT ["/docker-entrypoint.sh"]
