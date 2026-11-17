FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# Виртуальный экран, VNC, noVNC и утилиты
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11vnc novnc websockify curl ca-certificates procps \
    && rm -rf /var/lib/apt/lists/*

# cloudflared (автоматически под amd64 / arm64)
RUN ARCH=$(dpkg --print-architecture) \
    && curl -fsSL -o /tmp/cloudflared.deb \
       https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH}.deb \
    && dpkg -i /tmp/cloudflared.deb \
    && rm /tmp/cloudflared.deb

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY bot.py .

CMD ["python", "bot.py"]
