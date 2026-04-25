FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 build-essential libpq-dev curl \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && apt-get purge -y build-essential libpq-dev \
 && apt-get autoremove -y --purge \
 && rm -rf /var/lib/apt/lists/*
COPY . .
RUN python manage.py collectstatic --noinput || true
RUN useradd -u 1000 -m app && chown -R app:app /app
USER app
EXPOSE 8000
CMD ["gunicorn", "core.wsgi:application", "-b", "0.0.0.0:8000", "-w", "4", "-k", "gthread", "--threads", "4"]
