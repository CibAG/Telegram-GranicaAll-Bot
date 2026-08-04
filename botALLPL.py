from datetime import datetime, timezone, timedelta
import html
import json
import os
import sqlite3
import threading
import time
from flask import Flask

import requests
import telebot
from dotenv import load_dotenv

load_dotenv()

# Принудительно сбрасываем прокси-переменные окружения Render
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)
os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)

# --- Настройки администратора ---
ADMIN_CHAT_ID = 8878062282

# --- Зоны ожидания ---
ZONES = {
    "brest": {
        "name": "Брест",
        "url": "https://mon.declarant.by/#/zone/brest-bts",
        "checkpoint_id": "brest-bts",
        "base_url": "https://belarusborder.by/info"
    },
    "berestovitsa": {
        "name": "Берестовица",
        "url": "https://mon.declarant.by/#/zone/berestovitsa",
        "checkpoint_id": "berestovitsa",
        "base_url": "https://belarusborder.by/info"
    },
    "bruzgi": {
        "name": "Брузги",
        "url": "https://mon.declarant.by/#/zone/bruzgi",
        "checkpoint_id": "bruzgi",
        "base_url": "https://belarusborder.by/info"
    }
}

sessions_lock = threading.Lock()
sessions: dict[int, dict] = {}

# --- Инициализация базы данных пользователей ---
db_lock = threading.Lock()

def init_db():
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
                is_blocked INTEGER DEFAULT 0
            )
        """)
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN is_blocked INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        conn.commit()
        conn.close()

init_db()

def save_user_to_db(message):
    chat_id = message.chat.id
    username = message.from_user.username or ""
    first_name = message.from_user.first_name or ""
    last_name = message.from_user.last_name or ""
    joined_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with db_lock:
        conn = sqlite3.connect("bot_users.db")
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR IGNORE INTO users (chat_id, username, first_name, last_name, joined_at, is_blocked)
            VALUES (?, ?, ?, ?, ?, 0)
        """, (chat_id, username, first_name, last_name, joined_at))
        conn.commit()
        conn.close()


def is_user_blocked(chat_id: int) -> bool:
    if chat_id == ADMIN_CHAT_ID:
        return False
    with db_lock:
        conn = sqlite3.connect("bot_users.db")
        cursor = conn.cursor()
        cursor.execute("SELECT is_blocked FROM users WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()
        conn.close()
    return bool(row and row[0] == 1)


def load_config(path="bot_config.json"):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


config = load_config()
API_TOKEN = os.getenv("API_TOKEN") or config.get("api_token")
if not API_TOKEN:
    raise SystemExit("API_TOKEN не задан.")

QUERY_TOKEN = config.get("query_token", "test")

bot = telebot.TeleBot(API_TOKEN)

app = Flask(__name__)


@app.route("/")
def home():
    return "Bot is active", 200


def get_main_keyboard(alarm_status=False, chat_id=None):
    markup = telebot.types.ReplyKeyboardMarkup(
        resize_keyboard=True
    )
    markup.row(
        telebot.types.KeyboardButton("🚀 Старт"),
        telebot.types.KeyboardButton("🛑 Стоп")
    )
    markup.row(
        telebot.types.KeyboardButton("🚗 Фильтр машины"),
        telebot.types.KeyboardButton("❌ Снять фильтр")
    )
    alarm_text = "🔔 Сирена: ВКЛ" if alarm_status else "🔕 Сирена: ВЫКЛ"
    
    if chat_id == ADMIN_CHAT_ID:
        markup.row(
            telebot.types.KeyboardButton(alarm_text),
            telebot.types.KeyboardButton("👥 Пользователи")
        )
    else:
        markup.row(
            telebot.types.KeyboardButton(alarm_text)
        )
    return markup


def get_zone_keyboard():
    """Клавиатура выбора зоны ожидания"""
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton("🛑 Брест", callback_data="zone_brest"),
        telebot.types.InlineKeyboardButton("🛑 Берестовица", callback_data="zone_berestovitsa"),
        telebot.types.InlineKeyboardButton("🛑 Брузги", callback_data="zone_bruzgi")
    )
    return markup


def get_report_keyboard(zone_key: str = "brest"):
    """Клавиатура с кнопкой сайта мониторинга для выбранной зоны"""
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
    """Получить название зоны по ключу"""
    return ZONES.get(zone_key, ZONES["brest"])["name"]


