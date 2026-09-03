FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

WORKDIR /app

COPY worker/requirements.txt worker/requirements.txt
RUN pip install --no-cache-dir -r worker/requirements.txt

COPY src src
COPY worker worker
COPY config.yaml config.yaml

WORKDIR /app/worker
ENV PYTHONPATH=/app/src

EXPOSE 8080

CMD ["gunicorn", "-b", "0.0.0.0:8080", "-w", "1", "-t", "1200", "--threads", "2", "app:app"]
