FROM python:3.13-slim

LABEL maintainer="TrackMe Limited <support@trackme-solutions.com>"
LABEL description="ML Gen — Synthetic metrics generator for TrackMe ML Outlier Detection testing"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ml_gen.py .
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# Run as non-root user
RUN useradd -m -r appuser
USER appuser

# Unbuffered output for real-time logging in Docker
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["/app/entrypoint.sh"]
