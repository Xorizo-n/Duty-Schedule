from flask import Flask, render_template, make_response
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, date, timedelta
import os
import re
import sys
import logging
import time
import threading
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv()

app = Flask(__name__)

# =============================================================================
# ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ДЛЯ УПРАВЛЕНИЯ ДАННЫМИ
# =============================================================================

# Кэш расписания
schedule_cache = None
# Время последнего успешного обновления
last_update_time = 0
# Текст последней ошибки
last_error = None
# Минимальный интервал между обновлениями (секунды)
UPDATE_INTERVAL = 300  # 5 минут
# Флаг обновления данных
is_updating = False
# Версия приложения
APP_VERSION = "2.0.0"

# =============================================================================
# НАСТРОЙКА ЛОГИРОВАНИЯ С РОТАЦИЕЙ
# =============================================================================

def setup_logging():
    """Настройка логирования с ротацией"""
    logger = logging.getLogger()
    logger.setLevel(logging.WARNING)
    
    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Консольный вывод
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # Файловый вывод с ротацией
    try:
        log_dir = '/var/log/duty-app'
        if not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)
        
        log_file = os.path.join(log_dir, 'app.log')
        
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=10*1024*1024,  # 10 MB
            backupCount=5,
            encoding='utf-8'
        )
        file_handler.setLevel(logging.WARNING)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        
    except Exception as e:
        print(f"Не удалось настроить файловое логирование: {e}")
    
    return logger

logger = setup_logging()

# =============================================================================
# КОНФИГУРАЦИЯ
# =============================================================================

GOOGLE_SHEET_URL = os.getenv('GOOGLE_SHEET_URL')
CREDENTIALS_FILE = os.getenv('GOOGLE_CREDENTIALS_FILE', 'credentials.json')

if not GOOGLE_SHEET_URL:
    logger.error("GOOGLE_SHEET_URL не установлен в переменных окружения")
    raise ValueError("GOOGLE_SHEET_URL не установлен в переменных окружения")

# =============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =============================================================================

def add_cache_headers(response):
    """Добавляем заголовки для предотвращения кэширования"""
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

def cleanup_old_logs():
    """Очистка старых логов (старше 7 дней)"""
    try:
        log_dir = '/var/log/duty-app'
        if not os.path.exists(log_dir):
            return
        
        cutoff_time = time.time() - (7 * 24 * 60 * 60)  # 7 дней назад
        
        for filename in os.listdir(log_dir):
            filepath = os.path.join(log_dir, filename)
            if os.path.isfile(filepath) and filename.endswith('.log'):
                if filename != 'app.log' and os.path.getmtime(filepath) < cutoff_time:
                    os.remove(filepath)
                    logger.info(f"Удален старый лог: {filename}")
                    
    except Exception as e:
        logger.error(f"Ошибка при очистке логов: {e}")

# =============================================================================
# ФУНКЦИИ ДЛЯ РАБОТЫ С GOOGLE SHEETS
# =============================================================================

def get_google_sheets_client():
    """Инициализация клиента Google Sheets"""
    try:
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive"
        ]
        
        if not os.path.exists(CREDENTIALS_FILE):
            logger.error(f"Файл учетных данных не найден: {CREDENTIALS_FILE}")
            return None
            
        creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scope)
        client = gspread.authorize(creds)
        return client
        
    except Exception as e:
        logger.error(f"Ошибка при инициализации клиента Google Sheets: {e}")
        return None

def clean_name(name):
    """Очистка имени от комментариев и лишних символов"""
    if not name:
        return ""
    
    name = re.sub(r'\([^)]*\)', '', name)
    name = re.sub(r'с \d+:\d+', '', name)
    name = name.replace('<br>', ', ').strip()
    name = re.sub(r'\s+', ' ', name)
    
    return name.strip(' ,')

def is_date_cell(cell_value):
    """Проверяет, является ли ячейка датой"""
    if not cell_value:
        return False
    
    cell_value = str(cell_value).strip()
    date_pattern_full = r'^\d{1,2}\.\d{1,2}\.\d{4}$'
    date_pattern_short = r'^\d{1,2}\.\d{1,2}$'
    
    return bool(re.match(date_pattern_full, cell_value) or re.match(date_pattern_short, cell_value))

