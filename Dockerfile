# One Railway service, one process: uvicorn serves the API and the built React app.

# Build the frontend. vite.config.ts writes the build to ../api/static.
FROM node:24-alpine AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY api/ ./api/
COPY --from=frontend /build/api/static ./api/static

# Apply migrations, then start. sh -c so Railway's $PORT is expanded at runtime.
CMD ["sh", "-c", "alembic -c api/alembic.ini upgrade head && exec uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
