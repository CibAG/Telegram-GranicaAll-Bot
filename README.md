Telegram Granica Bot
Telegram-бот мониторинга очереди на КПП Брест (API belarusborder.by).

Бот периодически опрашивает очередь легковых авто и присылает сводку в чат:

машин в очереди

сколько уже стоит первая

оценка ожидания для новой записи (по темпу пропуска)

статистика за час / сутки

Постоянный фильтр по машине (опционально, отображает статус и позицию конкретного авто в каждом отчете)

Каждый чат работает независимо.

Быстрый старт
1. Клонировать
git clone https://github.com/CibAG/Telegram-Granica-Bot.git
cd Telegram-Granica-Bot

2. Виртуальное окружение и зависимости
python -m venv .venv

Windows
.venv\Scripts\activate

Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt

3. Токен бота
Создайте бота у @BotFather, затем:

Windows
copy .env.example .env

Linux / macOS
cp .env.example .env

В .env укажите:

API_TOKEN=123456:ABC-DEF...

Файл .env не коммитится.

4. Конфиг КПП (по желанию)
bot_config.json — URL API и checkpoint (без секретов):

{
"base_url": "https://belarusborder.by/info",
"checkpoint_id": "a9173a85-3fc0-424c-84f0-defa632481e4",
"query_token": "test"
}

5. Запуск
python bot.py

Windows: можно запустить start_bot.bat.

Управление в Telegram:
/start или кнопка «🚀 Старт» — выбор интервала (3 мин, 10 мин или свой) и запуск мониторинга.

/stop или кнопка «🛑 Стоп» — остановка фоновых отчетов.

Кнопка «🚗 Фильтр машины» — ввод гос. номера для постоянного отслеживания (позиция и статус машины будут автоматически добавляться в каждый плановый отчет).

Кнопка «❌ Снять фильтр» — отключение постоянного отслеживания авто.

Отправка текста (номера) в чат — разовый поиск позиции машины в текущей очереди.

Структура
├── bot.py              # основной бот
├── bot_config.json     # checkpoint / API (без токена)
├── .env.example        # образец секретов
├── requirements.txt
├── start_bot.bat       # запуск на Windows
└── README.md

Оценка ожидания
Первая стоит — сейчас − registration_date первой машины в carLiveQueue

Оценка для вас — машин_в_очереди / carLastHour × 60 мин

(если за час 0 — по суточному темпу carLastDay / 24)

Безопасность
Не публикуйте .env и токен бота

Если токен уже светился — перевыпустите в @BotFather