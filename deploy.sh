#!/bin/bash

echo "🚀 Развертывание Duty Schedule App"
echo "===================================="

# Конфигурация
DOCKER_IMAGE="koroserg/duty-schedule:latest"

# Проверка Docker
if ! command -v docker &> /dev/null; then
    echo "❌ Docker не установлен"
    exit 1
fi

# Проверка файлов
if [ ! -f "credentials.json" ]; then
    echo "❌ Файл credentials.json не найден"
    echo ""
    echo "Инструкция:"
    echo "1. Создайте сервисный аккаунт в Google Cloud"
    echo "2. Скачайте JSON ключи"
    echo "3. Переименуйте в credentials.json"
    echo "4. Положите в эту папку"
    exit 1
fi

# Запрос URL таблицы
if [ ! -f ".env" ]; then
    echo ""
    echo "🔗 Введите URL вашей Google таблицы:"
    echo "Пример: https://docs.google.com/spreadsheets/d/ABC123/edit"
    echo ""
    read -p "URL: " google_url
    
    if [ -z "$google_url" ]; then
        echo "❌ URL не может быть пустым"
        exit 1
    fi
    
    echo "GOOGLE_SHEET_URL=$google_url" > .env
    echo "✅ Файл .env создан"
fi

# Обновление образа
echo ""
echo "📥 Получение последней версии из Docker Hub..."
docker pull $DOCKER_IMAGE || {
    echo "⚠️  Не удалось получить образ, собираем локально..."
    docker-compose build
}

# Запуск
echo ""
echo "🚀 Запуск приложения..."
docker-compose down 2>/dev/null
docker-compose up -d

# Ожидание
echo ""
echo "⏳ Ожидание запуска..."
sleep 8

# Проверка
if curl -s http://localhost:5000/health > /dev/null 2>&1; then
    echo "✅ Приложение запущено!"
    echo "🌐 Доступно по адресу: http://localhost:5000"
    echo ""
    echo "📋 Команды управления:"
    echo "   docker-compose logs -f      # Логи"
    echo "   docker-compose restart      # Перезапуск"
    echo "   docker-compose down         # Остановка"
    echo ""
    echo "🔄 Watchtower будет автоматически обновлять приложение"
else
    echo "⚠️  Приложение запущено, но health check не прошел"
    echo "Проверьте логи: docker-compose logs -f"
fi