# OPS-19: pinned by digest (the multi-arch index of python:3.12-slim, 2026-10-08).
# Bump deliberately: docker buildx imagetools inspect python:3.12-slim
FROM python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f
WORKDIR /app
COPY . .
# 067 (one install): the first-party packs ship inside polyrob; PACKS picks whose
# SDK extras to bake in — ids (space or comma list), `all`, or empty (none, the
# default). e.g. docker build --build-arg PACKS="x discovery" .
ARG PACKS=""
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/polyrob-browsers
# 066 P1 / D2: every dependency hash-checked against requirements.lock (the packs'
# SDK extras included), then the project --no-deps (pip's hash mode cannot hash a
# directory).
RUN python core/lock_closure.py deps --lock requirements.lock --pyproject pyproject.toml \
      --extras server,browser,memory-vector,docs,media --packs "$PACKS" -o /tmp/deps.txt \
 && pip install --no-cache-dir --require-hashes --prefer-binary -r /tmp/deps.txt \
 && pip install --no-cache-dir --no-deps --no-build-isolation . \
 && rm /tmp/deps.txt \
 && python -m playwright install --with-deps chromium
# API-only image: `.[server,browser,memory-vector,docs,media]` + `python main.py`. It does
# not carry the telegram/email surfaces or the crypto/solana rails — use a host
# install (DEPLOYMENT.md) for those. UVICORN_HOST=0.0.0.0 so a plain
# `docker run -p 127.0.0.1:8000:8000` reaches it through the host's loopback.
ENV UVICORN_HOST=0.0.0.0 UVICORN_PORT=8000 POLYROB_IN_DOCKER=1
# The image runs as `polyrob`, and /app (the code) stays root-owned. The data
# home is the one writable tree, so a plain `docker run` with no env still has a
# place for bot.db and the logs (compose mounts the same path).
ENV POLYROB_DATA_DIR=/app/.polyrob
RUN groupadd --gid 1000 polyrob \
 && useradd --uid 1000 --gid polyrob --create-home polyrob \
 && install -d -m 0700 -o polyrob -g polyrob /app/.polyrob
USER polyrob:polyrob
EXPOSE 8000
CMD ["python", "main.py"]