def parse_date_cell(date_str):
    """Парсит дату из формата ДД.ММ.ГГГГ или ДД.ММ"""
    try:
        date_str = str(date_str).strip()
        
        if re.match(r'^\d{1,2}\.\d{1,2}\.\d{4}$', date_str):
            return datetime.strptime(date_str, '%d.%m.%Y').date()
        elif re.match(r'^\d{1,2}\.\d{1,2}$', date_str):
            current_year = datetime.now().year
            date_with_year = f"{date_str}.{current_year}"
            return datetime.strptime(date_with_year, '%d.%m.%Y').date()
        
        return None
    except ValueError:
        return None

def get_weekday_name(date_obj):
    """Возвращает название дня недели на русском"""
    weekdays = {
        0: 'ПН', 1: 'ВТ', 2: 'СР', 3: 'ЧТ', 4: 'ПТ', 5: 'СБ', 6: 'ВС'
    }
    return weekdays[date_obj.weekday()]

def parse_schedule_data(worksheet):
    """Парсинг данных таблицы дежурств"""
    try:
        all_values = worksheet.get_all_values()
        schedule = []
        
        for row_idx, row in enumerate(all_values):
            for col_idx, cell_value in enumerate(row):
                if is_date_cell(cell_value):
                    date_value = parse_date_cell(cell_value)
                    
                    if date_value and row_idx + 1 < len(all_values):
                        duty_person_cell = all_values[row_idx + 1][col_idx]
                        duty_person = clean_name(duty_person_cell)
                        
                        if duty_person:
                            schedule.append({
                                'date': date_value,
                                'name': duty_person,
                                'date_str': cell_value.strip(),
                                'raw_name': duty_person_cell,
                                'cell_location': f"{chr(65 + col_idx)}{row_idx + 1}",
                                'weekday': get_weekday_name(date_value)
                            })
        
        logger.info(f"Найдено записей о дежурствах: {len(schedule)}")
        return schedule
        
    except Exception as e:
        logger.error(f"Ошибка при парсинге данных: {e}")
        return None

def update_schedule_data():
    """Фоновая задача обновления данных"""
    global schedule_cache, last_update_time, last_error, is_updating
    
    if is_updating:
        return
    
    is_updating = True
    logger.info("🔄 Запуск обновления данных...")
    
    try:
        client = get_google_sheets_client()
        if not client:
            last_error = "Не удалось инициализировать клиент Google Sheets"
            logger.error(last_error)
            return
            
        sheet = client.open_by_url(GOOGLE_SHEET_URL)
        worksheet = sheet.worksheet("Вечернее дежурство")
        
        new_data = parse_schedule_data(worksheet)
        if new_data is not None:
            schedule_cache = new_data
            last_update_time = time.time()
            last_error = None
            logger.info(f"✅ Данные успешно обновлены. Записей: {len(new_data)}")
        else:
            last_error = "Не удалось распарсить данные таблицы"
            logger.error(last_error)
            
    except Exception as e:
        last_error = f"Ошибка при обновлении данных: {e}"
        logger.error(last_error)
    finally:
        is_updating = False

def background_updater():
    """Фоновая задача для периодического обновления"""
    while True:
        try:
            # Обновляем данные если кэш пустой или устарел
            current_time = time.time()
            if not schedule_cache or (current_time - last_update_time > UPDATE_INTERVAL):
                update_schedule_data()
            
            # Раз в день чистим логи
            if current_time % 86400 < 60:  # Раз в сутки
                cleanup_old_logs()
            
            # Ждем 1 минуту перед следующей проверкой
            time.sleep(60)
            
        except Exception as e:
            logger.error(f"Ошибка в фоновом обновителе: {e}")
            time.sleep(60)

def get_cached_schedule():
    """Получение данных из кэша"""
    global schedule_cache, last_error
    
    # Если данных нет, пытаемся обновить синхронно
    if not schedule_cache:
        logger.info("Кэш пустой, выполняем синхронное обновление...")
        update_schedule_data()
    
    return schedule_cache, last_error

def get_today_duty(schedule_data):
    """Получение дежурного на сегодня"""
    if not schedule_data:
        return None
    
    today = date.today()
    for duty in schedule_data:
        if duty['date'] == today:
            return duty
    return None

