from datetime import datetime, timezone, timedelta
import html
import json
import os
import sqlite3
import threading
import time
import sys
from flask import Flask

import requests
import telebot
from dotenv import load_dotenv

# Явно указываем путь к .env файлу
dotenv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
load_dotenv(dotenv_path=dotenv_path)

# Сбрасываем прокси
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)
os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)

sys.stdout.write("=" * 50 + "\n")
sys.stdout.write("🚀 ЗАПУСК БОТА НА RENDER\n")
sys.stdout.write("=" * 50 + "\n")
sys.stdout.flush()

# --- Настройки администратора ---
try:
    _raw_admin_id = os.getenv("ADMIN_CHAT_ID", "").strip()
    if _raw_admin_id:
        ADMIN_CHAT_ID = int(_raw_admin_id)
    else:
        ADMIN_CHAT_ID = 0
    
    if ADMIN_CHAT_ID == 0:
        sys.stdout.write("❌ ОШИБКА: ADMIN_CHAT_ID не задан!\n")
        sys.stdout.flush()
        sys.exit(1)
    
    sys.stdout.write(f"✅ ADMIN_CHAT_ID: {ADMIN_CHAT_ID}\n")
    sys.stdout.flush()
except Exception as e:
    sys.stdout.write(f"⚠️ Ошибка при загрузке ADMIN_CHAT_ID: {e}\n")
    sys.stdout.flush()
    sys.exit(1)

# --- Зоны ожидания ---
ZONES = {
    "brest": {
        "name": "Брест",
        "url": "https://mon.declarant.by/#/zone/brest-bts",
        "checkpoint_id": "a9173a85-3fc0-424c-84f0-defa632481e4",
        "base_url": "https://belarusborder.by/info",
    },
    "berestovitsa": {
        "name": "Берестовица",
        "url": "https://mon.declarant.by/#/zone/berestovitsa",
        "checkpoint_id": "7e46a2d1-ab2f-11ec-bafb-ac1f6bf889c1",
        "base_url": "https://belarusborder.by/info",
    },
    "bruzgi": {
        "name": "Брузги",
        "url": "https://mon.declarant.by/#/zone/bruzgi",
        "checkpoint_id": "3b797d4d-706a-440f-a1a4-826c191e1e36",
        "base_url": "https://belarusborder.by/info",
    },
}

sessions_lock = threading.Lock()
sessions: dict[int, dict] = {}
db_lock = threading.Lock()

# ==========================================
# ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ
# ==========================================
def init_db():
    try:
        with db_lock:
            conn = sqlite3.connect("bot_users.db")
            cursor = conn.cursor()
            
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    chat_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    joined_at TEXT,
                    is_blocked INTEGER DEFAULT 0,
                    last_activity TEXT,
                    message_count INTEGER DEFAULT 0
                )
            """)
            try:
                cursor.execute("ALTER TABLE users ADD COLUMN last_activity TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute("ALTER TABLE users ADD COLUMN message_count INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS border_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    zone_key TEXT,
                    stats_hour INTEGER DEFAULT 0,
                    stats_day INTEGER DEFAULT 0,
                    total_cars INTEGER DEFAULT 0
                )
            """)
            
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_zone_timestamp 
                ON border_stats(zone_key, timestamp)
            """)
            
            conn.commit()
            conn.close()
        sys.stdout.write("✅ База данных инициализирована (включая таблицу истории)\n")
        sys.stdout.flush()
    except Exception as e:
        sys.stdout.write(f"⚠️ Ошибка инициализации БД: {e}\n")
        sys.stdout.flush()

init_db()

# ==========================================
# ФУНКЦИИ ДЛЯ РАБОТЫ С ИСТОРИЕЙ СТАТИСТИКИ
# ==========================================
def save_stats_to_db(zone_key: str, stats_hour: int, stats_day: int, total_cars: int):
    try:
        with db_lock:
            conn = sqlite3.connect("bot_users.db")
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT INTO border_stats (timestamp, zone_key, stats_hour, stats_day, total_cars)
                VALUES (?, ?, ?, ?, ?)
            """, (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), zone_key, stats_hour, stats_day, total_cars))
            
            cutoff_time = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute("""
                DELETE FROM border_stats 
                WHERE timestamp < ?
            """, (cutoff_time,))
            
            conn.commit()
            conn.close()
    except Exception as e:
        sys.stdout.write(f"⚠️ Ошибка сохранения статистики: {e}\n")
        sys.stdout.flush()


def get_average_stats(zone_key: str, hours: int = 24):
    try:
        with db_lock:
            conn = sqlite3.connect("bot_users.db")
            cursor = conn.cursor()
            
            cutoff_time = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
            
            cursor.execute("""
                SELECT AVG(stats_hour), AVG(stats_day), COUNT(*)
                FROM border_stats 
                WHERE zone_key = ? AND timestamp > ?
            """, (zone_key, cutoff_time))
            
            row = cursor.fetchone()
            conn.close()
            
            if row and row[2] > 0:
                avg_hour = row[0] if row[0] else 0
                avg_day = row[1] if row[1] else 0
                return avg_hour, avg_day, row[2]
            else:
                return 0, 0, 0
    except Exception as e:
        sys.stdout.write(f"⚠️ Ошибка получения средней статистики: {e}\n")
        sys.stdout.flush()
        return 0, 0, 0


def update_user_activity(chat_id):
    try:
        with db_lock:
            conn = sqlite3.connect("bot_users.db")
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE users 
                SET last_activity = ?, message_count = message_count + 1
                WHERE chat_id = ?
            """, (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), chat_id))
            conn.commit()
            conn.close()
    except Exception as e:
        sys.stdout.write(f"⚠️ Ошибка обновления активности: {e}\n")
        sys.stdout.flush()

def save_user_to_db(message):
    try:
        chat_id = message.chat.id
        username = message.from_user.username or ""
        first_name = message.from_user.first_name or ""
        last_name = message.from_user.last_name or ""
        joined_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with db_lock:
            conn = sqlite3.connect("bot_users.db")
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR IGNORE INTO users (chat_id, username, first_name, last_name, joined_at, is_blocked, last_activity, message_count)
                VALUES (?, ?, ?, ?, ?, 0, ?, 0)
            """, (chat_id, username, first_name, last_name, joined_at, joined_at))
            conn.commit()
            conn.close()
        
        update_user_activity(chat_id)
    except Exception as e:
        sys.stdout.write(f"⚠️ Ошибка сохранения пользователя: {e}\n")
        sys.stdout.flush()


