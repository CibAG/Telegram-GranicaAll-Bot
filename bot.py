from datetime import datetime
import html
import json
import os
import threading
import time
from flask import Flask

import requests
import telebot
from dotenv import load_dotenv

load_dotenv()

sessions_lock = threading.Lock()
sessions: dict[int, dict] = {}


def load_config(path="bot_config.json"):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


config = load_config()
API_TOKEN = os.getenv("API_TOKEN") or config.get("api_token")
if not API_TOKEN:
    raise SystemExit(
        "API_TOKEN не задан. Создайте файл .env (см. .env.example) "
        "или задайте переменную окружения API_TOKEN."
    )

BASE = config.get("base_url", "https://belarusborder.by/info")
CHECKPOINT = config.get("checkpoint_id")
QUERY_TOKEN = config.get("query_token", "test")

bot = telebot.TeleBot(API_TOKEN)

# --- FLASK СЕРВЕР ДЛЯ RENDER ---
app = Flask(__name__)


@app.route("/")
def home():
    return "Bot is running!"


def run_flask():
    app.run(host="0.0.0.0", port=10000)


def get_main_keyboard():
    markup = telebot.types.ReplyKeyboardMarkup(
        resize_keyboard=True, row_width=2
    )
    btn_start = telebot.types.KeyboardButton("🚀 Старт")
    btn_stop = telebot.types.KeyboardButton("🛑 Стоп")
    markup.add(btn_start, btn_stop)
    return markup


def is_monitoring(chat_id: int) -> bool:
    session = sessions.get(chat_id)
    return bool(session and session["thread"].is_alive())


def fetch_data():
    try:
        # Явно отключаем прокси, чтобы Render не заворачивал запросы
        proxies = {"http": None, "https": None}

        stats = requests.get(
            f"{BASE}/monitoring/statistics",
            params={"token": QUERY_TOKEN, "checkpointId": CHECKPOINT},
            timeout=20,
            proxies=proxies,
        ).json()

        queue = requests.get(
            f"{BASE}/monitoring-new",
            params={"token": QUERY_TOKEN, "checkpointId": CHECKPOINT},
            timeout=20,
            proxies=proxies,
        ).json()

        cars = queue.get("carLiveQueue", [])

        if cars:
            first = min(
                cars,
                key=lambda x: datetime.strptime(
                    x["registration_date"], "%H:%M:%S %d.%m.%Y"
                ),
            )
            reg_time = datetime.strptime(
                first["registration_date"], "%H:%M:%S %d.%m.%Y"
            )
            wait_minutes = int(
                (datetime.now() - reg_time).total_seconds() / 60
            )
            registration_date = first["registration_date"]
            changed_date = first.get("changed_date", "-")
        else:
            registration_date = "-"
            changed_date = "-"
            wait_minutes = 0

        total = len(cars)
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
        }
    except Exception as e:
        return {"error": str(e)}


def format_estimate_html(minutes):
    if minutes is None:
        text = "н/д (нет данных о пропуске)"
    else:
        hours, mins = divmod(max(0, minutes), 60)
        if hours:
            text = f"{minutes} мин. (~{hours} ч {mins} мин.)"
        else:
            text = f"{minutes} мин."

    safe = html.escape(text)
    return (
        f'<a href="https://mon.declarant.by/#/zone/brest-bts">'
        f"<b><u>{safe}</u></b></a>"
    )


def build_report(d):
    return (
        "📊 <b>Мониторинг границы Брест</b>\n"
        "━━━━━━━━━━━━━━━\n"
        f"🚗 Машин в очереди: {html.escape(str(d['total']))}\n"
        f"🕒 Первая стоит: {html.escape(str(d['wait']))} мин.\n"
        f"⏳ Оценка для вас: {format_estimate_html(d['estimate'])}\n"
        f"📉 За час: {html.escape(str(d['stats_hour']))} маш.\n"
        f"📅 За сутки: {html.escape(str(d['stats_day']))} маш.\n"
        f"📅 Дата 1-го авто: {html.escape(str(d['reg_date']))}\n"
        f"🔄 Статус изменён: {html.escape(str(d['changed']))}\n"
        '🔗 <a href="https://mon.declarant.by/#/zone/brest-bts">'
        "Сайт мониторинга Брест</a>\n"
        "━━━━━━━━━━━━━━━\n"
    )