def get_two_work_weeks(schedule_data):
    """Получаем 2 недели рабочих дней (12 дней: ПН-СБ)"""
    if not schedule_data:
        return []
    
    today = date.today()
    current_week_start = today - timedelta(days=today.weekday())
    
    if today.weekday() == 6:  # Воскресенье
        current_week_start = today + timedelta(days=1)
    
    # Создаем 2 недели рабочих дней
    all_work_days = []
    for week_offset in range(2):
        week_start = current_week_start + timedelta(weeks=week_offset)
        for day_offset in range(6):  # ПН-СБ
            current_date = week_start + timedelta(days=day_offset)
            all_work_days.append(current_date)
    
    # Создаем словарь для быстрого поиска
    schedule_dict = {duty['date']: duty for duty in schedule_data}
    
    # Формируем данные для отображения
    display_weeks = []
    current_week_data = []
    
    for work_date in all_work_days:
        duty = schedule_dict.get(work_date)
        if duty:
            display_duty = duty.copy()
        else:
            display_duty = {
                'date': work_date,
                'name': '',
                'date_str': work_date.strftime('%d.%m.%Y'),
                'raw_name': '',
                'weekday': get_weekday_name(work_date)
            }
        
        current_week_data.append(display_duty)
        
        if len(current_week_data) == 6:
            display_weeks.append(current_week_data)
            current_week_data = []
    
    if current_week_data:
        display_weeks.append(current_week_data)
    
    return display_weeks

# =============================================================================
# МАРШРУТЫ FLASK
# =============================================================================

@app.after_request
def apply_caching(response):
    """Применяем заголовки кэширования ко всем ответам"""
    return add_cache_headers(response)

@app.route('/')
def index():
    """Главная страница с дежурствами"""
    # Используем кэшированные данные (без запросов к Google)
    schedule_data, error_msg = get_cached_schedule()
    
    today_duty = get_today_duty(schedule_data) if schedule_data else None
    weeks = get_two_work_weeks(schedule_data) if schedule_data else []
    
    current_time = datetime.now().strftime('%H:%M')
    last_updated_display = datetime.fromtimestamp(last_update_time).strftime('%H:%M') if last_update_time else "никогда"
    
    response = make_response(render_template('index.html', 
                         today_duty=today_duty,
                         weeks=weeks,
                         today=date.today(),
                         current_time=current_time,
                         last_updated=last_updated_display,
                         error=error_msg,
                         version=APP_VERSION))
    
    return response

@app.route('/health')
def health_check():
    """Health check для Docker и оркестрации"""
    return {
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'version': APP_VERSION,
        'data_updated': bool(schedule_cache),
        'last_update': datetime.fromtimestamp(last_update_time).isoformat() if last_update_time else None
    }

@app.route('/version')
def version_info():
    """Информация о версии приложения"""
    return {
        'app_name': 'Duty Schedule App',
        'version': APP_VERSION,
        'status': 'running',
        'last_data_update': datetime.fromtimestamp(last_update_time).isoformat() if last_update_time else None
    }

# =============================================================================
# ЗАПУСК ПРИЛОЖЕНИЯ
# =============================================================================

def main():
    """Основная функция запуска"""
    print("=" * 60)
    print("🚀 Запуск Duty Schedule App")
    print("=" * 60)
    print(f"📊 Автообновление данных: каждые {UPDATE_INTERVAL//60} минут")
    print(f"🗑️  Очистка логов: старше 7 дней")
    print(f"🔗 Google Sheet URL: {GOOGLE_SHEET_URL[:50]}...")
    print(f"🔑 Credentials file: {CREDENTIALS_FILE}")
    print(f"📦 Версия: {APP_VERSION}")
    print("=" * 60)
    
    # Запускаем фоновый обновитель
    updater_thread = threading.Thread(target=background_updater, daemon=True)
    updater_thread.start()
    logger.info("✅ Фоновый обновитель запущен")
    
    # Первоначальная загрузка данных
    print("📥 Первоначальная загрузка данных...")
    update_schedule_data()
    
    try:
        app.run(debug=False, host='0.0.0.0', port=5000)
    except Exception as e:
        logger.error(f"Критическая ошибка при запуске: {e}")
        print(f"❌ Приложение завершилось с ошибкой: {e}")

if __name__ == '__main__':
    main()