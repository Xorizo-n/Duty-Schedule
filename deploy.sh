#!/bin/bash
set -u

echo "🚀 Развертывание Duty Schedule App"
echo "===================================="

DOCKER_IMAGE="koroserg/duty-schedule"
DOCKER_TAG="latest"
COMPOSE_FILE="docker-compose.yml"
WATCHTOWER_ENABLED=true

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

DOCKER_COMPOSE_CMD="docker compose"
if ! $DOCKER_COMPOSE_CMD version >/dev/null 2>&1; then
    if command -v docker-compose >/dev/null 2>&1; then
        DOCKER_COMPOSE_CMD="docker-compose"
        echo -e "${YELLOW}⚠️ Используется устаревшая команда docker-compose${NC}"
    else
        echo -e "${RED}❌ Docker Compose не установлен${NC}"
        echo "Установите Docker Compose: https://docs.docker.com/compose/install/"
        exit 1
    fi
fi

echo -e "${BLUE}🔍 Проверка окружения...${NC}"
if ! command -v docker >/dev/null 2>&1; then
    echo -e "${RED}❌ Docker не установлен${NC}"
    echo "Установите Docker: https://docs.docker.com/get-docker/"
    exit 1
fi
echo -e "${GREEN}✅ Используется команда: $DOCKER_COMPOSE_CMD${NC}"

# Ключ Google и участники VK живут на томе duty_settings (/app/data) и
# загружаются через страницу /settings. Файлы рядом со скриптом — только
# первичное наполнение тома: если он ещё пуст, они копируются туда после старта.
if [ ! -f "credentials.json" ]; then
    echo -e "${YELLOW}⚠️ Файл credentials.json рядом со скриптом не найден${NC}"
    echo "   Ключ сервисного аккаунта можно загрузить после запуска на странице /settings."
    echo ""
    echo -e "${YELLOW}📋 Как получить ключ:${NC}"
    echo "1. Google Cloud Console: https://console.cloud.google.com"
    echo "2. Включите Google Sheets API, создайте сервисный аккаунт"
    echo "3. Сгенерируйте JSON-ключ и откройте таблицу на чтение для e-mail аккаунта"
    echo ""
fi

ensure_env_var() {
    local key="$1"
    local default_value="$2"
    if ! grep -Eq "^${key}=" .env; then
        echo "${key}=${default_value}" >> .env
    fi
}

