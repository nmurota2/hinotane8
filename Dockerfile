FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Tokyo

WORKDIR /app

# cron を入れておく（バッチのスケジュール実行に使う）
RUN apt-get update \
    && apt-get install -y --no-install-recommends cron tzdata ca-certificates \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY deploy/crontab /etc/cron.d/hinotane
RUN chmod 0644 /etc/cron.d/hinotane && crontab /etc/cron.d/hinotane

RUN mkdir -p /app/data /app/logs

CMD ["hinotane", "serve"]