def is_user_blocked(chat_id: int) -> bool:
    if chat_id == ADMIN_CHAT_ID:
        return False
    try:
        with db_lock:
            conn = sqlite3.connect("bot_users.db")
            cursor = conn.cursor()
            cursor.execute("SELECT is_blocked FROM users WHERE chat_id = ?", (chat_id,))
            row = cursor.fetchone()
            conn.close()
        return bool(row and row[0] == 1)
    except:
        return False


def load_config(path="bot_config.json"):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return {}


config = load_config()
API_TOKEN = os.getenv("API_TOKEN", "").strip() or config.get("api_token")
if not API_TOKEN:
    sys.stdout.write("❌ ОШИБКА: API_TOKEN не задан!\n")
    sys.stdout.flush()
    sys.exit(1)

QUERY_TOKEN = config.get("query_token", "test")

sys.stdout.write("✅ API_TOKEN загружен\n")
sys.stdout.write(f"✅ QUERY_TOKEN: {QUERY_TOKEN}\n")
sys.stdout.write("=" * 50 + "\n")
sys.stdout.flush()

bot = telebot.TeleBot(API_TOKEN, threaded=True)
app = Flask(__name__)


@app.route("/")
def home():
    return "Bot is active ✅", 200


@app.route("/health")
def health():
    return {"status": "ok", "bot": "running"}, 200


def get_main_keyboard(alarm_status=False, chat_id=None):
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(
        telebot.types.KeyboardButton("🚀 Старт"),
        telebot.types.KeyboardButton("🛑 Стоп")
    )
    markup.row(
        telebot.types.KeyboardButton("🌍 Зона"),
        telebot.types.KeyboardButton("🚗 Фильтр машины"),
        telebot.types.KeyboardButton("❌ Снять фильтр")
    )
    
    alarm_text = "🔕 Выключить сирену" if alarm_status else "🔔 Включить сирену"
    
    if chat_id == ADMIN_CHAT_ID:
        markup.row(
            telebot.types.KeyboardButton(alarm_text),
            telebot.types.KeyboardButton("⚙️ Админ-панель")
        )
    else:
        markup.row(telebot.types.KeyboardButton(alarm_text))
    
    return markup


def get_zone_keyboard():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton("🛑 Брест", callback_data="zone_brest"),
        telebot.types.InlineKeyboardButton("🛑 Берестовица", callback_data="zone_berestovitsa"),
        telebot.types.InlineKeyboardButton("🛑 Брузги", callback_data="zone_bruzgi")
    )
    return markup


def get_report_keyboard(zone_key: str = "brest"):
    zone = ZONES.get(zone_key, ZONES["brest"])
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton(
            f"🔗 Сайт мониторинга {zone['name']}",
            url=zone["url"]
        )
    )
    return markup


def get_zone_name(zone_key: str) -> str:
    return ZONES.get(zone_key, ZONES["brest"])["name"]


def is_monitoring(chat_id: int) -> bool:
    with sessions_lock:
        session = sessions.get(chat_id)
        thread = session.get("thread") if session else None
        return bool(thread and thread.is_alive())


def _safe_json(response):
    if not response.content:
        return {}
    try:
        return response.json()
    except:
        return {}


def fetch_data(zone_key: str = "brest"):
    try:
        zone = ZONES.get(zone_key, ZONES["brest"])
        BASE = zone["base_url"]
        CHECKPOINT = zone["checkpoint_id"]
        
        session_req = requests.Session()
        session_req.trust_env = False
        no_proxy = {"http": None, "https": None}
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://mon.declarant.by",
            "Referer": "https://mon.declarant.by/",
        }

        stats = _safe_json(session_req.get(
            f"{BASE}/monitoring/statistics",
            params={"token": QUERY_TOKEN, "checkpointId": CHECKPOINT},
            timeout=20,
            proxies=no_proxy,
            headers=headers,
        ))
        queue = _safe_json(session_req.get(
            f"{BASE}/monitoring-new",
            params={"token": QUERY_TOKEN, "checkpointId": CHECKPOINT},
            timeout=20,
            proxies=no_proxy,
            headers=headers,
        ))
        if not queue:
            return {"error": "API очереди вернул пустой ответ"}

        cars = queue.get("carLiveQueue", []) or []
        
        sorted_cars = []
        if cars:
            sorted_cars = sorted(
                cars,
                key=lambda x: datetime.strptime(
                    x["registration_date"], "%H:%M:%S %d.%m.%Y"
                ),
            )
            first = sorted_cars[0]
            reg_time = datetime.strptime(
                first["registration_date"], "%H:%M:%S %d.%m.%Y"
            )
            
            by_timezone = timezone(timedelta(hours=3))
            now_by = datetime.now(by_timezone).replace(tzinfo=None)
            
            wait_minutes = int((now_by - reg_time).total_seconds() / 60)
            if wait_minutes < 0:
                wait_minutes = 0

            registration_date = first["registration_date"]
            changed_date = first.get("changed_date", "-")
        else:
            registration_date = "-"
            changed_date = "-"
            wait_minutes = 0

        total = len(sorted_cars)
        stats_hour = stats.get("carLastHour", 0) or 0
        stats_day = stats.get("carLastDay", 0) or 0

        return {
            "total": total,
            "stats_hour": stats_hour,
            "stats_day": stats_day,
            "reg_date": registration_date,
            "changed": changed_date,
            "wait": wait_minutes,
            "sorted_cars": sorted_cars
        }
    except Exception as e:
        return {"error": str(e)}


def format_estimate_html(minutes):
    if minutes is None:
        text = "н/д"
    else:
        hours, mins = divmod(max(0, minutes), 60)
        if hours >= 24:
            days = hours // 24
            remaining_hours = hours % 24
            text = f"{minutes} мин. (~{days} сут. {remaining_hours} ч {mins} мин.)"
        elif hours > 0:
            text = f"{minutes} мин. (~{hours} ч {mins} мин.)"
        else:
            text = f"{minutes} мин."
    return f"<b>{html.escape(text)}</b>"


def calculate_estimate(total: int, avg_hour: float, avg_day: float):
    if total == 0:
        return 0
    
    if avg_hour > 0:
        return int(total / avg_hour * 60)
    elif avg_day > 0:
        return int(total / (avg_day / 24) * 60)
    else:
        return None


