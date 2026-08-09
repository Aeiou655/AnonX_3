FROM python:3.13-slim

ARG WITH_DENO=0
ARG WITH_CHROMIUM=0
ARG WITH_FIREFOX=1

RUN groupadd -r AnonX_3 && useradd -r -g AnonX_3 -d /app -s /sbin/nologin AnonX_3

WORKDIR /app

COPY requirements.txt .

RUN apt-get update -y && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        ca-certificates \
        fonts-noto-core \
        fonts-noto-extra \
        fonts-noto-color-emoji \
    && if [ "$WITH_CHROMIUM" = "1" ]; then \
         apt-get install -y --no-install-recommends chromium; \
       fi \
    && if [ "$WITH_FIREFOX" = "1" ]; then \
         apt-get install -y --no-install-recommends firefox-esr; \
       fi \
    && if [ "$WITH_DENO" = "1" ]; then \
         curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh; \
       fi \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --upgrade pip \
    && python3 -m pip install --requirement requirements.txt \
    && python3 -m pip check

COPY . .

RUN mkdir -p /app/firefox-profile \
    && chown -R AnonX_3:AnonX_3 /app

USER AnonX_3

CMD ["bash", "start"]
