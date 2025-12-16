from flask import Flask, render_template, jsonify
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
import pytz
import ntplib
from datetime import datetime, timezone
import socket

# Загружаем переменные окружения
load_dotenv()

app = Flask(__name__)

# =============================================================================
# КОНФИГУРАЦИЯ
# =============================================================================

GOOGLE_SHEET_URL = os.getenv('GOOGLE_SHEET_URL')
CREDENTIALS_FILE = os.getenv('GOOGLE_CREDENTIALS_FILE', 'credentials.json')
SERVER_TIMEZONE = os.getenv('SERVER_TIMEZONE', 'Asia/Yekaterinburg')

if not GOOGLE_SHEET_URL:
    raise ValueError("GOOGLE_SHEET_URL не установлен в переменных окружения")

# =============================================================================
# ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ДЛЯ УПРАВЛЕНИЯ ДАННЫМИ
# =============================================================================

# Кэш данных
data_cache = {
    'schedule': None,
    'last_update': 0,
    'error': None,
    'ntp_time': None,
    'ntp_last_sync': 0
}

# Блокировка для потокобезопасности
cache_lock = threading.Lock()

# Интервалы обновления (секунды)
GOOGLE_UPDATE_INTERVAL = 60  # 1 минута
NTP_UPDATE_INTERVAL = 60     # 1 минута

# Версия приложения
APP_VERSION = "2.1.0"

# =============================================================================
# НАСТРОЙКА ЛОГИРОВАНИЯ
# =============================================================================

def setup_logging():
    """Настройка логирования"""
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
# ФУНКЦИИ ДЛЯ РАБОТЫ С NTP
# =============================================================================

def get_ntp_time():
    """Получение точного времени с NTP сервера напрямую"""
    NTP_SERVERS = [
        'time.google.com',      # Google Public NTP
        'time.windows.com',     # Microsoft NTP
        'pool.ntp.org',         # NTP Pool Project
        'time.apple.com',       # Apple NTP
        'ntp1.stratum2.ru',     # Российский публичный NTP
        'ntp2.stratum2.ru',
    ]
    
    for ntp_server in NTP_SERVERS:
        try:
            logger.info(f"Попытка синхронизации с {ntp_server}...")
            
            # Используем ntplib для запроса
            client = ntplib.NTPClient()
            response = client.request(ntp_server, version=3, timeout=5)
            
            # Время NTP (1900 epoch) -> Unix timestamp
            ntp_timestamp = response.tx_time
            
            # Конвертируем в datetime с UTC
            ntp_time = datetime.fromtimestamp(ntp_timestamp, tz=timezone.utc)
            
            # Конвертируем в указанный часовой пояс сервера
            server_tz = pytz.timezone(SERVER_TIMEZONE)
            ntp_time = ntp_time.astimezone(server_tz)
            
            logger.info(f"✅ Время синхронизировано с {ntp_server}: {ntp_time.strftime('%H:%M:%S')}")
            logger.info(f"   Задержка: {response.delay:.3f} сек, Расхождение: {response.offset:.3f} сек")
            
            return ntp_time
            
        except (ntplib.NTPException, socket.timeout, socket.gaierror, ConnectionRefusedError) as e:
            logger.warning(f"Не удалось получить время с {ntp_server}: {e}")
            continue
        except Exception as e:
            logger.error(f"Ошибка при запросе к {ntp_server}: {e}")
            continue
    
    logger.error("❌ Не удалось синхронизироваться ни с одним NTP сервером")
    # Fallback: локальное время сервера с поправкой на таймзону
    return datetime.now(pytz.timezone(SERVER_TIMEZONE))

def update_ntp_time():
    """Обновление времени с NTP сервера"""
    with cache_lock:
        try:
            ntp_time = get_ntp_time()
            data_cache['ntp_time'] = ntp_time
            data_cache['ntp_last_sync'] = time.time()
            logger.info(f"NTP время обновлено: {ntp_time.strftime('%H:%M:%S')}")
        except Exception as e:
            logger.error(f"Ошибка обновления NTP времени: {e}")
            # Используем текущее время как fallback
            data_cache['ntp_time'] = datetime.now(pytz.timezone(SERVER_TIMEZONE))

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