def get_status_text(car):
    raw_status = car.get("status") or car.get("state") or car.get("statusName")
    status_map = {2: "Прибыл в ЗО", 3: "Вызван в ПП"}
    
    if isinstance(raw_status, int) and raw_status in status_map:
        return status_map[raw_status]
    if raw_status and not isinstance(raw_status, int):
        return str(raw_status)
    return "В очереди"


def get_car_status_line(cars, target_query, stats_hour):
    if not target_query or not cars:
        return ""
    
    search_query = target_query.replace(" ", "").lower()
    for idx, car in enumerate(cars, start=1):
        car_number = str(
            car.get("regnum") or car.get("number") or 
            car.get("nzp") or car.get("reg_number") or ""
        ).replace(" ", "").lower()
        
        if search_query in car_number or search_query == car_number:
            reg_num = car.get("regnum") or car.get("number") or car.get("nzp") or target_query
            status = get_status_text(car)
            
            eta_str = ""
            if stats_hour > 0:
                car_mins = int((idx / stats_hour) * 60)
                h, m = divmod(car_mins, 60)
                time_formatted = f"~{h}ч {m}мин" if h else f"~{m}мин"
                eta_str = f" | Ожидание: <b>{time_formatted}</b>"
            
            return f"🎯 <b>{html.escape(str(reg_num))}</b>: <b>{idx}-я</b> | <i>{html.escape(str(status))}</i>{eta_str}\n"
    
    return f"🎯 <b>{html.escape(target_query)}</b>: не найдена\n"


def build_report(d, zone_key: str = "brest", car_filter=None, interval: int = None, avg_period: int = 24, history_count: int = 0, urgent_mode: bool = False):
    zone_name = get_zone_name(zone_key)
    car_line = get_car_status_line(d.get("sorted_cars", []), car_filter, d.get("stats_hour", 0))
    filter_block = f"{car_line}━━━━━━━━━━━━━━━\n" if car_line else ""
    
    if urgent_mode:
        interval_text = "  АВАРИЙНЫЙ РЕЖИМ (30 сек)"
    else:
        interval_text = f" Периодом {interval} мин." if interval else ""
    
    if history_count > 0:
        avg_line = f" На основе {history_count} записей за {avg_period} ч\n"
    else:
        avg_line = "📊 По текущим данным\n"
    
    return (
        f" <b>Мониторинг границы {zone_name}</b>\n"
        "━━━━━━━━━━━━━━━\n"
        f"{filter_block}"
        f"🚗 Машин в очереди: {html.escape(str(d['total']))}\n"
        f"🕒 Первая стоит: {html.escape(str(d['wait']))} мин.\n"
        f"⏳ Оценка для вас: {format_estimate_html(d['estimate'])}\n"
        f"{avg_line}"
        f"📉 За час: {html.escape(str(d['stats_hour']))} маш.\n"
        f"📅 За сутки: {html.escape(str(d['stats_day']))} маш.\n"
        f"📅 Дата 1-го авто: {html.escape(str(d['reg_date']))}\n"
        f" Статус изменён: {html.escape(str(d['changed']))}\n"
        "━━━━━━━━━━━━━━━\n"
        f"💡 <i>Мониторинг работает в фоновом режиме.{interval_text}</i>"
    )


# ==========================================
# ФОНОВЫЙ СБОРЩИК СТАТИСТИКИ ПО ВСЕМ ЗОНАМ
# ==========================================
def stats_collector_loop():
    sys.stdout.write("📊 [STATS] Фоновый сборщик статистики запущен\n")
    sys.stdout.flush()
    
    iteration = 0
    while True:
        try:
            iteration += 1
            
            if iteration % 5 == 0:
                sys.stdout.write(f"💓 [STATS] Сборщик жив, собрано {iteration} циклов\n")
                sys.stdout.flush()
            
            for zone_key in ZONES.keys():
                try:
                    data = fetch_data(zone_key)
                    if "error" not in data:
                        save_stats_to_db(
                            zone_key,
                            data.get("stats_hour", 0),
                            data.get("stats_day", 0),
                            data.get("total", 0)
                        )
                        sys.stdout.write(f"✅ [STATS] Сохранена статистика для {zone_key}: {data.get('total', 0)} машин\n")
                        sys.stdout.flush()
                    else:
                        sys.stdout.write(f"⚠️ [STATS] Ошибка получения данных для {zone_key}: {data['error']}\n")
                        sys.stdout.flush()
                except Exception as e:
                    sys.stdout.write(f"❌ [STATS] Ошибка сбора для {zone_key}: {e}\n")
                    sys.stdout.flush()
            
            sys.stdout.write(f"⏳ [STATS] Следующий сбор через 30 минут...\n")
            sys.stdout.flush()
            time.sleep(1800)
                
        except Exception as e:
            sys.stdout.write(f" [STATS] КРИТИЧЕСКАЯ ОШИБКА в сборщике: {e}\n")
            sys.stdout.write(f"🔄 [STATS] Перезапуск через 60 секунд...\n")
            sys.stdout.flush()
            time.sleep(60)


