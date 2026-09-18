FROM python:3.11-slim

WORKDIR /app

# Зависимости бэкенда
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# Бэкенд: приложение, точки входа, конфиг gunicorn
COPY backend/ ./backend/

# Фронтенд: шаблоны и статика (собирать нечего — vanilla JS)
COPY frontend/ ./frontend/

# Секреты (credentials.json, .env, vk_users.json) в образ не попадают:
# они монтируются томами из docker-compose и отсечены в .dockerignore.

# Создаем пользователя для безопасности
# /app/data создаётся в образе, чтобы одноимённый том унаследовал владельца:
# Docker копирует права каталога из образа только при создании нового тома.
RUN useradd -m -u 1000 appuser \
    && mkdir -p /app/logs /app/data \
    && chown -R appuser:appuser /app

EXPOSE 5000

CMD ["python", "backend/container_start.py"]
