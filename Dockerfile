FROM python:3.12-slim

# pdftotext (poppler) — απαραίτητο για την ανάλυση των PDF
RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# cache (λεξικό/geocode) σε εφήμερο /tmp· τα run μένουν στο R2 (βλ. env)
ENV DEMOLITIONS_CACHE_DIR=/tmp/demolitions-cache

# Render/hosted ορίζει $PORT· ένας worker (single-job model) + threads για το
# polling και το streaming zip· timeout 0 ώστε να μην κόβεται το long download
CMD ["sh", "-c", "gunicorn -w 1 --threads 8 --timeout 0 -b 0.0.0.0:${PORT:-8000} webui:app"]
