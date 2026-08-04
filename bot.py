# -*- coding: utf-8 -*-
"""
Telegram-бот, подключённый к Claude (Anthropic API).
Каждое сообщение пользователя пересылается в Claude, ответ
присылается обратно в Telegram. Хранит историю переписки на
каждого пользователя, чтобы Claude помнил контекст диалога.

Нужно ДВА разных ключа:
1. BOT_TOKEN — токен вашего Telegram-бота (от @BotFather)
2. ANTHROPIC_API_KEY — ключ доступа к Claude API (получить на
   console.anthropic.com — это ОТДЕЛЬНЫЙ сервис от чата claude.ai,
   там нужно завести аккаунт разработчика и пополнить баланс,
   API платный по токенам, но недорого для личного бота)

Работает в двух режимах (как и прошлые боты): long polling (Termux)
и webhook (Render).
"""
import logging
import os
import random
import sqlite3
import threading
import time
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from logging.handlers import RotatingFileHandler

import requests

BOT_TOKEN = os.getenv("BOT_TOKEN", "8622811697:AAF_CAod3oWyCo9S4mldtib78lOpinJw8S4")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "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")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
DB_PATH = "claude_tg_bot.db"
STATE_LOCK = threading.Lock()
DB_LOCK = threading.Lock()

MAX_HISTORY_MESSAGES = 20   # сколько последних сообщений помнить (контекст)
SYSTEM_PROMPT = (
    "Ты дружелюбный ИИ-ассистент, работающий внутри Telegram-бота. "
    "Отвечай по существу, без лишней воды. Форматирование — обычным "
    "текстом, без Markdown-разметки (Telegram её не всегда красиво "
    "показывает), можно использовать эмодзи для выразительности."
)

# ============================================================
# ЛОГИРОВАНИЕ
# ============================================================

logger = logging.getLogger("claude_tg_bot")
logger.setLevel(logging.INFO)
_fh = RotatingFileHandler("bot.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
_ch = logging.StreamHandler()
_ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_fh)
logger.addHandler(_ch)

# ============================================================
# БАЗА ДАННЫХ (история переписки)
# ============================================================


