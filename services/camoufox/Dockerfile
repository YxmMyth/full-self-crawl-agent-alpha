FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    xvfb \
    wget \
    gnupg2 \
    ca-certificates \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libx11-xcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    xdg-utils \
    --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "camoufox[geoip]"

# Download the custom Camoufox Firefox build (~150MB)
RUN python -m camoufox fetch

COPY server.py /app/server.py
WORKDIR /app

EXPOSE 1234

CMD ["python", "server.py"]