def parse_schedule_data(worksheet, duty_type='evening'):
    """Парсинг данных таблицы дежурств (исправленная версия)"""
    try:
        all_values = worksheet.get_all_values()
        schedule = []
        
        for row_idx, row in enumerate(all_values):
            for col_idx, cell_value in enumerate(row):
                if is_date_cell(cell_value):
                    date_value = parse_date_cell(cell_value)
                    
                    if date_value:
                        # Ищем дежурного в ячейке ПОД датой (обычная структура)
                        duty_name = ""
                        if row_idx + 1 < len(all_values):
                            duty_cell = all_values[row_idx + 1][col_idx]
                            duty_name = clean_name(duty_cell)
                        
                        # Определяем тип дежурства по параметру, а не по названию листа
                        if duty_type == 'evening':
                            schedule.append({
                                'date': date_value,
                                'evening': duty_name,
                                'morning': '',  # Пусто для вечернего листа
                                'date_str': cell_value.strip(),
                                'cell_location': f"{chr(65 + col_idx)}{row_idx + 1}",
                                'weekday': get_weekday_name(date_value)
                            })
                        elif duty_type == 'morning':
                            schedule.append({
                                'date': date_value,
                                'evening': '',  # Пусто для утреннего листа
                                'morning': duty_name,
                                'date_str': cell_value.strip(),
                                'cell_location': f"{chr(65 + col_idx)}{row_idx + 1}",
                                'weekday': get_weekday_name(date_value)
                            })
        
        logger.info(f"✅ Найдено записей в листе '{worksheet.title}': {len(schedule)}")
        return schedule
        
    except Exception as e:
        logger.error(f"Ошибка при парсинге данных из листа '{worksheet.title}': {e}")
        return None

def update_google_sheets():
    """Обновление данных из Google Sheets (утренние и вечерние дежурства)"""
    with cache_lock:
        try:
            logger.info("🔄 Обновление данных из Google Sheets...")
            
            client = get_google_sheets_client()
            if not client:
                data_cache['error'] = "Не удалось инициализировать клиент Google Sheets"
                logger.error(data_cache['error'])
                return
            
            # Получаем данные из двух листов
            sheet = client.open_by_url(GOOGLE_SHEET_URL)
            
            evening_schedule = []
            morning_schedule = []
            
            # Вечерние дежурства
            try:
                evening_ws = sheet.worksheet("Вечернее дежурство")
                evening_data = parse_schedule_data(evening_ws, duty_type='evening')
                if evening_data:
                    evening_schedule = evening_data
                    logger.info(f"✅ Вечерние дежурства: {len(evening_data)} записей")
                    # Логируем первые 5 дат для отладки
                    for i, duty in enumerate(evening_data[:5]):
                        logger.info(f"  Вечер {i+1}: {duty['date']} - {duty['evening']}")
                else:
                    logger.warning("Вечерние дежурства: данные не найдены")
            except Exception as e:
                logger.error(f"Ошибка при чтении листа 'Вечернее дежурство': {e}")
            
            # Утренние дежурства
            try:
                morning_ws = sheet.worksheet("Дежурство по утрам")
                morning_data = parse_schedule_data(morning_ws, duty_type='morning')
                if morning_data:
                    morning_schedule = morning_data
                    logger.info(f"✅ Утренние дежурства: {len(morning_data)} записей")
                    # Логируем первые 5 дат для отладки
                    for i, duty in enumerate(morning_data[:5]):
                        logger.info(f"  Утро {i+1}: {duty['date']} - {duty['morning']}")
                else:
                    logger.warning("Утренние дежурства: данные не найдены")
            except Exception as e:
                logger.warning(f"Не удалось прочитать лист 'Дежурство по утрам': {e}")
                # Это нормально, если утренних дежурств нет
            
            # Отладочная информация
            logger.info("📊 Статистика перед объединением:")
            logger.info(f"  Вечерних записей: {len(evening_schedule)}")
            logger.info(f"  Утренних записей: {len(morning_schedule)}")
            
            # Проверяем, есть ли общие даты
            evening_dates = {d['date'] for d in evening_schedule}
            morning_dates = {d['date'] for d in morning_schedule}
            common_dates = evening_dates & morning_dates
            
            logger.info(f"  Общие даты: {len(common_dates)}")
            if common_dates:
                for date in sorted(list(common_dates))[:5]:
                    logger.info(f"    - {date}")
            
            # Объединяем расписания
            combined_schedule = combine_schedules(evening_schedule, morning_schedule)
            
            # Логируем результат объединения
            logger.info("📊 Результат объединения расписаний (первые 10 записей):")
            for i, duty in enumerate(combined_schedule[:10]):
                logger.info(f"  {i+1}: {duty['date']} - Утро: '{duty['morning']}', Вечер: '{duty['evening']}'")
            
            data_cache['schedule'] = combined_schedule
            data_cache['last_update'] = time.time()
            data_cache['error'] = None
            
            logger.info(f"✅ Данные успешно обновлены. Всего записей: {len(combined_schedule)}")
                
        except Exception as e:
            data_cache['error'] = f"Ошибка при обновлении данных: {e}"
            logger.error(data_cache['error'])

