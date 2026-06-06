FROM python:3.11-slim

WORKDIR /app

# Копируем зависимости
COPY requirements.txt .

# Устанавливаем зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код приложения
COPY container_start.py .
COPY duty_app.py .
COPY wsgi.py .
COPY gunicorn.conf.py .
COPY duty_scheduler/ ./duty_scheduler/
COPY templates/ ./templates/
COPY static/ ./static/

# Создаем пользователя для безопасности
RUN useradd -m -u 1000 appuser \
    && mkdir -p /app/logs \
    && chown -R appuser:appuser /app

EXPOSE 5000

CMD ["python", "container_start.py"]
