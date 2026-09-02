FROM python:3.11-slim

# opencv-python-headless still needs libGL's stubs for its imgproc bindings.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY wandportal/ ./wandportal/
COPY config.yaml .

# Templates live on a mounted volume so trained spells survive a rebuild.
VOLUME ["/app/data"]
EXPOSE 8080

CMD ["python", "-m", "wandportal", "-c", "config.yaml"]
