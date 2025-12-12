FROM python:3.9-slim as builder

WORKDIR /app

# Копируем зависимости отдельно для кэширования
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# Финальный образ
FROM python:3.9-slim

WORKDIR /app

# Копируем зависимости из builder
COPY --from=builder /root/.local /root/.local

# Копируем код приложения
COPY duty_app.py .
COPY templates/ ./templates/
COPY static/ ./static/
COPY logrotate.conf /etc/logrotate.d/duty-app

# Создаем пользователя
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app && \
    mkdir -p /var/log/duty-app && \
    chown -R appuser:appuser /var/log/duty-app

USER appuser

ENV PATH=/root/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    FLASK_DEBUG=0

ENTRYPOINT ["python", "duty_app.py"]