def is_monitoring(chat_id: int) -> bool:
    with sessions_lock:
        session = sessions.get(chat_id)
        return bool(session and session["thread"].is_alive())


def fetch_data(zone_key: str = "brest"):
    """Получить данные мониторинга для указанной зоны"""
    try:
        zone = ZONES.get(zone_key, ZONES["brest"])
        BASE = zone["base_url"]
        CHECKPOINT = zone["checkpoint_id"]
        
        session_req = requests.Session()
        session_req.trust_env = False
        no_proxy = {"http": None, "https": None}

        stats = session_req.get(
            f"{BASE}/monitoring/statistics",
            params={"token": QUERY_TOKEN, "checkpointId": CHECKPOINT},
            timeout=20,
            proxies=no_proxy
        ).json()
        queue = session_req.get(
            f"{BASE}/monitoring-new",
            params={"token": QUERY_TOKEN, "checkpointId": CHECKPOINT},
            timeout=20,
            proxies=no_proxy
        ).json()

        cars = queue.get("carLiveQueue", [])
        
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
            
            wait_minutes = int(
                (now_by - reg_time).total_seconds() / 60
            )
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

        if total == 0:
            estimate_minutes = 0
        elif stats_hour > 0:
            estimate_minutes = int(total / stats_hour * 60)
        elif stats_day > 0:
            estimate_minutes = int(total / (stats_day / 24) * 60)
        else:
            estimate_minutes = None

        return {
            "total": total,
            "stats_hour": stats_hour,
            "stats_day": stats_day,
            "reg_date": registration_date,
            "changed": changed_date,
            "wait": wait_minutes,
            "estimate": estimate_minutes,
            "sorted_cars": sorted_cars
        }
    except Exception as e:
        return {"error": str(e)}


def format_estimate_html(minutes):
    if minutes is None:
        text = "н/д"
    else:
        hours, mins = divmod(max(0, minutes), 60)
        text = (
            f"{minutes} мин. (~{hours} ч {mins} мин.)"
            if hours
            else f"{minutes} мин."
        )
    return f"<b>{html.escape(text)}</b>"


def get_status_text(car):
    raw_status = car.get("status") or car.get("state") or car.get("statusName")
    
    status_map = {
        2: "Прибыл в ЗО",
        3: "Вызван в ПП",
    }
    
    if isinstance(raw_status, int) and raw_status in status_map:
        return status_map[raw_status]
    
    if raw_status and not isinstance(raw_status, int):
        return str(raw_status)
        
    return "В очереди"


def check_car_alarm_trigger(cars, target_query, chat_id):
    if is_user_blocked(chat_id):
        return
    if not target_query or not cars:
        return

    search_query = target_query.replace(" ", "").lower()
    for idx, car in enumerate(cars, start=1):
        car_number = str(
            car.get("regnum") or 
            car.get("number") or 
            car.get("nzp") or 
            car.get("reg_number") or ""
        ).replace(" ", "").lower()
        
        if search_query in car_number or search_query == car_number:
            raw_status = car.get("status") or car.get("state") or car.get("statusName")
            is_called = (raw_status == 3 or str(raw_status).lower() in ["вызван в пп", "3"])
            
            with sessions_lock:
                session = sessions.get(chat_id, {})
                last_state = session.get("last_called_state", False)
                
                if is_called and not last_state and session.get("alarm_enabled", False):
                    session["last_called_state"] = True
                    trigger_alarm(chat_id, car.get("regnum") or target_query)
                elif not is_called:
                    session["last_called_state"] = False
            break


def trigger_alarm(chat_id, reg_num):
    if is_user_blocked(chat_id):
        return
    try:
        bot.send_message(
            chat_id,
            f"🚨 <b>ВНИМАНИЕ! Машина {html.escape(str(reg_num))} вызвана в ПП!</b> 🚨",
            parse_mode="HTML"
        )
        
        if os.path.exists("alarm.ogg"):
            with open("alarm.ogg", "rb") as audio:
                bot.send_voice(chat_id, audio, caption="🔊 Тревога! Ваша машина вызвана в пункт пропуска!")
        else:
            alarm_audio_url = "https://upload.wikimedia.org/wikipedia/commons/9/9b/Air_raid_siren_uk.ogg"
            bot.send_audio(chat_id, alarm_audio_url, caption="🔊 Тревога! Ваша машина вызвана в пункт пропуска!")
            
    except Exception as e:
        print(f"Ошибка отправки сирены: {e}")


