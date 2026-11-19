# Build with the gta-stream-clips project root as context.
FROM python:3.12-slim-bookworm

ARG WITH_WHISPER=0
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    STREAMCUT_DATA=/var/lib/streamcut/data \
    HOME=/var/lib/streamcut/data \
    XDG_CACHE_HOME=/var/lib/streamcut/data/cache
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata ca-certificates fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && if [ "$WITH_WHISPER" = 1 ]; then python -m pip install --no-cache-dir 'faster-whisper==1.2.1' 'opencv-python-headless>=4.8,<5'; else test "$WITH_WHISPER" = 0; fi \
    && groupadd --gid 10001 streamcut \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir "$HOME" streamcut \
    && install -d -o streamcut -g streamcut -m 0750 /var/lib/streamcut

WORKDIR /app
COPY captions.py clips.py editing.py ./
COPY assets/fonts/ ./assets/fonts/
COPY control-room/*.py ./control-room/
COPY control-room/dist/ ./control-room/dist/
COPY --chmod=0755 control-room/deploy/entrypoint.sh /usr/local/bin/start-streamcut

USER 10001:10001
ENTRYPOINT ["/usr/local/bin/start-streamcut"]