# ==========================================
# УМНЫЙ ЦИКЛ МОНИТОРИНГА (С АВТО-УСКОРЕНИЕМ)
# ==========================================
def monitoring_loop(chat_id: int, stop_event: threading.Event, interval: int, zone_key: str = "brest", start_time: str = None):
    sys.stdout.write(f"✅ [BOT] Поток мониторинга для {chat_id} запущен (интервал: {interval} мин)\n")
    sys.stdout.flush()
    
    iteration = 0
    consecutive_errors = 0
    max_consecutive_errors = 3
    
    urgent_mode = False  # Флаг аварийного режима
    
    while not stop_event.is_set():
        try:
            iteration += 1
            
            if iteration % 10 == 0:
                mode_text = "🔥 АВАРИЙНЫЙ (30 сек)" if urgent_mode else f"обычный ({interval} мин)"
                sys.stdout.write(f"💓 [BOT] Поток {chat_id} жив (итерация {iteration}, режим: {mode_text})\n")
                sys.stdout.flush()

            if is_user_blocked(chat_id):
                sys.stdout.write(f"⚠️ [BOT] Пользователь {chat_id} заблокирован\n")
                sys.stdout.flush()
                break
            
            data = fetch_data(zone_key)
            if "error" in data:
                consecutive_errors += 1
                sys.stdout.write(f"⚠️ [BOT] Ошибка API ({consecutive_errors}/{max_consecutive_errors}): {data['error']}\n")
                sys.stdout.flush()
                
                if consecutive_errors >= max_consecutive_errors:
                    try:
                        bot.send_message(chat_id, f"️ Мониторинг остановлен из-за ошибок API.\nЗапустите заново: /start")
                    except:
                        pass
                    break
                
                time.sleep(60)
                continue
            
            consecutive_errors = 0
            
            with sessions_lock:
                session = sessions.get(chat_id, {})
                avg_period = session.get("avg_period", 24)
                current_filter = session.get("car_filter")
                alarm_enabled = session.get("alarm_enabled", False)
            
            avg_hour, avg_day, history_count = get_average_stats(zone_key, hours=avg_period)
            
            estimate = calculate_estimate(data["total"], avg_hour, avg_day)
            data["estimate"] = estimate
            
            cars = data.get("sorted_cars", [])
            with sessions_lock:
                session = sessions.get(chat_id, {})
                session["last_cars"] = cars

            # Проверяем статус фильтрованной машины для умной сирены
            is_called = False
            if current_filter and alarm_enabled and cars:
                search_query = current_filter.replace(" ", "").lower()
                for car in cars:
                    car_number = str(
                        car.get("regnum") or car.get("number") or 
                        car.get("nzp") or car.get("reg_number") or ""
                    ).replace(" ", "").lower()
                    
                    if search_query in car_number or search_query == car_number:
                        raw_status = car.get("status") or car.get("state") or car.get("statusName")
                        if raw_status == 3 or str(raw_status).lower() in ["вызван в пп", "3"]:
                            is_called = True
                        break
            
            # Логика переключения режимов
            if is_called and not urgent_mode:
                urgent_mode = True
                try:
                    bot.send_message(
                        chat_id,
                        f"🚨 <b>ВНИМАНИЕ! Машина {html.escape(current_filter)} вызвана в ПП!</b>\n"
                        f" <b>Мониторинг ускорен до 30 секунд!</b>",
                        parse_mode="HTML"
                    )
                except Exception as e:
                    sys.stdout.write(f"⚠️ [BOT] Ошибка отправки уведомления: {e}\n")
                    sys.stdout.flush()
                    
            elif not is_called and urgent_mode:
                urgent_mode = False
                try:
                    bot.send_message(
                        chat_id,
                        f"✅ <b>Статус машины {html.escape(current_filter)} изменился.</b>\n"
                        f"🔄 <b>Мониторинг вернулся к интервалу {interval} мин.</b>",
                        parse_mode="HTML"
                    )
                except Exception as e:
                    sys.stdout.write(f"⚠️ [BOT] Ошибка отправки уведомления: {e}\n")
                    sys.stdout.flush()

            try:
                bot.send_message(
                    chat_id,
                    build_report(data, zone_key, current_filter, interval, avg_period, history_count, urgent_mode),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=get_report_keyboard(zone_key),
                )
            except Exception as e:
                sys.stdout.write(f"⚠️ [BOT] Ошибка отправки: {e}\n")
                sys.stdout.flush()

            # Ждем интервал (в секундах)
            sleep_time = 30 if urgent_mode else interval * 60
            for _ in range(int(sleep_time)):
                if stop_event.is_set():
                    break
                time.sleep(1)
                
        except Exception as e:
            consecutive_errors += 1
            sys.stdout.write(f"❌ [BOT] Ошибка в потоке {chat_id} ({consecutive_errors}/{max_consecutive_errors}): {e}\n")
            sys.stdout.flush()
            
            if consecutive_errors >= max_consecutive_errors:
                try:
                    bot.send_message(chat_id, f"🚨 Мониторинг остановлен из-за ошибок.\nЗапустите заново: /start")
                except:
                    pass
                break
            
            time.sleep(60)
    
    sys.stdout.write(f"🛑 [BOT] Поток мониторинга для {chat_id} завершен (итераций: {iteration})\n")
    sys.stdout.flush()


def start_monitoring(chat_id: int, interval: int, zone_key: str = "brest") -> bool:
    if is_user_blocked(chat_id):
        return False
    
    start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    with sessions_lock:
        # Сначала останавливаем старый поток если есть
        session = sessions.get(chat_id)
        if session:
            old_thread = session.get("thread")
            if old_thread and old_thread.is_alive():
                old_stop_event = session.get("stop_event")
                if old_stop_event:
                    old_stop_event.set()
                old_thread.join(timeout=2)
                sys.stdout.write(f"🔄 [BOT] Остановлен старый поток для {chat_id}\n")
                sys.stdout.flush()
        
        stop_event = threading.Event()
        thread = threading.Thread(
            target=monitoring_loop,
            args=(chat_id, stop_event, interval, zone_key, start_time),
            daemon=True,
        )
        
        old_filter = session.get("car_filter") if session else None
        old_alarm = session.get("alarm_enabled", False) if session else False
        old_avg_period = session.get("avg_period", 24) if session else 24
        
        sessions[chat_id] = {
            "thread": thread,
            "stop_event": stop_event,
            "interval": interval,
            "last_cars": [],
            "car_filter": old_filter,
            "alarm_enabled": old_alarm,
            "last_called_state": False,
            "zone_key": zone_key,
            "start_time": start_time,
            "avg_period": old_avg_period
        }
        thread.start()
        return True


def stop_monitoring(chat_id: int) -> bool:
    with sessions_lock:
        session = sessions.get(chat_id)
        if not session:
            return False
        
        thread = session.get("thread")
        was_alive = bool(thread and thread.is_alive())
        
        if was_alive:
            stop_event = session.get("stop_event")
            if stop_event:
                stop_event.set()
            thread.join(timeout=2)
            sys.stdout.write(f"🛑 [BOT] Остановлен поток для {chat_id}\n")
            sys.stdout.flush()
        
        sessions[chat_id] = {
            "car_filter": session.get("car_filter"),
            "last_cars": [],
            "alarm_enabled": session.get("alarm_enabled", False),
            "last_called_state": False,
            "zone_key": session.get("zone_key", "brest"),
            "interval": session.get("interval"),
            "avg_period": session.get("avg_period", 24),
        }
        return was_alive


# ==================== ОБРАБОТЧИКИ ====================

