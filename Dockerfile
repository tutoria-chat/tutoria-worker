FROM python:3.13-slim

WORKDIR /app

# Install system deps needed by pdfplumber (poppler) and psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8001

# Single worker — this service is CPU/memory heavy; scale vertically not horizontally.
CMD python run.py
