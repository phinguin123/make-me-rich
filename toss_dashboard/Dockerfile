# Multi-arch (works on Oracle A1 / arm64 and x86_64).
FROM node:22-alpine AS ui
WORKDIR /ui
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN VITE_API_BASE= npm run build

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 TRADER_DATA_DIR=/app/backend/data
WORKDIR /app/backend
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./
COPY --from=ui /ui/dist /app/frontend/dist
EXPOSE 5050
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5050/healthz')"
CMD ["python", "app.py"]
