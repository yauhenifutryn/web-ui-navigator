FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md /app/
COPY src /app/src

RUN pip install --upgrade pip && pip install .

ENV PORT=8080

CMD ["uvicorn", "marketplace_bot.cloud.main:app", "--host", "0.0.0.0", "--port", "8080"]
