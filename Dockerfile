FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir ".[server,browser,memory-vector]" \
 && python -m playwright install --with-deps chromium
# API-only image: `.[server,browser,memory-vector]` + `python main.py`. It does
# not carry the telegram/email surfaces or the crypto/solana rails — use a host
# install (DEPLOYMENT.md) for those. UVICORN_HOST=0.0.0.0 so a plain
# `docker run -p 8000:8000` reaches it (api/server_boot.py defaults to loopback).
ENV UVICORN_HOST=0.0.0.0 UVICORN_PORT=8000 POLYROB_IN_DOCKER=1
EXPOSE 8000
CMD ["python", "main.py"]