def combine_schedules(evening_schedule, morning_schedule):
    """Объединение утренних и вечерних дежурств (гибкий вариант)"""
    # Создаем словари для быстрого поиска по дате
    evening_dict = {}
    for duty in evening_schedule:
        date_key = duty['date']
        if date_key not in evening_dict:  # Избегаем дубликатов
            evening_dict[date_key] = duty
    
    morning_dict = {}
    for duty in morning_schedule:
        date_key = duty['date']
        if date_key not in morning_dict:  # Избегаем дубликатов
            morning_dict[date_key] = duty
    
    # Объединяем все даты из обоих расписаний
    all_dates = set(evening_dict.keys()) | set(morning_dict.keys())
    
    combined_schedule = []
    
    for date_key in sorted(all_dates):
        evening_duty = evening_dict.get(date_key)
        morning_duty = morning_dict.get(date_key)
        
        # Проверяем, что дата валидна
        if not isinstance(date_key, date):
            logger.warning(f"Пропускаем невалидную дату: {date_key}")
            continue
        
        # Если есть оба дежурства на эту дату
        if evening_duty and morning_duty:
            combined_schedule.append({
                'date': date_key,
                'evening': evening_duty.get('evening', ''),
                'morning': morning_duty.get('morning', ''),
                'date_str': evening_duty.get('date_str', date_key.strftime('%d.%m.%Y')),
                'weekday': evening_duty.get('weekday', get_weekday_name(date_key))
            })
        # Если есть только вечернее дежурство
        elif evening_duty:
            combined_schedule.append({
                'date': date_key,
                'evening': evening_duty.get('evening', ''),
                'morning': '',  # Пустое утро
                'date_str': evening_duty.get('date_str', date_key.strftime('%d.%m.%Y')),
                'weekday': evening_duty.get('weekday', get_weekday_name(date_key))
            })
        # Если есть только утреннее дежурство
        elif morning_duty:
            combined_schedule.append({
                'date': date_key,
                'evening': '',  # Пустой вечер
                'morning': morning_duty.get('morning', ''),
                'date_str': morning_duty.get('date_str', date_key.strftime('%d.%m.%Y')),
                'weekday': morning_duty.get('weekday', get_weekday_name(date_key))
            })
    
    logger.info(f"✅ Гибкое объединение: {len(combined_schedule)} записей")
    logger.info(f"   - Из вечерних: {len(evening_schedule)} -> {len(evening_dict)} уникальных")
    logger.info(f"   - Из утренних: {len(morning_schedule)} -> {len(morning_dict)} уникальных")
    logger.info(f"   - Объединено: {len(combined_schedule)}")
    
    return combined_schedule

# =============================================================================
# ФОНОВЫЕ ЗАДАЧИ
# =============================================================================

def background_updater():
    """Фоновая задача для периодического обновления"""
    logger.info("🚀 Фоновый обновитель запущен")
    
    # Первоначальное обновление
    update_ntp_time()
    update_google_sheets()
    
    last_google_update = time.time()
    last_ntp_update = time.time()
    
    while True:
        try:
            current_time = time.time()
            
            # Обновляем Google Sheets данные каждую минуту
            if current_time - last_google_update >= GOOGLE_UPDATE_INTERVAL:
                update_google_sheets()
                last_google_update = current_time
            
            # Обновляем NTP время каждую минуту
            if current_time - last_ntp_update >= NTP_UPDATE_INTERVAL:
                update_ntp_time()
                last_ntp_update = current_time
            
            # Ждем 10 секунд перед следующей проверкой
            time.sleep(10)
            
        except Exception as e:
            logger.error(f"Ошибка в фоновом обновителе: {e}")
            time.sleep(10)

# =============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ РАБОТЫ С ДАННЫМИ
# =============================================================================

def get_today_duty(schedule_data):
    """Получение дежурных на сегодня"""
    if not schedule_data:
        return None
    
    today = date.today()
    for duty in schedule_data:
        if duty['date'] == today:
            return duty
    return None

