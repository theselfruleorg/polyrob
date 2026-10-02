FROM python:3.12-slim
WORKDIR /app
COPY . .
# 067 (one install): the first-party packs ship inside polyrob; PACKS picks whose
# SDK extras to bake in — ids (space or comma list), `all`, or empty (none, the
# default). e.g. docker build --build-arg PACKS="x discovery" .
ARG PACKS=""
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
# `docker run -p 8000:8000` reaches it (api/server_boot.py defaults to loopback).
ENV UVICORN_HOST=0.0.0.0 UVICORN_PORT=8000 POLYROB_IN_DOCKER=1
EXPOSE 8000
CMD ["python", "main.py"]