@bot.message_handler(commands=["start"])
@bot.message_handler(func=lambda message: "старт" in message.text.lower())
def start(message):
    if is_user_blocked(message.chat.id):
        bot.reply_to(message, "❌ Доступ к боту ограничен.")
        return
        
    update_user_activity(message.chat.id)
    save_user_to_db(message)
    chat_id = message.chat.id
    with sessions_lock:
        alarm_on = sessions.get(chat_id, {}).get("alarm_enabled", False)
        current_zone = sessions.get(chat_id, {}).get("zone_key", "brest")

    if is_monitoring(chat_id):
        bot.reply_to(
            message,
            f"⚠️ Мониторинг уже запущен для зоны: {get_zone_name(current_zone)}!",
            reply_markup=get_main_keyboard(alarm_on, chat_id),
        )
        return

    zone_name = get_zone_name(current_zone)
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton("⏱ 3 мин", callback_data="int_3"),
        telebot.types.InlineKeyboardButton("⏱ 10 мин", callback_data="int_10"),
        telebot.types.InlineKeyboardButton("✍️ Свой интервал", callback_data="int_custom"),
    )
    bot.send_message(
        chat_id,
        f"🚀 <b>Мониторинг границы {zone_name}</b>\nВыберите интервал опроса:",
        reply_markup=markup,
        parse_mode="HTML",
    )


@bot.message_handler(commands=["zone"])
@bot.message_handler(func=lambda message: "зона" in message.text.lower())
def select_zone(message):
    chat_id = message.chat.id
    if is_user_blocked(chat_id):
        bot.reply_to(message, " Доступ к боту ограничен.")
        return
    
    update_user_activity(chat_id)
    
    with sessions_lock:
        current_zone = sessions.get(chat_id, {}).get("zone_key", "brest")
        alarm_on = sessions.get(chat_id, {}).get("alarm_enabled", False)
    
    zone_name = get_zone_name(current_zone)
    bot.send_message(
        chat_id,
        f" <b>Текущая зона: {zone_name}</b>\nВыберите другую зону ожидания:",
        reply_markup=get_zone_keyboard(),
        parse_mode="HTML"
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("zone_"))
def handle_zone_selection(call):
    chat_id = call.message.chat.id
    if is_user_blocked(chat_id):
        return
    
    update_user_activity(chat_id)
    
    try:
        zone_key = call.data.replace("zone_", "")
        if zone_key not in ZONES:
            bot.answer_callback_query(call.id, "❌ Неизвестная зона")
            return
        
        was_running = is_monitoring(chat_id)
        saved_interval = None
        saved_filter = None
        saved_alarm = False
        saved_avg_period = None

        with sessions_lock:
            old = sessions.get(chat_id, {})
            saved_interval = old.get("interval")
            saved_filter = old.get("car_filter")
            saved_alarm = old.get("alarm_enabled", False)
            saved_avg_period = old.get("avg_period", 24)

        if was_running:
            stop_monitoring(chat_id)

        with sessions_lock:
            sessions[chat_id] = {
                "car_filter": saved_filter,
                "last_cars": [],
                "alarm_enabled": saved_alarm,
                "last_called_state": False,
                "zone_key": zone_key,
                "avg_period": saved_avg_period if saved_avg_period else 24,
            }
            if saved_interval is not None:
                sessions[chat_id]["interval"] = saved_interval

        zone_name = get_zone_name(zone_key)
        bot.answer_callback_query(call.id, f"✅ Зона выбрана: {zone_name}")

        if was_running and saved_interval:
            if start_monitoring(chat_id, saved_interval, zone_key):
                bot.send_message(
                    chat_id,
                    f"🌍 <b>Зона изменена на: {zone_name}</b>\n"
                    f"Мониторинг перезапущен (каждые {saved_interval} мин).",
                    reply_markup=get_main_keyboard(saved_alarm, chat_id),
                    parse_mode="HTML",
                )
                return
        
        bot.send_message(
            chat_id,
            f"🌍 <b>Зона изменена на: {zone_name}</b>\nЗапустите мониторинг кнопкой «🚀 Старт».",
            reply_markup=get_main_keyboard(saved_alarm, chat_id),
            parse_mode="HTML",
        )
    except Exception as e:
        bot.answer_callback_query(call.id, f"❌ Ошибка: {e}")


# ==========================================
# АДМИН-ПАНЕЛЬ (ТОЛЬКО ДЛЯ АДМИНА)
# ==========================================
@bot.message_handler(func=lambda message: "админ" in message.text.lower() or "настройк" in message.text.lower())
def admin_panel(message):
    if message.chat.id != ADMIN_CHAT_ID:
        return
    
    update_user_activity(message.chat.id)
    
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton(" Период усреднения", callback_data="admin_avg"),
    )
    markup.add(
        telebot.types.InlineKeyboardButton("👥 Пользователи", callback_data="admin_users"),
    )
    markup.add(
        telebot.types.InlineKeyboardButton("💾 Бэкап базы", callback_data="admin_backup"),
    )
    
    bot.send_message(
        message.chat.id,
        f"⚙️ <b>Админ-панель</b>\n\nВыберите действие:",
        reply_markup=markup,
        parse_mode="HTML"
    )


@bot.callback_query_handler(func=lambda call: call.data == "admin_avg")
def handle_admin_avg(call):
    chat_id = call.message.chat.id
    if chat_id != ADMIN_CHAT_ID:
        return
    
    with sessions_lock:
        current_avg = sessions.get(chat_id, {}).get("avg_period", 24)
    
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton("6 часов", callback_data="avg_6"),
        telebot.types.InlineKeyboardButton("12 часов", callback_data="avg_12"),
    )
    markup.add(
        telebot.types.InlineKeyboardButton("24 часа (по умолч.)", callback_data="avg_24"),
        telebot.types.InlineKeyboardButton("48 часов", callback_data="avg_48"),
    )
    markup.add(
        telebot.types.InlineKeyboardButton("72 часа (3 суток)", callback_data="avg_72"),
    )
    markup.add(
        telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="admin_back"),
    )
    
    bot.edit_message_text(
        f"📊 <b>Период усреднения для расчёта оценки</b>\n\n"
        f"Текущий период: <b>{current_avg} часов</b>\n\n"
        f"Чем больше период — тем стабильнее оценка.\n"
        f"Чем меньше период — тем точнее отражает текущую ситуацию.\n\n"
        f"Выберите новый период:",
        chat_id=chat_id,
        message_id=call.message.message_id,
        reply_markup=markup,
        parse_mode="HTML"
    )


@bot.callback_query_handler(func=lambda call: call.data == "admin_users")
def handle_admin_users(call):
    chat_id = call.message.chat.id
    if chat_id != ADMIN_CHAT_ID:
        return
    
    show_users_inline(call.message)


