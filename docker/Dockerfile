FROM node:22-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    sqlite3 \
    curl \
    ca-certificates \
    pandoc \
    chromium \
    procps \
    imagemagick \
    file \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g openclaw@2026.4.23 \
    && chown -R node:node /usr/local/lib/node_modules/openclaw

COPY workspace-researcher/skills/fallback/web-reader-pro/requirements.txt /tmp/pipeline-requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/pipeline-requirements.txt \
    && pip3 install --no-cache-dir --break-system-packages \
        trafilatura \
        ddgs \
        duckduckgo-search

# gog binary from build host (static Go binary)
COPY docker/gog-bin/gog /usr/local/bin/gog
RUN chmod +x /usr/local/bin/gog

COPY docker/build-context/ /home/openclaw/.openclaw/

# Seed copy for empty assets volume mount (host openclaw-data/assets overlays image assets).
RUN if [ -d /home/openclaw/.openclaw/assets ]; then \
      cp -a /home/openclaw/.openclaw/assets /home/openclaw/.openclaw-asset-seed; \
    else mkdir -p /home/openclaw/.openclaw-asset-seed; fi

COPY docker/entrypoint.sh /usr/local/bin/openclaw-entrypoint.sh
COPY docker/warm-plugins.sh /usr/local/bin/warm-plugins.sh
RUN chmod +x /usr/local/bin/openclaw-entrypoint.sh /usr/local/bin/warm-plugins.sh

RUN mkdir -p /home/openclaw/.config /home/openclaw/.openclaw/logs \
    && chown -R node:node /home/openclaw

ENV HOME=/home/openclaw \
    TZ=Asia/Kolkata \
    NODE_ENV=production \
    XDG_CONFIG_HOME=/home/openclaw/.config

# Bake bundled plugin runtime deps once at build (avoids reinstall storms at runtime).
RUN su node -s /bin/bash -c "HOME=/home/openclaw XDG_CONFIG_HOME=/home/openclaw/.config /usr/local/bin/warm-plugins.sh"

USER node
WORKDIR /home/openclaw

ENTRYPOINT ["/usr/local/bin/openclaw-entrypoint.sh"]