def get_two_work_weeks(schedule_data):
    """Получаем 2 недели рабочих дней с обработкой данных"""
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
            # Берем готовую запись из объединенного расписания
            display_duty = duty.copy()
        else:
            # Если нет данных для этой даты
            display_duty = {
                'date': work_date,
                'morning': '',
                'evening': '',
                'date_str': work_date.strftime('%d.%m.%Y'),
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
# API ЭНДПОИНТЫ
# =============================================================================

@app.route('/api/data')
def get_data():
    """API для получения данных (используется фронтом)"""
    with cache_lock:
        # ... подготовка данных как раньше ...
        
        # Получаем сегодняшнего дежурного (обновленная структура)
        schedule = data_cache.get('schedule', [])
        today_duty = get_today_duty(schedule)
        
        # Получаем расписание на 2 недели
        weeks = get_two_work_weeks(schedule)
        
        # Подготавливаем данные для JSON (с утренними/вечерними дежурствами)
        weeks_json = []
        for week in weeks:
            week_json = []
            for duty in week:
                week_json.append({
                    'date': duty['date'].strftime('%Y-%m-%d'),
                    'morning': duty.get('morning', ''),
                    'evening': duty.get('evening', ''),
                    'date_str': duty['date'].strftime('%d.%m'),
                    'weekday': duty['weekday']
                })
            weeks_json.append(week_json)
        
        return jsonify({
            'success': True,
            'data': {
                'today': date.today().strftime('%Y-%m-%d'),
                'today_duty': {
                    'morning': today_duty.get('morning', '') if today_duty else '',
                    'evening': today_duty.get('evening', '') if today_duty else '',
                    'date': today_duty['date'].strftime('%Y-%m-%d') if today_duty else ''
                } if today_duty else None,
                'weeks': weeks_json,
                # ... остальные поля без изменений ...
            },
            'timestamp': time.time()
        })

@app.route('/api/health')
def api_health():
    """Health check для фронта"""
    with cache_lock:
        return jsonify({
            'status': 'healthy',
            'ntp_synced': data_cache.get('ntp_time') is not None,
            'data_loaded': data_cache.get('schedule') is not None,
            'last_data_update': data_cache.get('last_update', 0),
            'last_ntp_sync': data_cache.get('ntp_last_sync', 0),
            'timestamp': time.time()
        })

# =============================================================================
# МАРШРУТЫ ДЛЯ ОТОБРАЖЕНИЯ
# =============================================================================

@app.route('/')
def index():
    """Главная страница (SSR версия)"""
    with cache_lock:
        schedule = data_cache.get('schedule', [])
        error = data_cache.get('error')
        
        # Получаем NTP время для отображения
        ntp_time = data_cache.get('ntp_time')
        if ntp_time:
            current_time = ntp_time.strftime('%H:%M:%S')
        else:
            current_time = datetime.now(pytz.timezone(SERVER_TIMEZONE)).strftime('%H:%M:%S')
        
        # Время последнего обновления (ТОЛЬКО ВРЕМЯ!)
        last_update = data_cache.get('last_update', 0)
        if last_update > 0:
            update_time = datetime.fromtimestamp(last_update, pytz.timezone(SERVER_TIMEZONE))
            last_updated = update_time.strftime('%H:%M')  # Только часы:минуты
        else:
            last_updated = "00:00"
        
        today_duty = get_today_duty(schedule)
        weeks = get_two_work_weeks(schedule)
        
        return render_template('index.html',
                             today_duty=today_duty,
                             weeks=weeks,
                             today=date.today(),
                             current_time=current_time,
                             last_updated=last_updated,  # Только время
                             error=error,
                             version=APP_VERSION)

# =============================================================================
# ЗАПУСК ПРИЛОЖЕНИЯ
# =============================================================================

def main():
    """Основная функция запуска"""
    print("=" * 60)
    print("🚀 Запуск Duty Schedule App v2.1")
    print("=" * 60)
    print(f"📊 Автообновление данных: каждые {GOOGLE_UPDATE_INTERVAL} секунд")
    print(f"⏰ Синхронизация времени: каждые {NTP_UPDATE_INTERVAL} секунд")
    print(f"🌍 Часовой пояс сервера: {SERVER_TIMEZONE}")
    print(f"🔗 Google Sheet URL: {GOOGLE_SHEET_URL[:50]}...")
    print(f"📦 Версия: {APP_VERSION}")
    print("=" * 60)
    
    # Запускаем фоновый обновитель
    updater_thread = threading.Thread(target=background_updater, daemon=True)
    updater_thread.start()
    logger.info("✅ Фоновый обновитель запущен")
    
    try:
        app.run(debug=False, host='0.0.0.0', port=5000)
    except Exception as e:
        logger.error(f"Критическая ошибка при запуске: {e}")
        print(f"❌ Приложение завершилось с ошибкой: {e}")

if __name__ == '__main__':
    main()