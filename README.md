# Telegram Granica Bot

Telegram-бот мониторинга очереди на КПП **Брест** (API [belarusborder.by](https://belarusborder.by)).

Бот периодически опрашивает очередь легковых авто и присылает сводку в чат:
- машин в очереди
- сколько уже стоит первая
- оценка ожидания для новой записи (по темпу пропуска)
- статистика за час / сутки

Каждый чат работает независимо.

## Быстрый старт

### 1. Клонировать

```bash
git clone https://github.com/CibAG/Telegram-Granica-Bot.git
cd Telegram-Granica-Bot
```

### 2. Виртуальное окружение и зависимости

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
```

### 3. Токен бота

Создайте бота у [@BotFather](https://t.me/BotFather), затем:

```bash
# Windows
copy .env.example .env

# Linux / macOS
cp .env.example .env
```

В `.env` укажите:

```
API_TOKEN=123456:ABC-DEF...
```

Файл `.env` не коммитится.

### 4. Конфиг КПП (по желанию)

`bot_config.json` — URL API и checkpoint (без секретов):

```json
{
  "base_url": "https://belarusborder.by/info",
  "checkpoint_id": "a9173a85-3fc0-424c-84f0-defa632481e4",
  "query_token": "test"
}
```

### 5. Запуск

```bash
python bot.py
```

Windows: можно запустить `start_bot.bat`.

В Telegram: `/start` или кнопка «Старт» → интервал → отчёты. Остановка: `/stop`.

## Структура

```
├── bot.py              # основной бот
├── bot_config.json     # checkpoint / API (без токена)
├── .env.example        # образец секретов
├── requirements.txt
├── start_bot.bat       # запуск на Windows
└── README.md
```

## Оценка ожидания

- **Первая стоит** — `сейчас − registration_date` первой машины в `carLiveQueue`
- **Оценка для вас** — `машин_в_очереди / carLastHour × 60` мин  
  (если за час 0 — по суточному темпу `carLastDay / 24`)

## Безопасность

- Не публикуйте `.env` и токен бота
- Если токен уже светился — перевыпустите в @BotFather