def get_car_status_line(cars, target_query, stats_hour):
    if not target_query or not cars:
        return ""
    
    search_query = target_query.replace(" ", "").lower()
    for idx, car in enumerate(cars, start=1):
        car_number = str(
            car.get("regnum") or 
            car.get("number") or 
            car.get("nzp") or 
            car.get("reg_number") or ""
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


def build_report(d, zone_key: str = "brest", car_filter=None):
    zone_name = get_zone_name(zone_key)
    car_line = get_car_status_line(d.get("sorted_cars", []), car_filter, d.get("stats_hour", 0))
    filter_block = f"{car_line}━━━━━━━━━━━━━━━\n" if car_line else ""
    
    return (
        f"📊 <b>Мониторинг границы {zone_name}</b>\n"
        "━━━━━━━━━━━━━━━\n"
        f"{filter_block}"
        f"🚗 Машин в очереди: {html.escape(str(d['total']))}\n"
        f"🕒 Первая стоит: {html.escape(str(d['wait']))} мин.\n"
        f"⏳ Оценка для вас: {format_estimate_html(d['estimate'])}\n"
        f"📉 За час: {html.escape(str(d['stats_hour']))} маш.\n"
        f"📅 За сутки: {html.escape(str(d['stats_day']))} маш.\n"
        f"📅 Дата 1-го авто: {html.escape(str(d['reg_date']))}\n"
        f"🔄 Статус изменён: {html.escape(str(d['changed']))}\n"
        "━━━━━━━━━━━━━━━\n"
        "💡 <i>Мониторинг работает в фоновом режиме.</i>"
    )


def monitoring_loop(chat_id: int, stop_event: threading.Event, interval: int, zone_key: str = "brest"):
    while not stop_event.is_set():
        if is_user_blocked(chat_id):
            break
        data = fetch_data(zone_key)
        if "error" in data:
            bot.send_message(chat_id, f"❌ Ошибка мониторинга: {data['error']}")
        else:
            cars = data.get("sorted_cars", [])
            with sessions_lock:
                session = sessions.get(chat_id, {})
                session["last_cars"] = cars
                current_filter = session.get("car_filter")

            check_car_alarm_trigger(cars, current_filter, chat_id)

            try:
                bot.send_message(
                    chat_id,
                    build_report(data, zone_key, current_filter),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=get_report_keyboard(zone_key),
                )
            except Exception as e:
                pass

        for _ in range(interval * 60):
            if stop_event.is_set() or is_user_blocked(chat_id):
                break
            time.sleep(1)


def start_monitoring(chat_id: int, interval: int, zone_key: str = "brest") -> bool:
    if is_user_blocked(chat_id):
        return False
    with sessions_lock:
        session = sessions.get(chat_id)
        if session and session["thread"].is_alive():
            return False
            
        stop_event = threading.Event()
        thread = threading.Thread(
            target=monitoring_loop,
            args=(chat_id, stop_event, interval, zone_key),
            daemon=True,
        )
        
        old_filter = session.get("car_filter") if session else None
        old_alarm = session.get("alarm_enabled", False) if session else False
        
        sessions[chat_id] = {
            "thread": thread,
            "stop_event": stop_event,
            "interval": interval,
            "last_cars": [],
            "car_filter": old_filter,
            "alarm_enabled": old_alarm,
            "last_called_state": False,
            "zone_key": zone_key
        }
        thread.start()
        return True


def stop_monitoring(chat_id: int) -> bool:
    with sessions_lock:
        session = sessions.get(chat_id)
        if not session or not session["thread"].is_alive():
            sessions.pop(chat_id, None)
            return False
        session["stop_event"].set()
        session["thread"].join(timeout=2)
        sessions.pop(chat_id, None)
        return True


@bot.message_handler(commands=["start"])
@bot.message_handler(func=lambda message: message.text == "🚀 Старт")
def start(message):
    if is_user_blocked(message.chat.id):
        bot.reply_to(message, "❌ Доступ к боту ограничен.")
        return
        
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
        telebot.types.InlineKeyboardButton(
            "⏱ 3 мин", callback_data="int_3"
        ),
        telebot.types.InlineKeyboardButton(
            "⏱ 10 мин", callback_data="int_10"
        ),
        telebot.types.InlineKeyboardButton(
            "✍️ Свой интервал", callback_data="int_custom"
        ),
    )
    bot.send_message(
        chat_id,
        f"🚀 <b>Мониторинг границы {zone_name}</b>\nВыберите интервал опроса:",
        reply_markup=markup,
        parse_mode="HTML",
    )


