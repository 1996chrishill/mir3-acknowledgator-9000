FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DATA_DIR=/data \
    API_PORT=8080

WORKDIR /app

COPY requirements.txt .
# headless-shell only (no full Chromium) to keep the image small
RUN pip install --no-cache-dir -r requirements.txt \
 && playwright install --with-deps --only-shell chromium \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/* /root/.cache

RUN useradd -u 1000 -m app && mkdir -p /data && chown app:app /data

COPY app/ app/

USER app
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('API_PORT','8080'),timeout=5)" || exit 1

CMD ["python", "-m", "app.main"]