def monitoring_loop(chat_id: int, stop_event: threading.Event, interval: int):
    while not stop_event.is_set():
        data = fetch_data()
        if "error" in data:
            bot.send_message(
                chat_id,
                f"❌ Ошибка мониторинга: {data['error']}",
            )
        else:
            bot.send_message(
                chat_id,
                build_report(data),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

        for _ in range(interval * 60):
            if stop_event.is_set():
                break
            time.sleep(1)


def start_monitoring(chat_id: int, interval: int) -> bool:
    with sessions_lock:
        if is_monitoring(chat_id):
            return False

        stop_event = threading.Event()
        thread = threading.Thread(
            target=monitoring_loop,
            args=(chat_id, stop_event, interval),
            daemon=True,
        )
        sessions[chat_id] = {
            "thread": thread,
            "stop_event": stop_event,
            "interval": interval,
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
    chat_id = message.chat.id
    with sessions_lock:
        if is_monitoring(chat_id):
            bot.reply_to(
                message,
                "⚠️ Мониторинг уже запущен!",
                reply_markup=get_main_keyboard(),
            )
            return

    markup = telebot.types.InlineKeyboardMarkup()
    btn10 = telebot.types.InlineKeyboardButton(
        "⏱ 10 мин", callback_data="int_10"
    )
    btn_custom = telebot.types.InlineKeyboardButton(
        "✍️ Свой интервал", callback_data="int_custom"
    )
    markup.add(btn10, btn_custom)

    bot.send_message(
        chat_id,
        "🚀 <b>Мониторинг границы Брест</b>\nВыберите интервал опроса:",
        reply_markup=markup,
        parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda call: call.data == "int_10")
def set_10(call):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    chat_id = call.message.chat.id

    if not start_monitoring(chat_id, 10):
        bot.send_message(
            chat_id,
            "⚠️ Мониторинг уже запущен!",
            reply_markup=get_main_keyboard(),
        )
        return

    bot.send_message(
        chat_id,
        "✅ Запуск: каждые 10 минут. Первый отчет отправляется...",
        reply_markup=get_main_keyboard(),
    )


@bot.callback_query_handler(func=lambda call: call.data == "int_custom")
def set_custom(call):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    chat_id = call.message.chat.id

    with sessions_lock:
        if is_monitoring(chat_id):
            bot.send_message(
                chat_id,
                "⚠️ Мониторинг уже запущен!",
                reply_markup=get_main_keyboard(),
            )
            return

    msg = bot.send_message(
        chat_id, "✍️ Введите количество <b>минут</b> (число):", parse_mode="HTML"
    )
    bot.register_next_step_handler(msg, process_custom_step)


def process_custom_step(message):
    chat_id = message.chat.id
    try:
        minutes = int(message.text.strip())
        if minutes < 1 or minutes > 1440:
            raise ValueError()
    except (ValueError, AttributeError, TypeError):
        bot.send_message(
            chat_id, "❌ Ошибка: введите целое число от 1 до 1440."
        )
        return

    if not start_monitoring(chat_id, minutes):
        bot.send_message(
            chat_id,
            "⚠️ Мониторинг уже запущен!",
            reply_markup=get_main_keyboard(),
        )
        return

    bot.send_message(
        chat_id,
        f"✅ Запуск: каждые {minutes} мин. Первый отчет отправляется...",
        reply_markup=get_main_keyboard(),
    )


@bot.message_handler(commands=["stop"])
@bot.message_handler(func=lambda message: message.text == "🛑 Стоп")
def stop(message):
    chat_id = message.chat.id
    if stop_monitoring(chat_id):
        bot.reply_to(
            message,
            "🛑 <b>Мониторинг остановлен.</b>",
            reply_markup=get_main_keyboard(),
            parse_mode="HTML",
        )
    else:
        bot.reply_to(
            message,
            "⚠️ Мониторинг не активен.",
            reply_markup=get_main_keyboard(),
        )


if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    print("Flask server started in background thread.")

    try:
        requests.get(
            f"https://api.telegram.org/bot{API_TOKEN}/deleteWebhook",
            timeout=10,
        )
    except requests.RequestException as e:
        print(f"Webhook delete failed: {e}")

    while True:
        try:
            print(f"[{datetime.now()}] Bot started and ready...")
            bot.polling(none_stop=True, interval=0, timeout=20)
        except Exception as e:
            print(f"[{datetime.now()}] Error: {e}. Restart in 5s...")
            time.sleep(5)