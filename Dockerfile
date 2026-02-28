# ─── Base image ───────────────────────────────────────────────────────────────
FROM python:3.11-slim-bookworm

# ─── System packages ──────────────────────────────────────────────────────────
# Install Google Chrome stable + all runtime deps in one layer to keep image lean
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget \
        curl \
        gnupg \
        ca-certificates \
        fonts-liberation \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libc6 \
        libcairo2 \
        libcups2 \
        libdbus-1-3 \
        libdrm2 \
        libexpat1 \
        libfontconfig1 \
        libgbm1 \
        libgcc-s1 \
        libglib2.0-0 \
        libgtk-3-0 \
        libnspr4 \
        libnss3 \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        libstdc++6 \
        libvulkan1 \
        libx11-6 \
        libx11-xcb1 \
        libxcb1 \
        libxcomposite1 \
        libxcursor1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxi6 \
        libxrandr2 \
        libxrender1 \
        libxss1 \
        libxtst6 \
        lsb-release \
        xdg-utils \
    && rm -rf /var/lib/apt/lists/*

# ─── Install Google Chrome stable ─────────────────────────────────────────────
RUN wget -q -O /tmp/chrome.deb \
        "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb" \
    && apt-get update \
    && apt-get install -y --no-install-recommends /tmp/chrome.deb \
    && rm /tmp/chrome.deb \
    && rm -rf /var/lib/apt/lists/*

# ─── App directory ────────────────────────────────────────────────────────────
WORKDIR /app

# ─── Python dependencies ──────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ─── Copy source ──────────────────────────────────────────────────────────────
COPY main.py .
# If you have a pre-generated cookies.json, copy it here too:
# COPY cookies.json .

# ─── Environment defaults (override in Railway dashboard) ─────────────────────
ENV HEADLESS=1 \
    PYTHONUNBUFFERED=1

# ─── Writable tmp dir for Chrome profile ──────────────────────────────────────
RUN mkdir -p /tmp/uc_profile && chmod 777 /tmp/uc_profile

# ─── Run ──────────────────────────────────────────────────────────────────────
CMD ["python", "main.py"]
