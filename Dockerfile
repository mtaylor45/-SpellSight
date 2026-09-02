FROM python:3.11-slim-bookworm

# opencv-python-headless still needs glib; libgomp is used by its threading layer.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libglib2.0-0 libgomp1 v4l-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY wandportal ./wandportal
COPY config.yaml ./config.yaml

ENV PYTHONUNBUFFERED=1 \
    WAND_CONFIG=/config/config.yaml \
    WAND_RECOGNIZER_TEMPLATES_PATH=/data/templates.json

VOLUME ["/data", "/config"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/api/status',timeout=3)" || exit 1

CMD ["python", "-m", "wandportal"]