@bot.callback_query_handler(func=lambda call: call.data == "admin_backup")
def handle_admin_backup(call):
    chat_id = call.message.chat.id
    if chat_id != ADMIN_CHAT_ID:
        return
    
    bot.answer_callback_query(call.id, "📦 Создание бэкапа...")
    
    try:
        with open("bot_users.db", "rb") as db_file:
            bot.send_document(
                chat_id,
                db_file,
                caption=f" <b>Бэкап базы данных</b>\n\n"
                        f"📅 Дата: {datetime.now().strftime('%d.%m.%Y %H:%M')}\n"
                        f"💾 Файл: bot_users.db\n\n"
                        f"<i>Для восстановления:</i>\n"
                        f"1. Остановите бота\n"
                        f"2. Замените файл bot_users.db на этот\n"
                        f"3. Задеплойте на Render",
                parse_mode="HTML"
            )
        
        bot.edit_message_text(
            "✅ <b>Бэкап базы данных отправлен!</b>\n\n"
            "Файл bot_users.db содержит:\n"
            "• Историю статистики по всем зонам\n"
            "• Список пользователей\n"
            "• Настройки мониторинга\n\n"
            "Сохраните его для восстановления после изменений.",
            chat_id=chat_id,
            message_id=call.message.message_id,
            parse_mode="HTML"
        )
        
    except FileNotFoundError:
        bot.edit_message_text(
            " <b>Ошибка:</b> Файл базы данных не найден!\n\n"
            "Убедитесь, что бот уже работал и создал базу.",
            chat_id=chat_id,
            message_id=call.message.message_id,
            parse_mode="HTML"
        )
    except Exception as e:
        bot.edit_message_text(
            f"❌ <b>Ошибка при создании бэкапа:</b> {e}",
            chat_id=chat_id,
            message_id=call.message.message_id,
            parse_mode="HTML"
        )


@bot.callback_query_handler(func=lambda call: call.data == "admin_back")
def handle_admin_back(call):
    chat_id = call.message.chat.id
    if chat_id != ADMIN_CHAT_ID:
        return
    
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton("📊 Период усреднения", callback_data="admin_avg"),
    )
    markup.add(
        telebot.types.InlineKeyboardButton("👥 Пользователи", callback_data="admin_users"),
    )
    markup.add(
        telebot.types.InlineKeyboardButton("💾 Бэкап базы", callback_data="admin_backup"),
    )
    
    bot.edit_message_text(
        f"⚙️ <b>Админ-панель</b>\n\nВыберите действие:",
        chat_id=chat_id,
        message_id=call.message.message_id,
        reply_markup=markup,
        parse_mode="HTML"
    )


def show_users_inline(message):
    chat_id = message.chat.id
    
    with db_lock:
        conn = sqlite3.connect("bot_users.db")
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id, username, first_name, last_name, joined_at, is_blocked, last_activity, message_count FROM users ORDER BY last_activity DESC")
        rows = cursor.fetchall()
        conn.close()

    if not rows:
        bot.send_message(chat_id, " В базе пока нет ни одного пользователя.")
        return

    markup = telebot.types.InlineKeyboardMarkup()
    text = f"👥 <b>Пользователи ({len(rows)}):</b>\n\n"
    
    now = datetime.now()
    
    for row in rows:
        user_chat_id, username, first_name, last_name, joined_at, is_blocked, last_activity, message_count = row
        uname = f"@{username}" if username else "нет юзернейма"
        name = f"{first_name} {last_name}".strip()
        
        if last_activity:
            try:
                last_act_time = datetime.strptime(last_activity, "%Y-%m-%d %H:%M:%S")
                diff = now - last_act_time
                if diff.total_seconds() < 60:
                    activity_status = "🟢 Только что"
                elif diff.total_seconds() < 3600:
                    mins = int(diff.total_seconds() / 60)
                    activity_status = f"🟢 {mins} мин назад"
                elif diff.total_seconds() < 86400:
                    hours = int(diff.total_seconds() / 3600)
                    activity_status = f"🟡 {hours} ч назад"
                else:
                    days = int(diff.total_seconds() / 86400)
                    activity_status = f"🔴 {days} дн назад"
            except:
                activity_status = " Неизвестно"
        else:
            activity_status = "⚪ Неизвестно"
        
        status_icon = "🔴 Заблок." if is_blocked == 1 else "✅ Активен"
        
        text += f"• <b>{html.escape(name)}</b> ({uname})\n"
        text += f"  ID: <code>{user_chat_id}</code>\n"
        text += f"  {status_icon} | {activity_status} | {message_count} сообщ.\n\n"
        
        if user_chat_id != ADMIN_CHAT_ID:
            if is_blocked == 0:
                markup.add(telebot.types.InlineKeyboardButton(
                    f"🚫 Заблокировать {first_name}", callback_data=f"block_{user_chat_id}"
                ))
            else:
                markup.add(telebot.types.InlineKeyboardButton(
                    f"✅ Разблокировать {first_name}", callback_data=f"unblock_{user_chat_id}"
                ))
            
            markup.add(telebot.types.InlineKeyboardButton(
                f"🗑️ Удалить {first_name}", callback_data=f"delete_{user_chat_id}"
            ))
    
    markup.add(telebot.types.InlineKeyboardButton("️ Назад в админ-панель", callback_data="admin_back"))

    bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("avg_"))
def handle_avg_period(call):
    chat_id = call.message.chat.id
    if chat_id != ADMIN_CHAT_ID:
        return
    
    try:
        new_period = int(call.data.replace("avg_", ""))
        
        with sessions_lock:
            if chat_id in sessions:
                sessions[chat_id]["avg_period"] = new_period
            else:
                sessions[chat_id] = {"avg_period": new_period}
        
        bot.answer_callback_query(call.id, f"✅ Период изменён на {new_period} ч")
        
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(
            telebot.types.InlineKeyboardButton("6 часов", callback_data="avg_6"),
            telebot.types.InlineKeyboardButton("12 часов", callback_data="avg_12"),
        )
        markup.add(
            telebot.types.InlineKeyboardButton("24 часа (по умолч.)", callback_data="avg_24"),
            telebot.types.InlineKeyboardButton("48 часов", callback_data="avg_48"),
        )
        markup.add(
            telebot.types.InlineKeyboardButton("72 часа (3 суток)", callback_data="avg_72"),
        )
        markup.add(
            telebot.types.InlineKeyboardButton("️ Назад", callback_data="admin_back"),
        )
        
        bot.edit_message_text(
            f"✅ <b>Период усреднения изменён на {new_period} часов</b>\n\n"
            f"Следующий отчёт будет рассчитан на основе данных за последние {new_period} ч.",
            chat_id=chat_id,
            message_id=call.message.message_id,
            reply_markup=markup,
            parse_mode="HTML"
        )
    except Exception as e:
        bot.answer_callback_query(call.id, f"❌ Ошибка: {e}")


