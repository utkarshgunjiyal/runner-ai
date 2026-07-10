# Multi-stage: build the Vite/React assets, then serve them with nginx.
# The dev server is never used in production; static assets are served by nginx
# with SPA fallback, and /agent/* is proxied to the backend (SSE-safe).

# ---- Stage 1: build ----
FROM node:22-alpine AS build
WORKDIR /app

# Deterministic install from the lockfile.
COPY package.json package-lock.json ./
RUN npm ci

# Build. VITE_BACKEND_URL defaults to "" (same-origin) so the SPA calls /agent/*
# and nginx proxies it — keeping auth cookies same-origin. Override only for a
# cross-origin backend.
ARG VITE_BACKEND_URL=""
ENV VITE_BACKEND_URL=$VITE_BACKEND_URL
COPY . .
RUN npm run build

# ---- Stage 2: serve ----
FROM nginx:1.27-alpine AS runtime

# BACKEND_URL is substituted into the nginx config at container start (runtime
# configuration — no rebuild needed to point at a different backend).
ENV BACKEND_URL=http://backend:8000
COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
# The official nginx image runs envsubst on /etc/nginx/templates/*.template and
# then launches nginx. It drops to the nginx user for worker processes.
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=5 \
  CMD wget -q -O /dev/null http://localhost:80/ || exit 1
