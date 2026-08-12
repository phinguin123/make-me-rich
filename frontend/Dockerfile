# ─────────────────────────────────────────────────────────────────────────────
# Frontend Dockerfile — three build targets:
#   deps         → installs node_modules (shared cache layer)
#   development  → Vite dev server with HMR on port 5173
#   production   → static build, served by Nginx on port 80
# ─────────────────────────────────────────────────────────────────────────────

# ── Shared deps layer (maximise layer cache reuse) ────────────────────────────
FROM node:20-alpine AS deps
WORKDIR /app
# Copy manifests only — npm ci re-runs only when these change.
COPY package.json package-lock.json* ./
RUN npm ci --prefer-offline

# ── Development target ────────────────────────────────────────────────────────
FROM node:20-alpine AS development
WORKDIR /app
# node_modules come from the deps layer; source is bind-mounted at runtime.
COPY --from=deps /app/node_modules ./node_modules
COPY package.json ./
ENV NODE_ENV=development
EXPOSE 5173
# --host 0.0.0.0 makes Vite reachable from outside the container.
CMD ["npx", "vite", "--host", "0.0.0.0", "--port", "5173"]

# ── Builder (production) ─────────────────────────────────────────────────────
FROM node:20-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build          # outputs to /app/dist

# ── Production: Nginx serving the SPA ────────────────────────────────────────
FROM nginx:1.27-alpine AS production
# Remove the default Nginx site.
RUN rm /etc/nginx/conf.d/default.conf
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/dist /usr/share/nginx/html
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
