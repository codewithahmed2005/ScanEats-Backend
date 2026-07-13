FROM python:3.10-slim

WORKDIR /app

# System dependencies mapping for Pillow / QR codes
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Expose port 7860 for Hugging Face network
EXPOSE 7860

CMD ["python", "app.py"]