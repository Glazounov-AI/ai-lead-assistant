FROM python:3.14-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ ./bot/
COPY core/ ./core/
COPY services/ ./services/

RUN mkdir -p /app/data

CMD ["python", "-m", "bot.main"]
