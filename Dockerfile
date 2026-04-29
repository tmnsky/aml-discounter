FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libicu-dev pkg-config gcc g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data && \
    if [ -f seed_data.db ]; then cp seed_data.db /data/sanctions_index.db; fi

ENV DATA_DIR=/data
ENV HOST=0.0.0.0
ENV PORT=8080

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
