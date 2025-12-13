#!/bin/bash

echo "🚀 Развертывание Duty Schedule App"
echo "===================================="

# Конфигурация
DOCKER_IMAGE="koroserg/duty-schedule"
DOCKER_TAG="latest"
COMPOSE_FILE="docker-compose.yml"
WATCHTOWER_ENABLED=true

# Цвета для вывода
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Определяем команду docker compose
DOCKER_COMPOSE_CMD="docker compose"
if ! $DOCKER_COMPOSE_CMD version > /dev/null 2>&1; then
    # Пробуем устаревшую версию как fallback
    if command -v docker-compose > /dev/null 2>&1; then
        DOCKER_COMPOSE_CMD="docker-compose"
        echo -e "${YELLOW}⚠️  Используется устаревшая команда docker-compose${NC}"
    else
        echo -e "${RED}❌ Docker Compose не установлен${NC}"
        echo "Установите Docker Compose: https://docs.docker.com/compose/install/"
        exit 1
    fi
fi

# Проверка Docker
echo -e "${BLUE}🔍 Проверка окружения...${NC}"
if ! command -v docker &> /dev/null; then
    echo -e "${RED}❌ Docker не установлен${NC}"
    echo "Установите Docker: https://docs.docker.com/get-docker/"
    exit 1
fi

echo -e "${GREEN}✅ Используется команда: $DOCKER_COMPOSE_CMD${NC}"

# Проверка файлов
if [ ! -f "credentials.json" ]; then
    echo -e "${RED}❌ Файл credentials.json не найден${NC}"
    echo ""
    echo -e "${YELLOW}📋 Инструкция по настройке Google Sheets API:${NC}"
    echo "1. Перейдите в Google Cloud Console: https://console.cloud.google.com"
    echo "2. Создайте новый проект или выберите существующий"
    echo "3. Включите Google Sheets API"
    echo "4. Создайте сервисный аккаунт"
    echo "5. Сгенерируйте JSON ключи"
    echo "6. Переименуйте скачанный файл в credentials.json"
    echo "7. Скопируйте его в эту папку"
    echo ""
    echo -e "${YELLOW}💡 Важно:${NC}"
    echo "- Предоставьте доступ к таблице для email сервисного аккаунта"
    echo "- Лист в таблице должен называться 'Вечернее дежурство'"
    exit 1
fi