@bot.callback_query_handler(func=lambda call: call.data.startswith("block_") or call.data.startswith("unblock_") or call.data.startswith("delete_"))
def handle_user_action(call):
    if call.message.chat.id != ADMIN_CHAT_ID:
        return
    
    try:
        action, chat_id_str = call.data.split("_")
        target_chat_id = int(chat_id_str)
        
        if action == "delete":
            with db_lock:
                conn = sqlite3.connect("bot_users.db")
                cursor = conn.cursor()
                cursor.execute("DELETE FROM users WHERE chat_id = ?", (target_chat_id,))
                conn.commit()
                conn.close()
            
            stop_monitoring(target_chat_id)
            bot.answer_callback_query(call.id, "✅ Пользователь удалён!")
        else:
            new_status = 1 if action == "block" else 0

            with db_lock:
                conn = sqlite3.connect("bot_users.db")
                cursor = conn.cursor()
                cursor.execute("UPDATE users SET is_blocked = ? WHERE chat_id = ?", (new_status, target_chat_id))
                conn.commit()
                conn.close()

            if new_status == 1:
                stop_monitoring(target_chat_id)
                try:
                    bot.send_message(target_chat_id, "❌ Администратор ограничил вам доступ к боту.")
                except:
                    pass

            bot.answer_callback_query(call.id, "✅ Статус пользователя изменен!")
        
        show_users_inline(call.message)
    except Exception as e:
        bot.answer_callback_query(call.id, f"❌ Ошибка: {e}")


@bot.callback_query_handler(func=lambda call: call.data in ["int_3", "int_10"])
def set_preset_interval(call):
    chat_id = call.message.chat.id
    if is_user_blocked(chat_id):
        return
    update_user_activity(chat_id)
    try:
        bot.answer_callback_query(call.id)
    except:
        pass
    interval = 3 if call.data == "int_3" else 10
    
    with sessions_lock:
        zone_key = sessions.get(chat_id, {}).get("zone_key", "brest")
    
    if not start_monitoring(chat_id, interval, zone_key):
        with sessions_lock:
            alarm_on = sessions.get(chat_id, {}).get("alarm_enabled", False)
        bot.send_message(chat_id, "️ Мониторинг уже запущен!", reply_markup=get_main_keyboard(alarm_on, chat_id))
        return
    
    with sessions_lock:
        alarm_on = sessions.get(chat_id, {}).get("alarm_enabled", False)

    zone_name = get_zone_name(zone_key)
    bot.send_message(
        chat_id,
        f"✅ Запуск: каждые {interval} минут. Первый отчет отправляется...\n🌍 Зона: {zone_name}",
        reply_markup=get_main_keyboard(alarm_on, chat_id),
    )


@bot.callback_query_handler(func=lambda call: call.data == "int_custom")
def set_custom(call):
    chat_id = call.message.chat.id
    if is_user_blocked(chat_id):
        return
    update_user_activity(chat_id)
    try:
        bot.answer_callback_query(call.id)
    except:
        pass
    with sessions_lock:
        alarm_on = sessions.get(chat_id, {}).get("alarm_enabled", False)

    if is_monitoring(chat_id):
        bot.send_message(chat_id, "⚠️ Мониторинг уже запущен!", reply_markup=get_main_keyboard(alarm_on, chat_id))
        return
    msg = bot.send_message(chat_id, "✍️ Введите количество <b>минут</b> для интервала (число):", parse_mode="HTML")
    bot.register_next_step_handler(msg, process_custom_step)


def process_custom_step(message):
    chat_id = message.chat.id
    if is_user_blocked(chat_id):
        return
    update_user_activity(chat_id)
    try:
        minutes = int(message.text.strip())
        if not (1 <= minutes <= 1440):
            raise ValueError()
    except:
        bot.send_message(chat_id, "❌ Ошибка: введите целое число от 1 до 1440.")
        return

    with sessions_lock:
        zone_key = sessions.get(chat_id, {}).get("zone_key", "brest")
        alarm_on = sessions.get(chat_id, {}).get("alarm_enabled", False)

    if not start_monitoring(chat_id, minutes, zone_key):
        with sessions_lock:
            alarm_on = sessions.get(chat_id, {}).get("alarm_enabled", False)
        bot.send_message(chat_id, "️ Мониторинг уже запущен!", reply_markup=get_main_keyboard(alarm_on, chat_id))
        return
    
    zone_name = get_zone_name(zone_key)
    with sessions_lock:
        alarm_on = sessions.get(chat_id, {}).get("alarm_enabled", False)

    bot.send_message(
        chat_id,
        f"✅ Запуск: каждые {minutes} мин. Первый отчет отправляется...\n🌍 Зона: {zone_name}",
        reply_markup=get_main_keyboard(alarm_on, chat_id),
    )


@bot.message_handler(commands=["stop"])
@bot.message_handler(func=lambda message: "стоп" in message.text.lower())
def stop(message):
    chat_id = message.chat.id
    update_user_activity(chat_id)
    with sessions_lock:
        alarm_on = sessions.get(chat_id, {}).get("alarm_enabled", False)

    if stop_monitoring(chat_id):
        bot.reply_to(message, "🛑 <b>Мониторинг остановлен.</b>", reply_markup=get_main_keyboard(alarm_on, chat_id), parse_mode="HTML")
    else:
        bot.reply_to(message, "⚠️ Мониторинг не активен.", reply_markup=get_main_keyboard(alarm_on, chat_id))


@bot.message_handler(commands=["alarm_on"])
@bot.message_handler(func=lambda message: message.text in [" Включить сирену", "🔔 Включить сирену"])
def enable_alarm(message):
    chat_id = message.chat.id
    update_user_activity(chat_id)
    with sessions_lock:
        if chat_id not in sessions:
            sessions[chat_id] = {"car_filter": None, "last_cars": [], "alarm_enabled": False, "zone_key": "brest", "avg_period": 24}
        sessions[chat_id]["alarm_enabled"] = True
        sessions[chat_id]["last_called_state"] = False

    bot.reply_to(message, "🔔 <b>Громкая сирена включена!</b>", reply_markup=get_main_keyboard(True, chat_id), parse_mode="HTML")


