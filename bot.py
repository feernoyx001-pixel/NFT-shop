# -*- coding: utf-8 -*-
"""
Бот-маркетплейс NFT/подарков Telegram.
ВАЖНО: бот НЕ держит чужие NFT/деньги — он только доска объявлений,
которая сводит продавца и покупателя. Сама сделка (передача NFT,
оплата) проходит между людьми напрямую — как на Авито.
Это сознательное архитектурное решение: бот не берёт на себя
custodial-хранение чужих активов (см. объяснение рисков в переписке).

Нужна только одна библиотека: requests (+ flask для webhook-режима).
Работает в двух режимах — long polling (Termux) и webhook (Render).
"""
import logging
import os
import re
import sqlite3
import threading
import time
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from logging.handlers import RotatingFileHandler

import requests

BOT_TOKEN = os.getenv("BOT_TOKEN", "8658476526:AAFRybtDXFcL2wdWcVvKlxqEd1WHNq1DGzk")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/"
DB_PATH = "nft_bot.db"
STATE_LOCK = threading.Lock()
DB_LOCK = threading.Lock()

# ============================================================
# ЛОГИРОВАНИЕ
# ============================================================

logger = logging.getLogger("nft_market_bot")
logger.setLevel(logging.INFO)
_fh = RotatingFileHandler("bot.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
_ch = logging.StreamHandler()
_ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_fh)
logger.addHandler(_ch)

# ============================================================
# КОНТЕНТ
# ============================================================

WELCOME_TEXT = (
    "👋 Добро пожаловать на маркетплейс NFT-подарков Telegram!\n\n"
    "Здесь можно выставить свои NFT/подарки на продажу и посмотреть, "
    "что продают другие — по TON или Stars.\n\n"
    "⚠️ ВАЖНО, прочитайте перед сделками:\n"
    "Этот бот — ДОСКА ОБЪЯВЛЕНИЙ, а не эскроу-сервис. Бот не держит "
    "ваши NFT и не переводит деньги за вас — вы находите контрагента "
    "здесь, а саму сделку (обмен NFT ↔ оплата) проводите напрямую. "
    "Всегда проверяйте продавца/покупателя перед сделкой — см. раздел "
    "'🛡 Как не быть кинутым'."
)

SAFETY_GUIDE = (
    "🛡 КАК НЕ БЫТЬ КИНУТЫМ ПРИ P2P-СДЕЛКЕ С NFT\n\n"
    "1️⃣ Никогда не переводите оплату ПЕРВЫМ без гарантий\n"
    "Классическая схема развода: 'заплати, потом отправлю NFT' — и "
    "человек исчезает. Требуйте либо взаимной отправки одновременно "
    "через проверенный эскроу-бот, либо сделку через посредника с "
    "репутацией.\n\n"
    "2️⃣ Проверяйте, что NFT реально существует и принадлежит продавцу\n"
    "Используйте кнопку '🔎 Проверить NFT' в этом боте — она покажет "
    "текущего владельца адреса в блокчейне TON. Если владелец не "
    "совпадает с тем, кто предлагает продажу — это обман.\n\n"
    "3️⃣ Для крупных сумм используйте известные эскроу-платформы\n"
    "Portals, Tonnel, Fragment и подобные сервисы держат актив на "
    "смарт-контракте до подтверждения обеими сторонами — это безопаснее "
    "прямой сделки в личке.\n\n"
    "4️⃣ Красные флаги продавца\n"
    "• Требует предоплату 100% без каких-либо гарантий\n"
    "• Профиль создан недавно, нет истории сделок/отзывов\n"
    "• Торопит с решением ('только 10 минут, потом отдам другому')\n"
    "• Отказывается подтвердить владение NFT через блокчейн-проверку\n\n"
    "5️⃣ Смотрите отзывы о контрагенте\n"
    "В разделе профиля продавца — история сделок и отзывов от других "
    "пользователей бота (самостоятельно оставленные, не проверены "
    "ботом — используйте как один из сигналов, не как гарантию)."
)