if [ ! -f ".env" ]; then
    echo ""
    echo -e "${BLUE}🔗 Настройка Google таблицы${NC}"
    echo "Пример URL: https://docs.google.com/spreadsheets/d/ABC123DEF456/edit"
    echo ""

    while true; do
        read -r -p "Введите URL вашей Google таблицы: " google_url
        if [ -z "$google_url" ]; then
            echo -e "${YELLOW}⚠️ URL не может быть пустым${NC}"
            continue
        fi
        if [[ "$google_url" =~ https://docs.google.com/spreadsheets/d/[a-zA-Z0-9_-]+ ]]; then
            break
        fi
        echo -e "${YELLOW}⚠️ Неверный формат URL. Используйте полный URL Google таблицы${NC}"
    done

    cat > .env <<EOF
# Конфигурация Google Sheets
GOOGLE_SHEET_URL=$google_url
DUTY_SHEET_GID=1262048925
DUTY_SHEET_NAME=Новое Дежуство

# Приложение
TZ=Asia/Yekaterinburg
SERVER_TIMEZONE=Asia/Yekaterinburg

# VK бот (заполните при необходимости)
VK_BOT_TOKEN=
VK_PEER_ID=
VK_API_VERSION=5.199

# Логирование
CONSOLE_LOG_LEVEL=INFO
FILE_LOG_LEVEL=WARNING
LOG_DIR=/app/logs

# Метаданные деплоя
DOCKER_IMAGE=$DOCKER_IMAGE
DOCKER_TAG=$DOCKER_TAG
EOF
    echo -e "${GREEN}✅ Файл .env создан${NC}"
else
    echo -e "${GREEN}✅ Файл .env уже существует, проверяю обязательные переменные${NC}"
fi

ensure_env_var "DUTY_SHEET_GID" "1262048925"
ensure_env_var "DUTY_SHEET_NAME" "Новое Дежуство"
ensure_env_var "TZ" "Asia/Yekaterinburg"
ensure_env_var "SERVER_TIMEZONE" "Asia/Yekaterinburg"
ensure_env_var "VK_API_VERSION" "5.199"
ensure_env_var "CONSOLE_LOG_LEVEL" "INFO"
ensure_env_var "FILE_LOG_LEVEL" "WARNING"
ensure_env_var "LOG_DIR" "/app/logs"
ensure_env_var "DOCKER_IMAGE" "$DOCKER_IMAGE"
ensure_env_var "DOCKER_TAG" "$DOCKER_TAG"

if ! grep -Eq "^GOOGLE_SHEET_URL=" .env; then
    echo -e "${RED}❌ В .env отсутствует GOOGLE_SHEET_URL${NC}"
    exit 1
fi

echo -e "${YELLOW}📝 Активные настройки .env:${NC}"
grep -E '^(GOOGLE_SHEET_URL|DUTY_SHEET_GID|DUTY_SHEET_NAME|TZ|SERVER_TIMEZONE|VK_PEER_ID|VK_API_VERSION|VK_COMMANDS_ENABLED|VK_GROUP_ID|SETTINGS_FILE|CONSOLE_LOG_LEVEL|FILE_LOG_LEVEL|LOG_DIR)=' .env
echo ""

if [ ! -f "$COMPOSE_FILE" ]; then
    echo -e "${YELLOW}📄 Создание docker-compose.yml...${NC}"
    cat > "$COMPOSE_FILE" <<'EOF'
services:
  duty-schedule:
    build: .
    image: koroserg/duty-schedule:latest
    container_name: duty-schedule-app
    restart: unless-stopped
    ports:
      - "5000:5000"
    environment:
      - GOOGLE_SHEET_URL=${GOOGLE_SHEET_URL}
      - GOOGLE_CREDENTIALS_FILE=/app/data/credentials.json
      - DUTY_SHEET_GID=${DUTY_SHEET_GID:-1262048925}
      - DUTY_SHEET_NAME=${DUTY_SHEET_NAME:-Новое Дежуство}
      - SERVER_TIMEZONE=Asia/Yekaterinburg
      - VK_BOT_TOKEN=${VK_BOT_TOKEN}
      - VK_PEER_ID=${VK_PEER_ID}
      - VK_API_VERSION=${VK_API_VERSION:-5.199}
      - VK_USERS_FILE=/app/data/vk_users.json
      - VK_COMMANDS_ENABLED=${VK_COMMANDS_ENABLED:-1}
      - VK_GROUP_ID=${VK_GROUP_ID:-}
      - SETTINGS_FILE=${SETTINGS_FILE:-/app/data/settings.json}
      - CONSOLE_LOG_LEVEL=${CONSOLE_LOG_LEVEL:-INFO}
      - FILE_LOG_LEVEL=${FILE_LOG_LEVEL:-WARNING}
      - LOG_DIR=${LOG_DIR:-/app/logs}
      - TZ=Asia/Yekaterinburg
    volumes:
      - duty_logs:/app/logs
      # Настройки из /settings, ключ Google и участники VK. Именованный том,
      # а не bind-mount: файлы пишет сам контейнер под appuser, права хоста
      # тут только мешают, а bind-mount отсутствующего файла Docker молча
      # превращает в каталог. Первичная загрузка — через /settings либо
      # `docker cp` (см. deploy.sh).
      - duty_settings:/app/data
    labels:
      - "com.centurylinklabs.watchtower.enable=true"
      - "com.centurylinklabs.watchtower.scope=duty-schedule"
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:5000/health', timeout=5).read()"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"

  watchtower:
    image: containrrr/watchtower:latest
    container_name: duty-watchtower
    restart: unless-stopped
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    command: --interval 120 --scope duty-schedule --cleanup
    environment:
      - WATCHTOWER_POLL_INTERVAL=120
      - TZ=Asia/Yekaterinburg
      - DOCKER_API_VERSION=1.44
    depends_on:
      - duty-schedule

volumes:
  duty_logs:
  duty_settings:
EOF
    echo -e "${GREEN}✅ Файл $COMPOSE_FILE создан${NC}"
else
    echo -e "${GREEN}✅ Файл $COMPOSE_FILE уже существует${NC}"
fi

if ! $DOCKER_COMPOSE_CMD -f "$COMPOSE_FILE" config -q; then
    echo -e "${RED}❌ Конфигурация docker-compose некорректна${NC}"
    exit 1
fi

echo ""
echo -e "${BLUE}📥 Обновление образа (не критично)...${NC}"
if docker pull "$DOCKER_IMAGE:$DOCKER_TAG" >/dev/null 2>&1; then
    echo -e "${GREEN}✅ Образ $DOCKER_IMAGE:$DOCKER_TAG доступен${NC}"
else
    echo -e "${YELLOW}⚠️ Не удалось скачать образ, будет локальная сборка${NC}"
fi

echo ""
echo -e "${BLUE}🛑 Остановка предыдущих контейнеров...${NC}"
$DOCKER_COMPOSE_CMD down >/dev/null 2>&1 || true

echo ""
echo -e "${BLUE}🚀 Запуск приложения (с пересборкой)...${NC}"
if ! $DOCKER_COMPOSE_CMD up -d --build; then
    echo -e "${RED}❌ Ошибка запуска docker compose${NC}"
    exit 1
fi
echo -e "${GREEN}✅ Контейнеры запущены${NC}"

check_http_ok() {
    local url="$1"
    if command -v curl >/dev/null 2>&1; then
        curl -fsS "$url" >/dev/null 2>&1
        return $?
    fi
    if command -v python3 >/dev/null 2>&1; then
        python3 - "$url" <<'PY' >/dev/null 2>&1
import sys
import urllib.request
urllib.request.urlopen(sys.argv[1], timeout=5).read()
PY
        return $?
    fi
    return 1
}

echo ""
echo -e "${BLUE}⏳ Ожидание готовности приложения (до 60 секунд)...${NC}"
HEALTH_CHECK_TIMEOUT=60
HEALTH_CHECK_INTERVAL=2
READY=false
for ((i=1; i<=HEALTH_CHECK_TIMEOUT/HEALTH_CHECK_INTERVAL; i++)); do
    if check_http_ok "http://localhost:5000/health"; then
        READY=true
        break
    fi
    if [ "$i" -eq 1 ]; then
        echo -n "Прогресс: "
    fi
    echo -n "#"
    sleep "$HEALTH_CHECK_INTERVAL"
done
echo ""

if [ "$READY" = true ]; then
    echo -e "${GREEN}✅ Приложение готово${NC}"
else
    echo -e "${YELLOW}⚠️ Приложение не ответило на /health за ${HEALTH_CHECK_TIMEOUT} сек${NC}"
fi

# Файл с хоста копируется в том только если там его ещё нет: том — источник
# правды, его правят через /settings, и затирать эти правки нельзя.
# `docker cp` вместо `compose cp`: работает и со старым docker-compose v1.
seed_data_file() {
    local name="$1"
    local container="duty-schedule-app"
    if [ ! -f "$name" ]; then
        return
    fi
    if docker exec "$container" test -f "/app/data/$name" 2>/dev/null; then
        echo -e "${GREEN}✅ /app/data/$name уже есть в томе, файл с хоста не трогаем${NC}"
        return
    fi
    if docker cp "$name" "$container:/app/data/$name" \
        && docker exec "$container" chown appuser:appuser "/app/data/$name"; then
        echo -e "${GREEN}✅ $name скопирован в том duty_settings${NC}"
    else
        echo -e "${YELLOW}⚠️ Не удалось скопировать $name в контейнер — загрузите его через /settings${NC}"
    fi
}

echo ""
echo -e "${BLUE}📂 Первичное наполнение тома настроек...${NC}"
seed_data_file "credentials.json"
seed_data_file "vk_users.json"

echo ""
echo -e "${BLUE}🔍 Проверка статуса...${NC}"
if check_http_ok "http://localhost:5000/health"; then
    echo -e "${GREEN}✅ Health check прошел успешно${NC}"
else
    echo -e "${YELLOW}⚠️ Health check не прошел${NC}"
fi

if command -v curl >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1; then
    VERSION_INFO=$(curl -fsS http://localhost:5000/version 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('version',''))" 2>/dev/null || true)
    if [ -n "$VERSION_INFO" ]; then
        echo "Версия приложения: $VERSION_INFO"
    fi
fi

echo ""
echo -e "${YELLOW}📊 Статус контейнеров:${NC}"
$DOCKER_COMPOSE_CMD ps

echo ""
echo -e "${GREEN}🎉 Развертывание завершено${NC}"
echo ""
echo -e "${BLUE}🌐 Приложение доступно по адресу:${NC}"
echo "   http://localhost:5000"
LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
if [ -n "$LOCAL_IP" ] && [ "$LOCAL_IP" != "127.0.0.1" ]; then
    echo "   http://$LOCAL_IP:5000"
fi
echo ""
echo -e "${YELLOW}📋 Команды управления:${NC}"
echo "   $DOCKER_COMPOSE_CMD logs -f duty-schedule   # Логи приложения"
echo "   $DOCKER_COMPOSE_CMD logs -f watchtower      # Логи автообновления"
echo "   $DOCKER_COMPOSE_CMD restart duty-schedule   # Перезапуск приложения"
echo "   $DOCKER_COMPOSE_CMD restart                 # Перезапуск всех сервисов"
echo "   $DOCKER_COMPOSE_CMD down                    # Остановка"
echo "   $DOCKER_COMPOSE_CMD ps                      # Статус контейнеров"
echo ""
echo -e "${BLUE}🔄 Watchtower:${NC}"
if [ "$WATCHTOWER_ENABLED" = true ]; then
    echo "✅ Автоматическое обновление включено"
    echo "   Интервал проверки: 2 минуты"
    echo "   Область действия: только duty-schedule"
else
    echo "❌ Автоматическое обновление отключено"
fi
echo ""
echo -e "${RED}⚠️ Важно:${NC}"
echo "   Не удаляйте: credentials.json, .env, vk_users.json"
echo "   Лист с графиком: 'Новое Дежуство' (gid 1262048925), см. DUTY_SHEET_GID"