@bot.message_handler(commands=["alarm_off"])
@bot.message_handler(func=lambda message: message.text in ["🔕 Выключить сирену", "🔕 Выключить сирену"])
def disable_alarm(message):
    chat_id = message.chat.id
    update_user_activity(chat_id)
    with sessions_lock:
        if chat_id not in sessions:
            sessions[chat_id] = {"car_filter": None, "last_cars": [], "alarm_enabled": False, "zone_key": "brest", "avg_period": 24}
        sessions[chat_id]["alarm_enabled"] = False

    bot.reply_to(message, "🔕 <b>Громкая сирена выключена.</b>", reply_markup=get_main_keyboard(False, chat_id), parse_mode="HTML")


@bot.message_handler(commands=["filter"])
@bot.message_handler(func=lambda message: "фильтр" in message.text.lower() and "машины" in message.text.lower() and "снять" not in message.text.lower())
def ask_car_filter(message):
    chat_id = message.chat.id
    update_user_activity(chat_id)
    with sessions_lock:
        alarm_on = sessions.get(chat_id, {}).get("alarm_enabled", False)

    msg = bot.send_message(chat_id, "✍️ Введите гос.номер машины:", reply_markup=get_main_keyboard(alarm_on, chat_id))
    bot.register_next_step_handler(msg, save_car_filter_step)


def save_car_filter_step(message):
    chat_id = message.chat.id
    update_user_activity(chat_id)
    text = message.text.strip()
    
    if text.startswith("/"):
        return

    with sessions_lock:
        if chat_id not in sessions:
            sessions[chat_id] = {"car_filter": None, "last_cars": [], "alarm_enabled": False, "zone_key": "brest", "avg_period": 24}
        sessions[chat_id]["car_filter"] = text
        alarm_on = sessions[chat_id]["alarm_enabled"]

    bot.reply_to(message, f"✅ Фильтр по машине <b>{html.escape(text)}</b> установлен! Включите сирену.", reply_markup=get_main_keyboard(alarm_on, chat_id), parse_mode="HTML")


@bot.message_handler(commands=["clear"])
@bot.message_handler(func=lambda message: "снять" in message.text.lower() and "фильтр" in message.text.lower())
def remove_car_filter(message):
    chat_id = message.chat.id
    update_user_activity(chat_id)
    
    with sessions_lock:
        if chat_id in sessions:
            sessions[chat_id]["car_filter"] = None
            alarm_on = sessions[chat_id].get("alarm_enabled", False)
        else:
            alarm_on = False

    bot.reply_to(message, "❌ Фильтр по машине отключен. Мониторинг продолжает работать.", reply_markup=get_main_keyboard(alarm_on, chat_id), parse_mode="HTML")


@bot.message_handler(content_types=['text'])
def handle_car_search(message):
    chat_id = message.chat.id
    text = message.text.strip()

    if text.startswith("/"):
        return
    
    text_lower = text.lower()
    if any(keyword in text_lower for keyword in ["старт", "стоп", "зона", "фильтр", "сирен", "админ"]):
        return

    update_user_activity(chat_id)

    with sessions_lock:
        session = sessions.get(chat_id)
        if not session or not session.get("last_cars"):
            alarm_on = session.get("alarm_enabled", False) if session else False
            bot.reply_to(message, "⚠️ Сначала запустите мониторинг кнопкой «🚀 Старт».", reply_markup=get_main_keyboard(alarm_on, chat_id))
            return
        cars = session["last_cars"]

    search_query = text.replace(" ", "").lower()
    found_car = None
    position = -1

    for idx, car in enumerate(cars, start=1):
        car_number = str(car.get("regnum") or car.get("number") or car.get("nzp") or car.get("reg_number") or "").replace(" ", "").lower()
        
        if search_query in car_number or search_query == car_number:
            found_car = car
            position = idx
            break

    if found_car:
        reg_num = found_car.get("regnum") or found_car.get("number") or found_car.get("nzp") or text
        status = get_status_text(found_car)
        response_text = (
            f"🔍 <b>Результат поиска:</b> {html.escape(str(reg_num))}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🚗 <b>Позиция:</b> <b>{position}</b>-я\n"
            f"📋 <b>Статус:</b> {html.escape(str(status))}\n"
            f"━━━━━━━━━━━━━━━"
        )
    else:
        response_text = f"❌ Машина <b>{html.escape(text)}</b> не найдена."

    bot.reply_to(message, response_text, parse_mode="HTML")


# ==================== ЗАПУСК БОТА ====================

def run_telegram_bot():
    sys.stdout.write("🤖 [BOT] Функция run_telegram_bot ЗАПУЩЕНА\n")
    sys.stdout.flush()
    
    time.sleep(3)
    
    try:
        sys.stdout.write(" [BOT] Очистка вебхуков...\n")
        sys.stdout.flush()
        response = requests.get(
            f"https://api.telegram.org/bot{API_TOKEN}/deleteWebhook?drop_pending_updates=true",
            timeout=10,
        )
        sys.stdout.write(f"✅ [BOT] Вебхуки очищены. Статус: {response.status_code}\n")
        sys.stdout.flush()
    except Exception as e:
        sys.stdout.write(f"⚠️ [BOT] Ошибка при очистке вебхуков: {e}\n")
        sys.stdout.flush()

    while True:
        try:
            sys.stdout.write("🔄 [BOT] Попытка подключения к Telegram API (polling)...\n")
            sys.stdout.flush()
            bot.infinity_polling(timeout=30, long_polling_timeout=30)
        except Exception as e:
            sys.stdout.write(f"❌ [BOT] Ошибка polling: {e}\n")
            sys.stdout.flush()
            sys.stdout.write("🔄 [BOT] Перезапуск через 5 сек...\n")
            sys.stdout.flush()
            time.sleep(5)


sys.stdout.write("🚀 [BOT] Создание потока для Telegram бота...\n")
sys.stdout.flush()
bot_thread = threading.Thread(target=run_telegram_bot, daemon=False)
bot_thread.start()

sys.stdout.write("📊 [STATS] Создание потока для сборщика статистики...\n")
sys.stdout.flush()
stats_thread = threading.Thread(target=stats_collector_loop, daemon=True)
stats_thread.start()

sys.stdout.write("✅ БОТ ГОТОВ К РАБОТЕ\n")
sys.stdout.write("=" * 50 + "\n")
sys.stdout.flush()

if __name__ == "__main__":
    sys.stdout.write("🌐 [FLASK] Запуск веб-сервера на порту 10000...\n")
    sys.stdout.flush()
    app.run(host="0.0.0.0", port=10000)