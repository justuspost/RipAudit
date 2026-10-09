# syntax=docker/dockerfile:1
FROM python:3.13-slim-trixie

ARG VERSION=0.1.0.dev0
ARG REVISION=unknown
LABEL org.opencontainers.image.title="RipAudit" \
      org.opencontainers.image.description="Flags ripped movie files whose runtime does not match an independent reference." \
      org.opencontainers.image.source="https://github.com/justuspost/RipAudit" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"

# ffprobe comes from Debian's ffmpeg package; tini reaps ffprobe child processes and forwards signals.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tini \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp \
    RIPAUDIT_CONFIG_DIR=/config \
    RIPAUDIT_PORT=8080

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . && rm -rf /app/src

# Unraid's default appdata owner is nobody:users (99:100). Override with `docker run --user`.
RUN mkdir -p /config /media && chown 99:100 /config
USER 99:100
VOLUME ["/config"]
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('RIPAUDIT_PORT','8080')+'/healthz', timeout=4).status == 200 else 1)"]

STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "ripaudit"]
