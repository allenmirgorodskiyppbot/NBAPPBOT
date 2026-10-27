FROM python:3.12-slim
WORKDIR /app
COPY requirements.lock pyproject.toml ./
RUN python -m pip install --no-cache-dir -r requirements.lock
COPY nbabot ./nbabot
RUN python -m pip install --no-cache-dir --no-deps .
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["python", "-m", "nbabot", "--config", "/app/config.local.json", "serve", "--host", "0.0.0.0"]
