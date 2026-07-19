FROM mcr.microsoft.com/playwright/python:v1.58.0-noble

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Seoul \
    COUPON_DATA_DIR=/data \
    COUPON_CONFIG_PATH=/data/browser_coupon_config.json \
    COUPON_ARTIFACT_DIR=/data/browser_artifacts \
    COUPON_BIND_HOST=0.0.0.0 \
    COUPON_PORT=8765

COPY requirements.txt ./
RUN python3 -m pip install --no-cache-dir -r requirements.txt
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fluxbox \
        novnc \
        websockify \
        x11vnc \
        xvfb \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m playwright install chrome

COPY . .
COPY docker/entrypoint.sh /entrypoint.sh
COPY docker/vnc-login.sh /vnc-login.sh
RUN chmod +x /entrypoint.sh /vnc-login.sh && mkdir -p /data/logs /data/browser_artifacts

EXPOSE 8765

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python3", "coupon_webapp.py"]