@bot.message_handler(commands=["zone"])
@bot.message_handler(func=lambda message: message.text == "🌍 Зона")
def select_zone(message):
    """Выбор зоны ожидания"""
    chat_id = message.chat.id
    if is_user_blocked(chat_id):
        bot.reply_to(message, "❌ Доступ к боту ограничен.")
        return
    
    with sessions_lock:
        current_zone = sessions.get(chat_id, {}).get("zone_key", "brest")
        alarm_on = sessions.get(chat_id, {}).get("alarm_enabled", False)
    
    zone_name = get_zone_name(current_zone)
    bot.send_message(
        chat_id,
        f"🌍 <b>Текущая зона: {zone_name}</b>\nВыберите другую зону ожидания:",
        reply_markup=get_zone_keyboard(),
        parse_mode="HTML"
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("zone_"))
def handle_zone_selection(call):
    """Обработка выбора зоны"""
    chat_id = call.message.chat.id
    if is_user_blocked(chat_id):
        return
    
    try:
        zone_key = call.data.replace("zone_", "")
        if zone_key not in ZONES:
            bot.answer_callback_query(call.id, "❌ Неизвестная зона")
            return
        
        with sessions_lock:
            if chat_id not in sessions:
                sessions[chat_id] = {
                    "car_filter": None,
                    "last_cars": [],
                    "alarm_enabled": False,
                    "last_called_state": False,
                    "zone_key": zone_key
                }
            else:
                sessions[chat_id]["zone_key"] = zone_key
            
            alarm_on = sessions[chat_id].get("alarm_enabled", False)
        
        zone_name = get_zone_name(zone_key)
        bot.answer_callback_query(call.id, f"✅ Зона выбрана: {zone_name}")
        
        # Если мониторинг уже запущен, остановим его
        if is_monitoring(chat_id):
            stop_monitoring(chat_id)
        
        bot.send_message(
            chat_id,
            f"🌍 <b>Зона изменена на: {zone_name}</b>\nЗапустите мониторинг командой /start",
            reply_markup=get_main_keyboard(alarm_on, chat_id),
            parse_mode="HTML"
        )
    except Exception as e:
        bot.answer_callback_query(call.id, f"❌ Ошибка: {e}")


@bot.message_handler(commands=["users"])
@bot.message_handler(func=lambda message: message.text == "👥 Пользователи")
def show_users(message):
    if message.chat.id != ADMIN_CHAT_ID:
        return

    with db_lock:
        conn = sqlite3.connect("bot_users.db")
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id, username, first_name, last_name, joined_at, is_blocked FROM users")
        rows = cursor.fetchall()
        conn.close()

    if not rows:
        bot.reply_to(message, "📭 В базе пока нет ни одного пользователя.")
        return

    markup = telebot.types.InlineKeyboardMarkup()
    text = f"👥 <b>Список пользователей ({len(rows)}):</b>\n\n"
    for row in rows:
        chat_id, username, first_name, last_name, joined_at, is_blocked = row
        uname = f"@{username}" if username else "нет юзернейма"
        name = f"{first_name} {last_name}".strip()
        
        status_icon = "🔴 Заблокирован" if is_blocked == 1 else "✅ Активен"
        
        text += f"• <b>{html.escape(name)}</b> ({uname})\n  ID: <code>{chat_id}</code> | {joined_at} | {status_icon}\n\n"
        
        if chat_id != ADMIN_CHAT_ID:
            if is_blocked == 0:
                markup.add(telebot.types.InlineKeyboardButton(
                    f"🚫 Заблокировать {first_name}", callback_data=f"block_{chat_id}"
                ))
            else:
                markup.add(telebot.types.InlineKeyboardButton(
                    f"✅ Разблокировать {first_name}", callback_data=f"unblock_{chat_id}"
                ))

    bot.reply_to(message, text, parse_mode="HTML", reply_markup=markup if markup.keyboard else None)


