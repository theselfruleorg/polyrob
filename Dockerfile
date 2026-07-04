FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir ".[server,browser,memory-vector]" \
 && python -m playwright install --with-deps chromium
ENV UVICORN_PORT=8000
EXPOSE 8000
CMD ["python", "main.py"]