HELP_TEXT = (
    "ℹ️ КОМАНДЫ И ВОЗМОЖНОСТИ\n\n"
    "/start — открыть меню\n"
    "/help — эта справка\n"
    "/cancel — отменить текущий ввод\n"
    "/myid — узнать свой Telegram user_id\n\n"
    "📌 В меню:\n"
    "• ➕ Выставить NFT — разместить объявление о продаже\n"
    "• 🛒 Смотреть объявления — каталог всех активных лотов\n"
    "• 📋 Мои объявления — управление своими лотами\n"
    "• 🔎 Проверить NFT — проверка владельца по адресу в блокчейне TON\n"
    "• 🛡 Как не быть кинутым — гид по безопасным сделкам\n"
    "• ⭐ Отзывы — оставить/посмотреть отзыв о контрагенте"
)

CURRENCIES = ["TON", "Stars", "TON или Stars"]

# ============================================================
# БАЗА ДАННЫХ
# ============================================================


def init_db():
    with closing(sqlite3.connect(DB_PATH)) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS users (
                        user_id INTEGER PRIMARY KEY,
                        username TEXT DEFAULT '',
                        ts TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS listings (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER, username TEXT DEFAULT '',
                        title TEXT, collection TEXT, price TEXT, currency TEXT,
                        description TEXT, contact TEXT,
                        status TEXT DEFAULT 'active',
                        ts TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS reviews (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        about_user_id INTEGER, from_user_id INTEGER,
                        from_username TEXT DEFAULT '',
                        rating INTEGER, comment TEXT,
                        ts TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.commit()


def touch_user(user_id, username):
    with DB_LOCK, closing(sqlite3.connect(DB_PATH)) as c:
        c.execute("INSERT INTO users (user_id, username) VALUES (?, ?) "
                   "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
                   (user_id, username or ""))
        c.commit()


def add_listing(user_id, username, title, collection, price, currency, description, contact):
    with DB_LOCK, closing(sqlite3.connect(DB_PATH)) as c:
        c.execute("""INSERT INTO listings
                     (user_id, username, title, collection, price, currency, description, contact)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                  (user_id, username or "", title, collection, price, currency, description, contact))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]


def get_active_listings(limit=10, offset=0, collection_filter=None):
    with closing(sqlite3.connect(DB_PATH)) as c:
        if collection_filter:
            return c.execute(
                "SELECT id, username, title, collection, price, currency, description, contact, ts "
                "FROM listings WHERE status='active' AND collection LIKE ? "
                "ORDER BY id DESC LIMIT ? OFFSET ?",
                (f"%{collection_filter}%", limit, offset)).fetchall()
        return c.execute(
            "SELECT id, username, title, collection, price, currency, description, contact, ts "
            "FROM listings WHERE status='active' ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset)).fetchall()


def get_user_listings(user_id):
    with closing(sqlite3.connect(DB_PATH)) as c:
        return c.execute(
            "SELECT id, title, price, currency, status, ts FROM listings "
            "WHERE user_id=? ORDER BY id DESC", (user_id,)).fetchall()


def delete_listing(listing_id, user_id):
    with DB_LOCK, closing(sqlite3.connect(DB_PATH)) as c:
        c.execute("DELETE FROM listings WHERE id=? AND user_id=?", (listing_id, user_id))
        c.commit()


def get_listing(listing_id):
    with closing(sqlite3.connect(DB_PATH)) as c:
        return c.execute(
            "SELECT id, user_id, username, title, collection, price, currency, description, contact "
            "FROM listings WHERE id=?", (listing_id,)).fetchone()


def add_review(about_user_id, from_user_id, from_username, rating, comment):
    with DB_LOCK, closing(sqlite3.connect(DB_PATH)) as c:
        c.execute("""INSERT INTO reviews (about_user_id, from_user_id, from_username, rating, comment)
                     VALUES (?, ?, ?, ?, ?)""",
                  (about_user_id, from_user_id, from_username or "", rating, comment))
        c.commit()


def get_reviews(about_user_id, limit=10):
    with closing(sqlite3.connect(DB_PATH)) as c:
        return c.execute(
            "SELECT from_username, rating, comment, ts FROM reviews "
            "WHERE about_user_id=? ORDER BY id DESC LIMIT ?", (about_user_id, limit)).fetchall()


def get_admin_stats():
    with closing(sqlite3.connect(DB_PATH)) as c:
        users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        active = c.execute("SELECT COUNT(*) FROM listings WHERE status='active'").fetchone()[0]
        total = c.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        reviews = c.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
        return users, active, total, reviews

# ============================================================
# ПРОВЕРКА NFT В БЛОКЧЕЙНЕ (защита от обмана, публичный API TON)
# ============================================================


def check_nft_owner(address):
    """Проверка владельца NFT по адресу через публичный tonapi.io.
    Не требует ключа для единичных запросов (может быть с ограничением
    по частоте запросов)."""
    address = address.strip()
    try:
        r = requests.get(f"https://tonapi.io/v2/nfts/{address}", timeout=10)
        if r.status_code == 404:
            return "❌ NFT с таким адресом не найден. Проверьте, что адрес указан верно."
        if r.status_code != 200:
            return f"⚠️ Не удалось проверить (код {r.status_code}). Попробуйте позже — возможно, ограничение частоты запросов у публичного API."
        data = r.json()
        owner = data.get("owner", {})
        owner_addr = owner.get("address", "неизвестно")
        collection = data.get("collection", {}).get("name", "—")
        name = data.get("metadata", {}).get("name", "—")
        verified = data.get("approved_by", [])
        lines = [
            f"🔎 Проверка NFT: {address}",
            f"Название: {name}",
            f"Коллекция: {collection}",
            f"Текущий владелец (адрес кошелька): {owner_addr}",
        ]
        if verified:
            lines.append(f"✅ Верифицировано площадками: {', '.join(verified)}")
        lines.append("\n💡 Сверьте 'текущий владелец' с тем, кто предлагает вам продажу — "
                      "если адреса не совпадают, продавец не владеет этим NFT.")
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ Ошибка проверки: {e}\n\nПопробуйте проверить вручную на tonviewer.com/{address}"

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


def send(chat_id, text, inline_kb=None):
    params = {"chat_id": chat_id, "text": text}
    if inline_kb:
        params["reply_markup"] = json.dumps({"inline_keyboard": inline_kb})
    return api_call("sendMessage", **params)


def edit(chat_id, message_id, text, inline_kb=None):
    params = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if inline_kb:
        params["reply_markup"] = json.dumps({"inline_keyboard": inline_kb})
    return api_call("editMessageText", **params)


def answer_callback(callback_id, text=None):
    params = {"callback_query_id": callback_id}
    if text:
        params["text"] = text
    api_call("answerCallbackQuery", **params)


def btn(text, data):
    return {"text": text, "callback_data": data}


def kb_main():
    return [
        [btn("➕ Выставить NFT", "new_listing")],
        [btn("🛒 Смотреть объявления", "browse:0")],
        [btn("📋 Мои объявления", "my_listings")],
        [btn("🔎 Проверить NFT", "check_nft")],
        [btn("⭐ Отзывы", "reviews_menu")],
        [btn("🛡 Как не быть кинутым", "safety")],
    ]


def kb_back(target="main"):
    return [[btn("⬅️ Назад", target)]]


# состояние диалога: user_id -> {"action": ..., "step": ..., "data": {...}}
STATE = {}


def open_main_menu(chat_id):
    send(chat_id, WELCOME_TEXT, kb_main())


def handle_message(msg):
    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    username = msg["from"].get("username") or msg["from"].get("first_name", "")
    text = msg.get("text", "").strip()

    if text == "/start":
        with STATE_LOCK:
            STATE.pop(user_id, None)
        touch_user(user_id, username)
        open_main_menu(chat_id)
        return

    if text == "/help":
        send(chat_id, HELP_TEXT)
        return

    if text == "/myid":
        send(chat_id, f"Ваш Telegram user_id: {user_id}")
        return

    if text == "/cancel":
        with STATE_LOCK:
            had = STATE.pop(user_id, None) is not None
        send(chat_id, "Отменено." if had else "Нечего отменять.", kb_back("main"))
        return

    if text == "/admin":
        if ADMIN_ID == 0 or user_id != ADMIN_ID:
            send(chat_id, "⛔ Только для администратора.")
            return
        users, active, total, reviews = get_admin_stats()
        send(chat_id, f"👑 АДМИН-ПАНЕЛЬ\n\nПользователей: {users}\n"
                       f"Активных объявлений: {active}\nВсего объявлений: {total}\n"
                       f"Отзывов оставлено: {reviews}")
        return

    # многошаговый ввод (создание объявления / проверка NFT / отзыв)
    with STATE_LOCK:
        pending = STATE.get(user_id)

    if pending is None:
        send(chat_id, "Нажмите /start, чтобы открыть меню.")
        return

    action = pending["action"]

    if action == "check_nft":
        with STATE_LOCK:
            STATE.pop(user_id, None)
        send(chat_id, "⏳ Проверяю в блокчейне...")
        result = check_nft_owner(text)
        send(chat_id, result, kb_back("main"))
        return

    if action == "new_listing":
        step = pending["step"]
        data = pending["data"]
        steps = ["title", "collection", "price", "description", "contact"]
        data[steps[step]] = text
        next_step = step + 1
        prompts = {
            "title": "Название NFT/подарка:",
            "collection": "Название коллекции:",
            "price": "Цена (число) и валюта, например: 50 TON или 20000 Stars:",
            "description": "Краткое описание (редкость, особенности):",
            "contact": "Контакт для связи (ваш @username или ссылка):",
        }
        if next_step < len(steps):
            with STATE_LOCK:
                STATE[user_id]["step"] = next_step
            send(chat_id, prompts[steps[next_step]])
            return
        else:
            with STATE_LOCK:
                STATE.pop(user_id, None)
            price_text = data.get("price", "")
            listing_id = add_listing(
                user_id, username, data.get("title", ""), data.get("collection", ""),
                price_text, "", data.get("description", ""), data.get("contact", "")
            )
            send(chat_id, f"✅ Объявление №{listing_id} размещено!\n\n"
                          f"{data.get('title')} ({data.get('collection')}) — {price_text}\n\n"
                          f"Посмотреть в каталоге можно через '🛒 Смотреть объявления'.",
                 kb_back("main"))
            return

    if action == "leave_review":
        step = pending["step"]
        data = pending["data"]
        if step == 0:
            if text not in ("1", "2", "3", "4", "5"):
                send(chat_id, "Введите оценку числом от 1 до 5:")
                return
            data["rating"] = int(text)
            with STATE_LOCK:
                STATE[user_id]["step"] = 1
            send(chat_id, "Комментарий к отзыву (коротко):")
            return
        else:
            with STATE_LOCK:
                STATE.pop(user_id, None)
            add_review(data["about_user_id"], user_id, username, data["rating"], text)
            send(chat_id, "✅ Отзыв сохранён. Спасибо!", kb_back("main"))
            return


def show_browse(chat_id, message_id, offset):
    rows = get_active_listings(limit=5, offset=offset)
    if not rows:
        text = "Объявлений больше нет." if offset > 0 else "Пока нет активных объявлений — станьте первым!"
        edit(chat_id, message_id, text, kb_back("main"))
        return
    lines = ["🛒 АКТИВНЫЕ ОБЪЯВЛЕНИЯ:\n"]
    kb = []
    for (lid, uname, title, collection, price, currency, desc, contact, ts) in rows:
        lines.append(f"#{lid} — {title} ({collection})\nЦена: {price}\nОписание: {desc}\n"
                      f"Продавец: @{uname or 'аноним'}\nКонтакт: {contact}\n")
        kb.append([btn(f"⭐ Отзыв о продавце #{lid}", f"review_seller:{lid}")])
    nav = []
    if offset > 0:
        nav.append(btn("⬅️ Назад", f"browse:{max(0, offset-5)}"))
    nav.append(btn("➡️ Ещё", f"browse:{offset+5}"))
    kb.append(nav)
    kb.append([btn("⬅️ В меню", "main")])
    edit(chat_id, message_id, "\n".join(lines), kb)


def show_my_listings(chat_id, message_id, user_id):
    rows = get_user_listings(user_id)
    if not rows:
        edit(chat_id, message_id, "У вас пока нет объявлений.", kb_back("main"))
        return
    lines = ["📋 ВАШИ ОБЪЯВЛЕНИЯ:\n"]
    kb = []
    for (lid, title, price, currency, status, ts) in rows:
        lines.append(f"#{lid} — {title} — {price} [{status}]")
        kb.append([btn(f"🗑 Удалить #{lid}", f"del_listing:{lid}")])
    kb.append([btn("⬅️ В меню", "main")])
    edit(chat_id, message_id, "\n".join(lines), kb)


def handle_callback(cq):
    chat_id = cq["message"]["chat"]["id"]
    message_id = cq["message"]["message_id"]
    user_id = cq["from"]["id"]
    username = cq["from"].get("username") or cq["from"].get("first_name", "")
    data = cq["data"]
    answer_callback(cq["id"])
    touch_user(user_id, username)

    if data == "main":
        edit(chat_id, message_id, "Выберите раздел:", kb_main())
        return

    if data == "safety":
        edit(chat_id, message_id, SAFETY_GUIDE, kb_back("main"))
        return

    if data == "new_listing":
        with STATE_LOCK:
            STATE[user_id] = {"action": "new_listing", "step": 0, "data": {}}
        edit(chat_id, message_id, "Начинаем оформление объявления.\n\nНазвание NFT/подарка:")
        return

    if data == "check_nft":
        with STATE_LOCK:
            STATE[user_id] = {"action": "check_nft"}
        edit(chat_id, message_id, "Введите адрес NFT в блокчейне TON для проверки владельца:")
        return

    if data.startswith("browse:"):
        offset = int(data.split(":", 1)[1])
        show_browse(chat_id, message_id, offset)
        return

    if data == "my_listings":
        show_my_listings(chat_id, message_id, user_id)
        return

    if data.startswith("del_listing:"):
        lid = int(data.split(":", 1)[1])
        delete_listing(lid, user_id)
        show_my_listings(chat_id, message_id, user_id)
        return

    if data == "reviews_menu":
        edit(chat_id, message_id,
             "⭐ Чтобы посмотреть отзывы о продавце — откройте объявление в каталоге "
             "и нажмите кнопку отзыва под ним. Чтобы оставить отзыв — тоже там же.",
             kb_back("main"))
        return

    if data.startswith("review_seller:"):
        lid = int(data.split(":", 1)[1])
        listing = get_listing(lid)
        if not listing:
            edit(chat_id, message_id, "Объявление не найдено.", kb_back("main"))
            return
        about_user_id = listing[1]
        with STATE_LOCK:
            STATE[user_id] = {"action": "leave_review", "step": 0, "data": {"about_user_id": about_user_id}}
        edit(chat_id, message_id, "Оцените продавца от 1 до 5:")
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
            chat_id = None
            if "message" in u:
                chat_id = u["message"]["chat"]["id"]
            elif "callback_query" in u:
                chat_id = u["callback_query"]["message"]["chat"]["id"]
            if chat_id:
                send(chat_id, "⚠️ Произошла ошибка. Попробуйте /start.")
        except Exception:
            pass


def setup_bot_commands():
    commands = [
        {"command": "start", "description": "Открыть меню"},
        {"command": "help", "description": "Справка"},
        {"command": "cancel", "description": "Отменить ввод"},
        {"command": "myid", "description": "Узнать свой user_id"},
    ]
    api_call("setMyCommands", commands=json.dumps(commands))


def main():
    init_db()
    setup_bot_commands()
    logger.info("NFT-бот запущен (long polling). Ctrl+C для остановки.")
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
        return "OK, NFT bot is alive", 200

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
    logger.info("NFT-бот запущен в РЕЖИМЕ WEBHOOK (Render).")
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    use_webhook = bool(os.environ.get("RENDER")) or os.environ.get("USE_WEBHOOK") == "1"
    if use_webhook:
        run_webhook_mode()
    else:
        main()