@bot.callback_query_handler(func=lambda call: call.data.startswith("block_") or call.data.startswith("unblock_"))
def handle_block_toggle(call):
    if call.message.chat.id != ADMIN_CHAT_ID:
        return
    
    try:
        action, chat_id_str = call.data.split("_")
        target_chat_id = int(chat_id_str)
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
            except Exception:
                pass

        bot.answer_callback_query(call.id, "✅ Статус пользователя изменен!")
        show_users(call.message)
    except Exception as e:
        bot.answer_callback_query(call.id, f"❌ Ошибка: {e}")


@bot.callback_query_handler(func=lambda call: call.data in ["int_3", "int_10"])
def set_preset_interval(call):
    chat_id = call.message.chat.id
    if is_user_blocked(chat_id):
        return
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    interval = 3 if call.data == "int_3" else 10
    
    with sessions_lock:
        zone_key = sessions.get(chat_id, {}).get("zone_key", "brest")
    
    if not start_monitoring(chat_id, interval, zone_key):
        with sessions_lock:
            alarm_on = sessions.get(chat_id, {}).get("alarm_enabled", False)
        bot.send_message(
            chat_id,
            "⚠️ Мониторинг уже запущен!",
            reply_markup=get_main_keyboard(alarm_on, chat_id),
        )
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
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    with sessions_lock:
        alarm_on = sessions.get(chat_id, {}).get("alarm_enabled", False)

    if is_monitoring(chat_id):
        bot.send_message(
            chat_id,
            "⚠️ Мониторинг уже запущен!",
            reply_markup=get_main_keyboard(alarm_on, chat_id),
        )
        return
    msg = bot.send_message(
        chat_id, "✍️ Введите количество <b>минут</b> для интервала (число):", parse_mode="HTML"
    )
    bot.register_next_step_handler(msg, process_custom_step)


def process_custom_step(message):
    chat_id = message.chat.id
    if is_user_blocked(chat_id):
        return
    try:
        minutes = int(message.text.strip())
        if not (1 <= minutes <= 1440):
            raise ValueError()
    except (ValueError, AttributeError, TypeError):
        bot.send_message(
            chat_id, "❌ Ошибка: введите целое число от 1 до 1440."
        )
        return

    with sessions_lock:
        zone_key = sessions.get(chat_id, {}).get("zone_key", "brest")
        alarm_on = sessions.get(chat_id, {}).get("alarm_enabled", False)

    if not start_monitoring(chat_id, minutes, zone_key):
        with sessions_lock:
            alarm_on = sessions.get(chat_id, {}).get("alarm_enabled", False)
        bot.send_message(
            chat_id,
            "⚠️ Мониторинг уже запущен!",
            reply_markup=get_main_keyboard(alarm_on, chat_id),
        )
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
@bot.message_handler(func=lambda message: message.text == "🛑 Стоп")
def stop(message):
    chat_id = message.chat.id
    with sessions_lock:
        alarm_on = sessions.get(chat_id, {}).get("alarm_enabled", False)

    if stop_monitoring(chat_id):
        bot.reply_to(
            message,
            "🛑 <b>Мониторинг остановлен.</b>",
            reply_markup=get_main_keyboard(alarm_on, chat_id),
            parse_mode="HTML",
        )
    else:
        bot.reply_to(
            message,
            "⚠️ Мониторинг не активен.",
            reply_markup=get_main_keyboard(alarm_on, chat_id),
        )


@bot.message_handler(commands=["alarm_on"])
@bot.message_handler(func=lambda message: message.text == "🔔 Сирена: ВКЛ")
def enable_alarm(message):
    chat_id = message.chat.id
    with sessions_lock:
        if chat_id not in sessions:
            sessions[chat_id] = {"car_filter": None, "last_cars": [], "alarm_enabled": False, "zone_key": "brest"}
        sessions[chat_id]["alarm_enabled"] = True

    bot.reply_to(
        message,
        "🔔 <b>Громкая сирена включена!</b>",
        reply_markup=get_main_keyboard(True, chat_id),
        parse_mode="HTML"
    )


