from datetime import datetime, timezone, timedelta
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

# Принудительно сбрасываем прокси-переменные окружения Render
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)
os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)

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
    raise SystemExit("API_TOKEN не задан.")

BASE = config.get("base_url", "https://belarusborder.by/info")
CHECKPOINT = config.get("checkpoint_id")
QUERY_TOKEN = config.get("query_token", "test")

bot = telebot.TeleBot(API_TOKEN)

app = Flask(__name__)


@app.route("/")
def home():
    return "Bot is active", 200


def get_main_keyboard():
    markup = telebot.types.ReplyKeyboardMarkup(
        resize_keyboard=True, row_width=2
    )
    markup.add(
        telebot.types.KeyboardButton("🚀 Старт"),
        telebot.types.KeyboardButton("🛑 Стоп"),
    )
    return markup


def get_report_keyboard():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton(
            "🔗 Сайт мониторинга Брест",
            url="https://mon.declarant.by/#/zone/brest-bts"
        )
    )
    return markup


def is_monitoring(chat_id: int) -> bool:
    with sessions_lock:
        session = sessions.get(chat_id)
        return bool(session and session["thread"].is_alive())


def fetch_data():
    try:
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
        "━━━━━━━━━━━━━━━\n"
        "💡 <i>Отправьте номер машины в чат, чтобы узнать её позицию!</i>"
    )


def monitoring_loop(chat_id: int, stop_event: threading.Event, interval: int):
    print(f"[DEBUG] Поток мониторинга для чата {chat_id} запущен с интервалом {interval} мин.")
    while not stop_event.is_set():
        print(f"[DEBUG] Начинаем опрос сервера для чата {chat_id}...")
        data = fetch_data()
        if "error" in data:
            print(f"[DEBUG] Ошибка при запросе данных: {data['error']}")
            bot.send_message(chat_id, f"❌ Ошибка мониторинга: {data['error']}")
        else:
            print(f"[DEBUG] Данные успешно получены. Машин: {data.get('total')}. Отправляем отчет...")
            with sessions_lock:
                if chat_id in sessions:
                    sessions[chat_id]["last_cars"] = data.get("sorted_cars", [])

            try:
                bot.send_message(
                    chat_id,
                    build_report(data),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=get_report_keyboard(),
                )
                print(f"[DEBUG] Отчет успешно отправлен в чат {chat_id}.")
            except Exception as e:
                print(f"[DEBUG] Ошибка при отправке сообщения в Telegram: {e}")

        print(f"[DEBUG] Засыпаем на {interval} минут...")
        for _ in range(interval * 60):
            if stop_event.is_set():
                print(f"[DEBUG] Получен сигнал остановки во время сна.")
                break
            time.sleep(1)
    print(f"[DEBUG] Поток мониторинга для чата {chat_id} завершил работу.")


def start_monitoring(chat_id: int, interval: int) -> bool:
    with sessions_lock:
        session = sessions.get(chat_id)
        if session and session["thread"].is_alive():
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
            "last_cars": []
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
    if is_monitoring(chat_id):
        bot.reply_to(
            message,
            "⚠️ Мониторинг уже запущен!",
            reply_markup=get_main_keyboard(),
        )
        return

    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton(
            "⏱ 10 мин", callback_data="int_10"
        ),
        telebot.types.InlineKeyboardButton(
            "✍️ Свой интервал", callback_data="int_custom"
        ),
    )
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
        if not (1 <= minutes <= 1440):
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


@bot.message_handler(content_types=['text'])
def handle_car_search(message):
    chat_id = message.chat.id
    text = message.text.strip()

    if text in ["🚀 Старт", "🛑 Стоп"]:
        return

    with sessions_lock:
        session = sessions.get(chat_id)
        if not session or not session.get("last_cars"):
            bot.reply_to(
                message,
                "⚠️ Сначала запустите мониторинг кнопкой «🚀 Старт», чтобы загрузить актуальную очередь.",
                reply_markup=get_main_keyboard()
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
        response_text = (
            f"🔍 <b>Результат поиска по номеру:</b> {html.escape(str(reg_num))}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🚗 <b>Позиция в очереди:</b> <b>{position}</b>-я с начала\n"
            f"📅 <b>Дата регистрации:</b> {html.escape(str(reg_date))}\n"
            f"━━━━━━━━━━━━━━━"
        )
    else:
        response_text = (
            f"❌ Машина с номером <b>{html.escape(text)}</b> не найдена в текущей активной очереди.\n"
            "Убедитесь, что номер введен правильно."
        )

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
            print("[DEBUG] Запуск infinity_polling()...")
            bot.infinity_polling(timeout=30, long_polling_timeout=30)
        except Exception as e:
            print(f"[DEBUG] Ошибка в polling: {e}")
            time.sleep(5)


threading.Thread(target=run_telegram_bot, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)