# Запрос URL таблицы
if [ ! -f ".env" ]; then
    echo ""
    echo -e "${BLUE}🔗 Настройка Google таблицы${NC}"
    echo "Пример URL: https://docs.google.com/spreadsheets/d/ABC123DEF456/edit"
    echo ""
    echo -e "${YELLOW}⚠️  Важно:${NC}"
    echo "- Лист должен называться 'Вечернее дежурство'"
    echo "- Предоставьте доступ к таблице для сервисного аккаунта"
    echo ""
    
    while true; do
        read -p "Введите URL вашей Google таблицы: " google_url
        
        if [ -z "$google_url" ]; then
            echo -e "${YELLOW}⚠️  URL не может быть пустым${NC}"
            continue
        fi
        
        # Простая валидация URL
        if [[ "$google_url" =~ https://docs.google.com/spreadsheets/d/[a-zA-Z0-9_-]+ ]]; then
            break
        else
            echo -e "${YELLOW}⚠️  Неверный формат URL. Пожалуйста, используйте полный URL Google таблицы${NC}"
        fi
    done
    
    # Извлечение ID таблицы из URL (для информации)
    if [[ "$google_url" =~ /d/([a-zA-Z0-9_-]+)/ ]]; then
        sheet_id="${BASH_REMATCH[1]}"
        echo "📄 ID таблицы: $sheet_id"
    fi
    
    # Создание .env файла
    cat > .env << EOF
# Конфигурация Google Sheets
GOOGLE_SHEET_URL=$google_url

# Настройки приложения
FLASK_ENV=production
FLASK_APP=duty_app.py
TZ=Europe/Moscow
GOOGLE_CREDENTIALS_FILE=credentials.json

# Настройки контейнера
DOCKER_IMAGE=$DOCKER_IMAGE
DOCKER_TAG=$DOCKER_TAG
EOF
    
    echo -e "${GREEN}✅ Файл .env создан${NC}"
    echo -e "${YELLOW}📝 Содержимое .env:${NC}"
    cat .env
else
    echo -e "${GREEN}✅ Файл .env уже существует${NC}"
    echo -e "${YELLOW}📝 Текущие настройки:${NC}"
    grep -v '^#' .env | grep -v '^$'
    echo ""
fi

# Создание docker-compose.yml если не существует
if [ ! -f "$COMPOSE_FILE" ]; then
    echo -e "${YELLOW}📄 Создание docker-compose.yml...${NC}"
    
    cat > "$COMPOSE_FILE" << 'EOF'
version: '3.8'

services:
  duty-schedule:
    image: ${DOCKER_IMAGE}:${DOCKER_TAG}
    container_name: duty-schedule-app
    restart: unless-stopped
    ports:
      - "5000:5000"
    environment:
      - FLASK_ENV=${FLASK_ENV}
      - FLASK_APP=${FLASK_APP}
      - GOOGLE_SHEET_URL=${GOOGLE_SHEET_URL}
      - GOOGLE_CREDENTIALS_FILE=${GOOGLE_CREDENTIALS_FILE}
      - TZ=${TZ}
    volumes:
      - ./credentials.json:/app/credentials.json:ro
      - duty_logs:/var/log/duty-app
    labels:
      - "com.centurylinklabs.watchtower.enable=true"
      - "com.centurylinklabs.watchtower.scope=duty-schedule"
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:5000/health').read()"]
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
    image: containrrr/watchtower
    container_name: watchtower
    restart: unless-stopped
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    command: --interval 300 --scope duty-schedule --cleanup
    environment:
      - WATCHTOWER_POLL_INTERVAL=300
      - TZ=${TZ}

volumes:
  duty_logs:
EOF
    
    echo -e "${GREEN}✅ Файл $COMPOSE_FILE создан${NC}"
else
    echo -e "${GREEN}✅ Файл $COMPOSE_FILE уже существует${NC}"
fi

# Обновление образа
echo ""
echo -e "${BLUE}📥 Получение обновлений...${NC}"
if docker pull "$DOCKER_IMAGE:$DOCKER_TAG" 2>/dev/null; then
    echo -e "${GREEN}✅ Образ успешно обновлен${NC}"
    echo "📦 Версия образа:"
    docker inspect --format='{{index .RepoTags 0}} {{index .Config.Labels "org.opencontainers.image.version"}}' "$DOCKER_IMAGE:$DOCKER_TAG" 2>/dev/null || echo "    (информация о версии не доступна)"
else
    echo -e "${YELLOW}⚠️  Не удалось получить образ из Docker Hub${NC}"
    echo "Будет использован локальный образ или выполнится сборка"
fi

# Остановка существующих контейнеров
echo ""
echo -e "${BLUE}🛑 Остановка предыдущих контейнеров...${NC}"
if $DOCKER_COMPOSE_CMD ps -q >/dev/null 2>&1; then
    $DOCKER_COMPOSE_CMD down
    echo -e "${GREEN}✅ Контейнеры остановлены${NC}"
else
    echo -e "${YELLOW}ℹ️  Нет запущенных контейнеров${NC}"
fi

# Запуск приложения
echo ""
echo -e "${BLUE}🚀 Запуск приложения...${NC}"
if $DOCKER_COMPOSE_CMD up -d; then
    echo -e "${GREEN}✅ Приложение запускается${NC}"
else
    echo -e "${RED}❌ Ошибка при запуске${NC}"
    echo "Пробуем собрать образ локально..."
    if $DOCKER_COMPOSE_CMD build && $DOCKER_COMPOSE_CMD up -d; then
        echo -e "${GREEN}✅ Приложение собрано и запущено локально${NC}"
    else
        echo -e "${RED}❌ Критическая ошибка при запуске${NC}"
        exit 1
    fi
fi

# Ожидание запуска
echo ""
echo -e "${BLUE}⏳ Ожидание запуска приложения (до 40 секунд)...${NC}"
HEALTH_CHECK_TIMEOUT=40
HEALTH_CHECK_INTERVAL=2
for ((i=1; i<=HEALTH_CHECK_TIMEOUT/HEALTH_CHECK_INTERVAL; i++)); do
    if curl -s -f http://localhost:5000/health > /dev/null 2>&1; then
        echo -e "${GREEN}✅ Приложение готово!${NC}"
        break
    fi
    
    if [ $i -eq 1 ]; then
        echo -n "Прогресс: "
    fi
    
    echo -n "#"
    
    if [ $i -eq $((HEALTH_CHECK_TIMEOUT/HEALTH_CHECK_INTERVAL)) ]; then
        echo ""
        echo -e "${YELLOW}⚠️  Приложение запущено, но health check не прошел${NC}"
        echo "Проверьте логи: $DOCKER_COMPOSE_CMD logs duty-schedule"
        break
    fi
    
    sleep $HEALTH_CHECK_INTERVAL
done

# Проверка статуса
echo ""
echo -e "${BLUE}🔍 Проверка статуса...${NC}"

# Проверка health check
if curl -s http://localhost:5000/health > /dev/null 2>&1; then
    echo -e "${GREEN}✅ Health check прошел успешно${NC}"
    
    # Дополнительная проверка версии
    VERSION_INFO=$(curl -s http://localhost:5000/version 2>/dev/null | python3 -c "import sys, json; data=json.load(sys.stdin); print(f'    Версия приложения: {data[\"version\"]}')" 2>/dev/null || echo "")
    if [ ! -z "$VERSION_INFO" ]; then
        echo "$VERSION_INFO"
    fi
else
    echo -e "${YELLOW}⚠️  Health check не прошел${NC}"
fi

# Проверка статуса контейнеров
echo ""
echo -e "${YELLOW}📊 Статус контейнеров:${NC}"
$DOCKER_COMPOSE_CMD ps

# Вывод информации
echo ""
echo -e "${GREEN}🎉 Развертывание завершено!${NC}"
echo ""
echo -e "${BLUE}🌐 Приложение доступно по адресу:${NC}"
echo "   http://localhost:5000"
LOCAL_IP=$(hostname -I | awk '{print $1}' 2>/dev/null)
if [ ! -z "$LOCAL_IP" ] && [ "$LOCAL_IP" != "127.0.0.1" ]; then
    echo "   http://$LOCAL_IP:5000"
fi
echo ""
echo -e "${YELLOW}📋 Команды управления:${NC}"
echo "   $DOCKER_COMPOSE_CMD logs -f duty-schedule   # Логи приложения"
echo "   $DOCKER_COMPOSE_CMD logs -f watchtower      # Логи автообновления"
echo "   $DOCKER_COMPOSE_CMD restart duty-schedule   # Перезапуск только приложения"
echo "   $DOCKER_COMPOSE_CMD restart                 # Перезапуск всех сервисов"
echo "   $DOCKER_COMPOSE_CMD down                    # Остановка"
echo "   $DOCKER_COMPOSE_CMD ps                      # Статус контейнеров"
echo ""
echo -e "${BLUE}🔄 Watchtower:${NC}"
if [ "$WATCHTOWER_ENABLED" = true ]; then
    echo "✅ Автоматическое обновление включено"
    echo "   Интервал проверки: 5 минут"
    echo "   Область действия: только duty-schedule"
    echo "   Старые образы: автоматически удаляются"
else
    echo "❌ Автоматическое обновление отключено"
fi
echo ""
echo -e "${YELLOW}🔧 Для ручного обновления:${NC}"
echo "   $DOCKER_COMPOSE_CMD pull duty-schedule"
echo "   $DOCKER_COMPOSE_CMD up -d duty-schedule"
echo ""
echo -e "${RED}⚠️  Важно:${NC}"
echo "   Не удаляйте файлы:"
echo "   - credentials.json (ключи Google API)"
echo "   - .env (настройки приложения)"
echo "   Лист в таблице должен называться: 'Вечернее дежурство'"