# Production self-hosted image: build the web client and run the workflow API
# in one reproducible container. Provider keys are injected at runtime only.
FROM node:22-alpine AS web-build
WORKDIR /build/web
COPY web/package.json web/pnpm-lock.yaml ./
RUN corepack enable && pnpm install --frozen-lockfile --ignore-scripts
COPY web/ ./
RUN pnpm exec vite build

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SECONDHELLO_ENV=production \
    SECONDHELLO_HOST=0.0.0.0 \
    SECONDHELLO_PORT=8765 \
    SECONDHELLO_WEB_ROOT=/app/web/dist
WORKDIR /app
COPY server/requirements.txt /app/server/requirements.txt
RUN pip install --no-cache-dir -r /app/server/requirements.txt
COPY server/ /app/server/
COPY --from=web-build /build/web/dist /app/web/dist
EXPOSE 8765
CMD ["python", "server/asgi.py"]
