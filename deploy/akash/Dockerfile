FROM python:3.12-slim
WORKDIR /app
COPY requirements.lock pyproject.toml ./
RUN python -m pip install --no-cache-dir -r requirements.lock
COPY config.example.json /app/config.example.json
COPY nbabot ./nbabot
COPY deploy/akash/runtime.py /app/akash-runtime.py
COPY scripts/akash-seed.py /app/akash-seed.py
RUN python -m pip install --no-cache-dir --no-deps .
ENV PYTHONUNBUFFERED=1 NBABOT_CONFIG=/data/state/config.json NBABOT_DEPLOYMENT_KIND=akash
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
  CMD python /app/akash-runtime.py health
CMD ["python", "/app/akash-runtime.py", "run"]
