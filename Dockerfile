# Single-stage image — no Node/npm (frontend is server-rendered + a static CSS file).
FROM python:3.12-slim

# uv for dependency management (matches local dev workflow).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /srv
ENV PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=config.settings

# Install dependencies first (cached layer) from the lockfile.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# App code.
COPY . .

# Collect static (admin + whitenoise). A throwaway key is fine — no DB touched.
RUN SECRET_KEY=build DEBUG=0 uv run --no-dev python manage.py collectstatic --noinput

# Run as non-root; /data is the mounted volume for the SQLite DB + backups.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /srv /data
USER appuser

ENV PATH="/srv/.venv/bin:$PATH" \
    DATA_DIR=/data

EXPOSE 8000
# Apply migrations, then serve. No host ports are published (see docker-compose.yml).
CMD ["sh", "-c", "python manage.py migrate --noinput && gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 3"]
