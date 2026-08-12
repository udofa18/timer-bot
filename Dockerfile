FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY telegram_timer_bot.py .

# No port needed — this bot uses polling, not webhooks.
CMD ["python", "telegram_timer_bot.py"]