@bot.message_handler(commands=["alarm_off"])
@bot.message_handler(func=lambda message: message.text == "🔕 Сирена: ВЫКЛ")
def disable_alarm(message):
    chat_id = message.chat.id
    with sessions_lock:
        if chat_id not in sessions:
            sessions[chat_id] = {"car_filter": None, "last_cars": [], "alarm_enabled": False, "zone_key": "brest"}
        sessions[chat_id]["alarm_enabled"] = False

    bot.reply_to(
        message,
        "🔕 <b>Громкая сирена выключена.</b>",
        reply_markup=get_main_keyboard(False, chat_id),
        parse_mode="HTML"
    )


@bot.message_handler(commands=["filter"])
@bot.message_handler(func=lambda message: message.text == "🚗 Фильтр машины")
def ask_car_filter(message):
    chat_id = message.chat.id
    with sessions_lock:
        alarm_on = sessions.get(chat_id, {}).get("alarm_enabled", False)

    msg = bot.send_message(
        chat_id,
        "✍️ Введите гос.номер машины для постоянного отслеживания в отчетах:",
        reply_markup=get_main_keyboard(alarm_on, chat_id)
    )
    bot.register_next_step_handler(msg, save_car_filter_step)


def save_car_filter_step(message):
    chat_id = message.chat.id
    text = message.text.strip()
    
    if text.startswith("/") or text in ["🚀 Старт", "🛑 Стоп", "🚗 Фильтр машины", "❌ Снять фильтр", "🔔 Сирена: ВКЛ", "🔕 Сирена: ВЫКЛ", "👥 Пользователи", "🌍 Зона"]:
        return

    with sessions_lock:
        if chat_id not in sessions:
            sessions[chat_id] = {"car_filter": None, "last_cars": [], "alarm_enabled": False, "zone_key": "brest"}
        sessions[chat_id]["car_filter"] = text
        alarm_on = sessions[chat_id]["alarm_enabled"]

    bot.reply_to(
        message,
        f"✅ Фильтр по машине <b>{html.escape(text)}</b> успешно установлен!",
        reply_markup=get_main_keyboard(alarm_on, chat_id),
        parse_mode="HTML"
    )


@bot.message_handler(commands=["clear"])
@bot.message_handler(func=lambda message: message.text == "❌ Снять фильтр")
def remove_car_filter(message):
    chat_id = message.chat.id
    with sessions_lock:
        if chat_id in sessions:
            sessions[chat_id]["car_filter"] = None
            alarm_on = sessions[chat_id].get("alarm_enabled", False)
        else:
            alarm_on = False

    bot.reply_to(
        message,
        "❌ Фильтр по машине отключен.",
        reply_markup=get_main_keyboard(alarm_on, chat_id),
        parse_mode="HTML"
    )


@bot.message_handler(content_types=['text'])
def handle_car_search(message):
    chat_id = message.chat.id
    text = message.text.strip()

    if text.startswith("/") or text in ["🚀 Старт", "🛑 Стоп", "🚗 Фильтр машины", "❌ Снять фильтр", "🔔 Сирена: ВКЛ", "🔕 Сирена: ВЫКЛ", "👥 Пользователи", "🌍 Зона"]:
        return

    with sessions_lock:
        session = sessions.get(chat_id)
        if not session or not session.get("last_cars"):
            alarm_on = session.get("alarm_enabled", False) if session else False
            bot.reply_to(
                message,
                "⚠️ Сначала запустите мониторинг кнопкой «🚀 Старт».",
                reply_markup=get_main_keyboard(alarm_on, chat_id)
            )
            return
        cars = session["last_cars"]

    search_query = text.replace(" ", "").lower()
    found_car = None
    position = -1

    for idx, car in enumerate(cars, start=1):
        car_number = str(
            car.get("regnum") or 
            car.get("number") or 
            car.get("nzp") or 
            car.get("reg_number") or ""
        ).replace(" ", "").lower()
        
        if search_query in car_number or search_query == car_number:
            found_car = car
            position = idx
            break

    if found_car:
        reg_num = found_car.get("regnum") or found_car.get("number") or found_car.get("nzp") or text
        reg_date = found_car.get("registration_date", "-")
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


_bot_initialized = False


def run_telegram_bot():
    global _bot_initialized
    if _bot_initialized:
        return
    _bot_initialized = True

    time.sleep(3)
    try:
        requests.get(
            f"https://api.telegram.org/bot{API_TOKEN}/deleteWebhook?drop_pending_updates=true",
            timeout=10,
        )
    except Exception:
        pass

    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=30)
        except Exception as e:
            time.sleep(5)


threading.Thread(target=run_telegram_bot, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)