def init_db():
    with closing(sqlite3.connect(DB_PATH)) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id INTEGER, role TEXT, content TEXT,
                        ts TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS users (
                        user_id INTEGER PRIMARY KEY, username TEXT DEFAULT '',
                        messages_count INTEGER DEFAULT 0,
                        ts TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS groups (
                        chat_id INTEGER PRIMARY KEY, title TEXT DEFAULT '',
                        ts TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.commit()


def register_group(chat_id, title):
    with DB_LOCK, closing(sqlite3.connect(DB_PATH)) as c:
        c.execute("INSERT INTO groups (chat_id, title) VALUES (?, ?) "
                   "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title",
                   (chat_id, title or ""))
        c.commit()


def get_all_groups():
    with closing(sqlite3.connect(DB_PATH)) as c:
        return [r[0] for r in c.execute("SELECT chat_id FROM groups").fetchall()]


def touch_user(user_id, username):
    with DB_LOCK, closing(sqlite3.connect(DB_PATH)) as c:
        c.execute("INSERT INTO users (user_id, username, messages_count) VALUES (?, ?, 1) "
                   "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, "
                   "messages_count=messages_count+1", (user_id, username or ""))
        c.commit()


def add_history(chat_id, role, content):
    with DB_LOCK, closing(sqlite3.connect(DB_PATH)) as c:
        c.execute("INSERT INTO history (chat_id, role, content) VALUES (?, ?, ?)",
                   (chat_id, role, content))
        c.commit()


def get_history(chat_id, limit=MAX_HISTORY_MESSAGES):
    with closing(sqlite3.connect(DB_PATH)) as c:
        rows = c.execute(
            "SELECT role, content FROM history WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (chat_id, limit)).fetchall()
        return list(reversed(rows))  # в хронологическом порядке


def clear_history(chat_id):
    with DB_LOCK, closing(sqlite3.connect(DB_PATH)) as c:
        c.execute("DELETE FROM history WHERE chat_id=?", (chat_id,))
        c.commit()


def get_admin_stats():
    with closing(sqlite3.connect(DB_PATH)) as c:
        users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        total_msgs = c.execute("SELECT COALESCE(SUM(messages_count),0) FROM users").fetchone()[0]
        return users, total_msgs

# ============================================================
# GROQ API (бесплатно, без карты) — OpenAI-совместимый формат
# ============================================================


def ask_ai(chat_id, user_text):
    """Отправляет историю диалога + новое сообщение в Groq, возвращает ответ."""
    history = get_history(chat_id)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages += [{"role": role, "content": content} for role, content in history]
    messages.append({"role": "user", "content": user_text})

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "max_tokens": 1024,
        "messages": messages,
    }
    try:
        r = requests.post(GROQ_URL, headers=headers, json=payload, timeout=60)
        if r.status_code != 200:
            logger.error(f"Groq API ошибка {r.status_code}: {r.text[:300]}")
            if r.status_code == 401:
                return "⚠️ Неверный GROQ_API_KEY — проверьте, что ключ указан верно."
            if r.status_code == 429:
                return "⚠️ Превышен бесплатный лимит запросов на сейчас. Попробуйте через минуту."
            return f"⚠️ Ошибка при обращении к нейросети (код {r.status_code}). Попробуйте позже."
        data = r.json()
        choices = data.get("choices", [])
        if not choices:
            return "⚠️ Пустой ответ от нейросети."
        return choices[0]["message"]["content"]
    except requests.exceptions.Timeout:
        return "⚠️ Нейросеть долго не отвечает (таймаут). Попробуйте ещё раз."
    except Exception as e:
        logger.error(f"Ошибка запроса к Groq: {e}")
        return f"⚠️ Ошибка соединения: {e}"

# ============================================================
# TELEGRAM API
# ============================================================


def api_call(method, **params):
    try:
        r = requests.post(API_URL + method, json=params, timeout=40)
        return r.json()
    except Exception as e:
        logger.error(f"api_call({method}) ошибка: {e}")
        return {"ok": False, "description": str(e)}


def send(chat_id, text):
    # Telegram режет сообщения длиннее ~4096 символов — режем на части
    for i in range(0, len(text), 4000):
        chunk = text[i:i + 4000]
        api_call("sendMessage", chat_id=chat_id, text=chunk)


def typing(chat_id):
    api_call("sendChatAction", chat_id=chat_id, action="typing")

# ============================================================
# ЛОГИКА БОТА
# ============================================================

WELCOME_TEXT = (
    "👋 Привет! Я бот-ассистент на базе ИИ (Llama через Groq, бесплатно).\n\n"
    "Просто пишите мне любой вопрос — отвечу как обычный ИИ-чат. "
    "Я помню контекст беседы в рамках этого чата.\n\n"
    "Добавьте меня в группу — иногда буду сам писать и предлагать поиграть!\n\n"
    "/games — список мини-игр и доп. команд\n"
    "/reset — забыть историю переписки и начать с чистого листа\n"
    "/help — эта справка"
)

HELP_TEXT = (
    "ℹ️ Как пользоваться:\n\n"
    "Просто напишите сообщение — я перешлю его в ИИ и пришлю ответ.\n\n"
    "/reset — очистить историю диалога\n"
    "/games — список мини-игр и доп. команд\n"
    "/start — приветствие\n"
    "/help — эта справка"
)

GAMES_TEXT = (
    "🎮 МИНИ-ИГРЫ И ДОП. КОМАНДЫ\n\n"
    "🔢 /guess — угадай число от 1 до 100\n"
    "✊✋✌️ /rps — камень-ножницы-бумага против бота\n"
    "🎲 /dice — бросить кубик (анимация)\n"
    "🎯 /dart — бросить дротик (анимация)\n"
    "🎰 /slot — крутить слот-машину (анимация)\n"
    "🪙 /coin — подбросить монетку\n"
    "🔮 /8ball вопрос — магический шар предскажет ответ\n"
    "😂 /joke — случайная шутка\n"
    "💡 /fact — случайный интересный факт\n\n"
    "В группах я иногда сам пишу что-нибудь — просто отвечайте, "
    "с радостью пообщаюсь!"
)

CASUAL_GROUP_MESSAGES = [
    "👋 Всем привет! Как ваши дела?",
    "💭 Что интересного происходит у вас сегодня?",
    "🎮 Соскучились по игре? Наберите /games — там есть чем заняться.",
    "☕ Как настроение у чата? Пишите, если что-то обсудить хочется.",
    "🤔 Задайте мне любой вопрос — с радостью отвечу!",
    "🎲 Хотите сыграть в угадайку? Наберите /guess — загадаю число от 1 до 100.",
    "✨ Просто заглянул поздороваться. Как у всех дела?",
]

JOKES = [
    "Программист заходит в бар. Заказывает 1 пиво. Заказывает 0 пива. "
    "Заказывает 99999999 пива. Заказывает ящик ящериц. Бар взрывается.",
    "— Почему программисты путают Хэллоуин и Рождество?\n— Потому что OCT 31 == DEC 25.",
    "Мой код работает, и я не знаю почему.\nМой код не работает, и я не знаю почему.",
    "Есть только 10 типов людей: те, кто понимает двоичную систему, и те, кто нет.",
    "— Сколько программистов нужно, чтобы вкрутить лампочку?\n— Ни одного, это аппаратная проблема.",
    "Оптимист говорит: стакан наполовину полон.\nПессимист: наполовину пуст.\nПрограммист: стакан в два раза больше, чем нужно.",
    "Лучший способ ускорить программу — убедить пользователя, что она и так быстрая.",
]

FACTS = [
    "💡 Первый компьютерный баг был в буквальном смысле насекомым — мотыльком, застрявшим в реле в 1947 году.",
    "💡 Слово 'робот' придумал чешский писатель Карел Чапек в 1920 году в пьесе R.U.R.",
    "💡 Первая версия языка Python появилась в 1991 году, названа в честь шоу Monty Python, а не змеи.",
    "💡 В среднем человек моргает 15-20 раз в минуту, но во время работы за компьютером — заметно реже.",
    "💡 Первое доменное имя в истории интернета — symbolics.com, зарегистрировано в 1985 году.",
    "💡 Осьминоги имеют три сердца и голубую кровь.",
    "💡 Мёд не портится — археологи находили съедобный мёд возрастом более 3000 лет.",
]

EIGHT_BALL_ANSWERS = [
    "Да, определённо.", "Совершенно точно да.", "Без сомнений.",
    "Скорее да, чем нет.", "Знаки говорят — да.",
    "Спроси позже.", "Лучше не отвечать сейчас.", "Сложно сказать, попробуй ещё раз.",
    "Не рассчитывай на это.", "Мой ответ — нет.", "Сомнительно.",
    "Однозначно нет.",
]

RPS_CHOICES = {"rock": "✊", "paper": "✋", "scissors": "✌️"}
RPS_NAMES = {"rock": "камень", "paper": "бумага", "scissors": "ножницы"}
RPS_BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}

GUESS_GAMES = {}  # chat_id -> {"secret": int, "attempts": int}


def handle_message(msg):
    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    username = msg["from"].get("username") or msg["from"].get("first_name", "")
    text = msg.get("text", "").strip()
    chat_type = msg["chat"].get("type", "private")

    if chat_type in ("group", "supergroup"):
        register_group(chat_id, msg["chat"].get("title", ""))

    if not text:
        return

    if text == "/start":
        send(chat_id, WELCOME_TEXT)
        return

    if text == "/help":
        send(chat_id, HELP_TEXT)
        return

    if text == "/games":
        send(chat_id, GAMES_TEXT)
        return

    if text == "/reset":
        clear_history(chat_id)
        send(chat_id, "🧹 История диалога очищена. Начинаем с чистого листа.")
        return

    if text == "/admin":
        if ADMIN_ID == 0 or user_id != ADMIN_ID:
            send(chat_id, "⛔ Только для администратора.")
            return
        users, total_msgs = get_admin_stats()
        groups_count = len(get_all_groups())
        send(chat_id, f"👑 Пользователей: {users}\nГрупп: {groups_count}\nВсего сообщений: {total_msgs}")
        return

    # ---------- Мини-игры и доп. команды ----------

    if text == "/guess":
        secret = random.randint(1, 100)
        GUESS_GAMES[chat_id] = {"secret": secret, "attempts": 0}
        send(chat_id, "🔢 Загадал число от 1 до 100. Пишите числа, буду подсказывать больше/меньше!")
        return

    if chat_id in GUESS_GAMES and text.lstrip("-").isdigit():
        game = GUESS_GAMES[chat_id]
        guess = int(text)
        game["attempts"] += 1
        if guess == game["secret"]:
            send(chat_id, f"🎉 Угадали за {game['attempts']} попыток! Число было {game['secret']}. "
                          f"Хотите ещё раз — /guess")
            del GUESS_GAMES[chat_id]
        elif guess < game["secret"]:
            send(chat_id, "📈 Больше!")
        else:
            send(chat_id, "📉 Меньше!")
        return

    if text == "/rps":
        kb = [[
            {"text": "✊ Камень", "callback_data": "rps:rock"},
            {"text": "✋ Бумага", "callback_data": "rps:paper"},
            {"text": "✌️ Ножницы", "callback_data": "rps:scissors"},
        ]]
        api_call("sendMessage", chat_id=chat_id, text="Выбирайте:",
                 reply_markup=json.dumps({"inline_keyboard": kb}))
        return

    if text == "/dice":
        api_call("sendDice", chat_id=chat_id, emoji="🎲")
        return
    if text == "/dart":
        api_call("sendDice", chat_id=chat_id, emoji="🎯")
        return
    if text == "/slot":
        api_call("sendDice", chat_id=chat_id, emoji="🎰")
        return

    if text == "/coin":
        result = random.choice(["🪙 Орёл!", "🪙 Решка!"])
        send(chat_id, result)
        return

    if text.startswith("/8ball"):
        question = text[len("/8ball"):].strip()
        if not question:
            send(chat_id, "Задайте вопрос после команды, например:\n/8ball выйдет ли из этого толк?")
            return
        send(chat_id, f"🔮 {random.choice(EIGHT_BALL_ANSWERS)}")
        return

    if text == "/joke":
        send(chat_id, "😂 " + random.choice(JOKES))
        return

    if text == "/fact":
        send(chat_id, random.choice(FACTS))
        return

    touch_user(user_id, username)
    typing(chat_id)

    reply = ask_ai(chat_id, text)

    add_history(chat_id, "user", text)
    add_history(chat_id, "assistant", reply)

    send(chat_id, reply)


def handle_callback(cq):
    chat_id = cq["message"]["chat"]["id"]
    message_id = cq["message"]["message_id"]
    username = cq["from"].get("username") or cq["from"].get("first_name", "Игрок")
    data = cq["data"]
    api_call("answerCallbackQuery", callback_query_id=cq["id"])

    if data.startswith("rps:"):
        user_choice = data.split(":", 1)[1]
        bot_choice = random.choice(list(RPS_CHOICES.keys()))
        if user_choice == bot_choice:
            result = "🤝 Ничья!"
        elif RPS_BEATS[user_choice] == bot_choice:
            result = f"🎉 {username} побеждает!"
        else:
            result = "🤖 Бот побеждает!"
        text = (f"{username}: {RPS_CHOICES[user_choice]} {RPS_NAMES[user_choice]}\n"
                f"Бот: {RPS_CHOICES[bot_choice]} {RPS_NAMES[bot_choice]}\n\n{result}\n\n"
                f"Сыграть ещё — /rps")
        api_call("editMessageText", chat_id=chat_id, message_id=message_id, text=text)
        return


def process_update(u):
    try:
        if "message" in u:
            handle_message(u["message"])
        elif "callback_query" in u:
            handle_callback(u["callback_query"])
    except Exception as e:
        logger.error(f"Ошибка обработки апдейта: {e}")
        try:
            chat_id = u.get("message", {}).get("chat", {}).get("id")
            if not chat_id and "callback_query" in u:
                chat_id = u["callback_query"]["message"]["chat"]["id"]
            if chat_id:
                send(chat_id, "⚠️ Произошла ошибка. Попробуйте ещё раз или /reset.")
        except Exception:
            pass


def setup_bot_commands():
    commands = [
        {"command": "start", "description": "Начать общение"},
        {"command": "help", "description": "Справка"},
        {"command": "games", "description": "Мини-игры и доп. команды"},
        {"command": "guess", "description": "Угадай число от 1 до 100"},
        {"command": "rps", "description": "Камень-ножницы-бумага"},
        {"command": "dice", "description": "Бросить кубик"},
        {"command": "dart", "description": "Бросить дротик"},
        {"command": "slot", "description": "Слот-машина"},
        {"command": "coin", "description": "Подбросить монетку"},
        {"command": "joke", "description": "Случайная шутка"},
        {"command": "fact", "description": "Случайный факт"},
        {"command": "reset", "description": "Очистить историю диалога"},
    ]
    api_call("setMyCommands", commands=json.dumps(commands))


PERIODIC_INTERVAL_SECONDS = 4 * 60 * 60  # раз в ~4 часа


def periodic_greetings_loop():
    """Фоновый поток: время от времени бот сам пишет что-нибудь в группы,
    где он состоит — чтобы не было ощущения 'мёртвого' бота."""
    while True:
        time.sleep(PERIODIC_INTERVAL_SECONDS)
        try:
            groups = get_all_groups()
            for chat_id in groups:
                try:
                    send(chat_id, random.choice(CASUAL_GROUP_MESSAGES))
                except Exception as e:
                    logger.warning(f"Не удалось отправить приветствие в {chat_id}: {e}")
        except Exception as e:
            logger.error(f"Ошибка в periodic_greetings_loop: {e}")


def start_background_greetings():
    t = threading.Thread(target=periodic_greetings_loop, daemon=True)
    t.start()
    logger.info(f"Фоновые приветствия в группах запущены (раз в {PERIODIC_INTERVAL_SECONDS//3600} ч).")


def main():
    init_db()
    setup_bot_commands()
    start_background_greetings()
    logger.info("ИИ-бот запущен (long polling). Ctrl+C для остановки.")
    offset = None
    executor = ThreadPoolExecutor(max_workers=8)
    while True:
        try:
            params = {"timeout": 30}
            if offset:
                params["offset"] = offset
            r = requests.get(API_URL + "getUpdates", params=params, timeout=40)
            data = r.json()
            if not data.get("ok"):
                logger.warning(f"Ошибка Telegram API: {data}")
                time.sleep(3)
                continue
            for u in data.get("result", []):
                offset = u["update_id"] + 1
                executor.submit(process_update, u)
        except requests.exceptions.RequestException as e:
            logger.warning(f"Сетевая ошибка: {e}")
            time.sleep(5)
        except KeyboardInterrupt:
            logger.info("Остановлено.")
            break


# ============================================================
# WEBHOOK-РЕЖИМ (для Render)
# ============================================================

try:
    from flask import Flask, request as flask_request, jsonify
    _FLASK_AVAILABLE = True
except ImportError:
    _FLASK_AVAILABLE = False

if _FLASK_AVAILABLE:
    app = Flask(__name__)

    @app.route("/", methods=["GET"])
    def health_check():
        return "OK, Claude bot is alive", 200

    @app.route(f"/webhook/<token>", methods=["POST"])
    def telegram_webhook(token):
        if token != BOT_TOKEN:
            return "forbidden", 403
        update = flask_request.get_json(force=True, silent=True) or {}
        threading.Thread(target=process_update, args=(update,), daemon=True).start()
        return jsonify(ok=True)


def set_webhook():
    external_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not external_url:
        external_url = os.environ.get("WEBHOOK_URL", "").rstrip("/")
    if not external_url:
        logger.error("Не найден RENDER_EXTERNAL_URL/WEBHOOK_URL — webhook не установлен.")
        return
    webhook_url = f"{external_url}/webhook/{BOT_TOKEN}"
    resp = api_call("setWebhook", url=webhook_url)
    if resp.get("ok"):
        logger.info(f"Webhook установлен: {webhook_url}")
    else:
        logger.error(f"Не удалось установить webhook: {resp}")


def run_webhook_mode():
    if not _FLASK_AVAILABLE:
        logger.error("Flask не установлен! pip install flask")
        return
    init_db()
    setup_bot_commands()
    set_webhook()
    start_background_greetings()
    logger.info("ИИ-бот запущен в РЕЖИМЕ WEBHOOK (Render).")
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    use_webhook = bool(os.environ.get("RENDER")) or os.environ.get("USE_WEBHOOK") == "1"
    if use_webhook:
        run_webhook_mode()
    else:
        main()
