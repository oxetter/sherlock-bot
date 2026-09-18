#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import io
import re
import socket
import time
import telebot
from telebot import types
from flask import Flask, request
from threading import Thread
from supabase import create_client
from datetime import datetime, timezone, timedelta

# ============ НАСТРОЙКИ ============
BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

CHANNEL_USERNAME = "worksoxetter"
CHANNEL_LINK = "https://t.me/worksoxetter"
TRIAL_LIMIT = 5

print("[DEBUG] Токен: " + str(len(BOT_TOKEN)))
print("[DEBUG] Supabase: " + str(len(SUPABASE_URL)))
print("[DEBUG] Admin: " + str(ADMIN_ID))


# ============ FLASK ============
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running"

@app.route("/health")
def health():
    return "OK"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)


# ============ SUPABASE ============
sb = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        sb = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("[+] Supabase подключён")
    except Exception as e:
        print("[ERROR] Supabase: " + str(e))


# ============ БОТ ============
bot = telebot.TeleBot(BOT_TOKEN) if BOT_TOKEN else None

TARIFFS = {
    "1day":    {"days": 1,    "stars": 15,  "label": "1 день"},
    "week":    {"days": 7,    "stars": 50,  "label": "Неделя"},
    "month":   {"days": 30,   "stars": 100, "label": "Месяц"},
    "forever": {"days": 36500,"stars": 200, "label": "Навсегда"},
}


# ============ БАЗА ============
def get_user(user_id):
    try:
        res = sb.table("users").select("*").eq("user_id", user_id).execute()
        if res.data:
            return res.data[0]
    except Exception as e:
        print("[ERROR] get_user: " + str(e))
    return None


def create_user(user_id, username, first_name):
    try:
        sb.table("users").insert({
            "user_id": user_id,
            "username": username or "",
            "first_name": first_name or "",
        }).execute()
    except Exception as e:
        print("[ERROR] create_user: " + str(e))


def has_subscription(user_id):
    u = get_user(user_id)
    if not u:
        return False
    until = u.get("subscription_until")
    if not until:
        return False
    try:
        dt = datetime.fromisoformat(until.replace("Z", "+00:00"))
        return dt > datetime.now(timezone.utc)
    except Exception:
        return False


def give_subscription(user_id, days):
    u = get_user(user_id)
    if not u:
        create_user(user_id, "", "")
        u = get_user(user_id)
    now = datetime.now(timezone.utc)
    until = u.get("subscription_until") if u else None
    base = now
    if until:
        try:
            dt = datetime.fromisoformat(until.replace("Z", "+00:00"))
            if dt > now:
                base = dt
        except Exception:
            pass
    new_until = base + timedelta(days=days)
    try:
        sb.table("users").update({
            "subscription_until": new_until.isoformat()
        }).eq("user_id", user_id).execute()
    except Exception as e:
        print("[ERROR] give_subscription: " + str(e))


def check_key(key):
    try:
        res = sb.table("keys").select("*").eq("key", key).execute()
        if res.data and not res.data[0].get("used_by"):
            return True
    except Exception:
        pass
    return False


def use_key(key, user_id):
    try:
        sb.table("keys").update({
            "used_by": user_id,
            "used_at": datetime.now(timezone.utc).isoformat()
        }).eq("key", key).execute()
        return True
    except Exception:
        return False


def log_payment(user_id, stars, days):
    try:
        sb.table("payments").insert({
            "user_id": user_id,
            "amount": stars,
            "days": days,
        }).execute()
    except Exception as e:
        print("[ERROR] log_payment: " + str(e))


def create_mirror_request(user_id, bot_token):
    try:
        sb.table("mirrors").insert({
            "user_id": user_id,
            "bot_token": bot_token,
            "status": "pending",
        }).execute()
        return True
    except Exception:
        return False


def get_trial_count(user_id):
    u = get_user(user_id)
    if not u:
        return 0
    return u.get("trial_used", 0) or 0


def use_trial(user_id):
    count = get_trial_count(user_id)
    if count >= TRIAL_LIMIT:
        return False
    try:
        sb.table("users").update({
            "trial_used": count + 1
        }).eq("user_id", user_id).execute()
        return True
    except Exception:
        return False


def has_trial_left(user_id):
    return get_trial_count(user_id) < TRIAL_LIMIT


# ============ ПОДПИСКА НА КАНАЛ ============
def is_subscribed(user_id):
    try:
        res = bot.get_chat_member(chat_id="@" + CHANNEL_USERNAME, user_id=user_id)
        return res.status in ["member", "administrator", "creator"]
    except Exception as e:
        print("[ERROR] is_subscribed: " + str(e))
        return False


def send_subscribe_message(chat_id):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📢 Подписаться", url=CHANNEL_LINK))
    markup.add(types.InlineKeyboardButton("✅ Проверить подписку", callback_data="check_sub"))
    bot.send_message(
        chat_id,
        "⚠️ Для использования бота подпишись на канал:\n" + CHANNEL_LINK,
        reply_markup=markup
    )
  

# ============ АНАЛИЗ НОМЕРА ============
def analyze_phone(phone):
    try:
        import phonenumbers
        from phonenumbers import carrier, geocoder, timezone
    except ImportError:
        os.system(f"{os.sys.executable} -m pip install phonenumbers")
        import phonenumbers
        from phonenumbers import carrier, geocoder, timezone

    phone_clean = re.sub(r"[^\d+]", "", phone)
    if phone_clean.startswith("8"):
        phone_clean = "+7" + phone_clean[1:]
    if not phone_clean.startswith("+"):
        phone_clean = "+" + phone_clean

    r = {"input": phone_clean, "valid": False}

    try:
        parsed = phonenumbers.parse(phone_clean, None)
    except Exception:
        return r

    r["valid"] = phonenumbers.is_valid_number(parsed)
    if not r["valid"]:
        return r

    r["country"] = phonenumbers.region_code_for_number(parsed)
    r["operator"] = carrier.name_for_number(parsed, "ru") or "Неизвестно"
    r["region"] = geocoder.description_for_number(parsed, "ru") or "Неизвестно"
    tz = timezone.time_zones_for_number(parsed)
    r["timezone"] = list(tz)[0] if tz else "Неизвестно"

    # VK
    try:
        num = re.sub(r"[^\d]", "", phone_clean)
        rq = requests.get("https://api.vk.com/method/auth.restore",
                          params={"phone": num, "v": "5.131"}, timeout=10)
        data = rq.json()
        if "response" in data:
            r["vk"] = "✅ Есть аккаунт"
        elif "error" in data and data["error"].get("error_code") == 5:
            r["vk"] = "❌ Нет"
        else:
            r["vk"] = "⚠️ Неизвестно"
    except Exception:
        r["vk"] = "⚠️ Ошибка"

    # Telegram
    try:
        num = re.sub(r"[^\d]", "", phone_clean)
        if num.startswith("8"):
            num = "7" + num[1:]
        link = "https://t.me/+" + num
        r["tg_link"] = link
        rq = requests.get(link, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        if "tgme_page_title" in rq.text:
            m = re.search(r'<div class="tgme_page_title"[^>]*>([^<]+)</div>', rq.text)
            r["telegram"] = "✅ " + (m.group(1).strip() if m else "есть")
        else:
            r["telegram"] = "❌ Не найден"
    except Exception:
        r["telegram"] = "⚠️ Ошибка"

    # WhatsApp
    try:
        num = re.sub(r"[^\d]", "", phone_clean)
        r["whatsapp"] = "https://wa.me/" + num
    except Exception:
        pass

    return r


# ============ EMAIL ============
def analyze_email(email):
    r = {"input": email, "valid": False}
    if not re.match(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$", email):
        return r
    r["valid"] = True
    r["domain"] = email.split("@")[1].lower()
    try:
        socket.gethostbyname(r["domain"])
        r["alive"] = True
    except Exception:
        r["alive"] = False

    if "mail.ru" in r["domain"] or "bk.ru" in r["domain"] or "inbox.ru" in r["domain"]:
        r["service"] = "Mail.ru"
    elif "gmail" in r["domain"]:
        r["service"] = "Gmail"
    elif "yandex" in r["domain"] or "ya.ru" in r["domain"]:
        r["service"] = "Yandex"
    elif "rambler" in r["domain"]:
        r["service"] = "Rambler"
    else:
        r["service"] = "Неизвестно"

    r["hibp"] = "https://haveibeenpwned.com/account/" + email
    return r


# ============ НИК ============
def check_username(username):
    username = username.strip().lstrip("@")
    r = {"input": username, "platforms": {}}
    platforms = {
        "VK": f"https://vk.com/{username}",
        "Telegram": f"https://t.me/{username}",
        "TikTok": f"https://www.tiktok.com/@{username}",
        "Instagram": f"https://www.instagram.com/{username}",
        "GitHub": f"https://github.com/{username}",
        "Twitter": f"https://twitter.com/{username}",
        "YouTube": f"https://www.youtube.com/@{username}",
    }
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    for name, url in platforms.items():
        try:
            rq = requests.head(url, headers=headers, timeout=8, allow_redirects=True)
            if rq.status_code == 200:
                r["platforms"][name] = url
        except Exception:
            pass
    return r


# ============ VK ID ============
def check_vk_id(vk_id):
    r = {"input": vk_id, "found": False}
    try:
        rq = requests.get(
            f"https://api.vk.com/method/users.get",
            params={"user_ids": vk_id, "fields": "city,bdate,country", "v": "5.131"},
            timeout=10
        )
        data = rq.json()
        if "response" in data and data["response"]:
            u = data["response"][0]
            r["found"] = True
            r["name"] = (u.get("first_name", "") + " " + u.get("last_name", "")).strip()
            if u.get("city"): r["city"] = u["city"].get("title")
            if u.get("bdate"):
                r["bdate"] = u["bdate"]
                parts = u["bdate"].split(".")
                if len(parts) == 3:
                    try:
                        year = int(parts[2])
                        r["age"] = str(datetime.now().year - year)
                    except Exception:
                        pass
    except Exception as e:
        print("[ERROR] check_vk_id: " + str(e))
    return r


# ============ ФИО ============
def search_fio(fio):
    """Ищет ФИО в открытых источниках — через VK поиск."""
    r = {"input": fio, "results": []}
    try:
        rq = requests.get(
            "https://api.vk.com/method/users.search",
            params={"q": fio, "count": 10, "v": "5.131"},
            timeout=10
        )
        data = rq.json()
        if "response" in data and "items" in data["response"]:
            for u in data["response"]["items"][:10]:
                r["results"].append({
                    "id": u.get("id"),
                    "name": (u.get("first_name", "") + " " + u.get("last_name", "")).strip(),
                    "link": "https://vk.com/id" + str(u.get("id")),
                })
    except Exception as e:
        print("[ERROR] search_fio: " + str(e))
    return r
  

# ============ КЛАВИАТУРЫ ============
def main_menu_inline():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("📱 Номер", callback_data="os_phone"),
        types.InlineKeyboardButton("📧 Email", callback_data="os_email"),
        types.InlineKeyboardButton("👤 Никнейм", callback_data="os_username"),
        types.InlineKeyboardButton("🆔 VK ID", callback_data="os_vkid"),
        types.InlineKeyboardButton("📛 ФИО", callback_data="os_fio"),
        types.InlineKeyboardButton("⭐ Подписка", callback_data="menu_subscribe"),
    )
    return markup


def subscribe_menu():
    markup = types.InlineKeyboardMarkup(row_width=1)
    for key, t in TARIFFS.items():
        label = t["label"] + " — " + str(t["stars"]) + " ⭐"
        markup.add(types.InlineKeyboardButton(label, callback_data="buy_" + key))
    return markup


# ============ ОБРАБОТЧИКИ ============
if bot:
    @bot.message_handler(commands=["start"])
    def cmd_start(message):
        uid = message.from_user.id
        if not get_user(uid):
            create_user(uid, message.from_user.username, message.from_user.first_name)

        if not is_subscribed(uid):
            send_subscribe_message(message.chat.id)
            return

        text = (
            "🔍 OSINT SHERLOCK BOT\n\n"
            "Ищу информацию в открытых источниках:\n"
            "📱 Номер — страна, оператор, соцсети\n"
            "📧 Email — сервис, утечки\n"
            "👤 Ник — где занят\n"
            "🆔 VK ID — имя, город, возраст\n"
            "📛 ФИО — упоминания в VK\n\n"
            "Подписка обязательна."
        )
        bot.send_message(message.chat.id, text, reply_markup=main_menu_inline())

    @bot.message_handler(commands=["menu"])
    def cmd_menu(message):
        bot.send_message(message.chat.id, "Выбери действие:", reply_markup=main_menu_inline())

    @bot.message_handler(commands=["help"])
    def cmd_help(message):
        cmd_start(message)

    @bot.message_handler(commands=["mykeys"])
    def cmd_mykeys(message):
        if message.from_user.id != ADMIN_ID:
            bot.send_message(message.chat.id, "❌ Нет доступа")
            return
        try:
            res = sb.table("keys").select("key").is_("used_by", "null").execute()
            keys = [r["key"] for r in res.data] if res.data else []
            if not keys:
                bot.send_message(message.chat.id, "Свободных ключей нет")
                return
            text = "\n".join(keys)
            buf = io.BytesIO(text.encode("utf-8"))
            buf.name = "keys.txt"
            bot.send_document(message.chat.id, buf, caption="Ключей: " + str(len(keys)))
        except Exception as e:
            bot.send_message(message.chat.id, "Ошибка: " + str(e)[:150])

    @bot.callback_query_handler(func=lambda call: call.data == "check_sub")
    def process_check_sub(call):
        uid = call.from_user.id
        if is_subscribed(uid):
            bot.answer_callback_query(call.id, "✅ Подписка подтверждена!")
            bot.send_message(
                call.message.chat.id,
                "✅ Добро пожаловать!",
                reply_markup=main_menu_inline()
            )
        else:
            bot.answer_callback_query(call.id, "❌ Ты не подписан!", show_alert=True)

    @bot.callback_query_handler(func=lambda call: call.data == "menu_subscribe")
    def cb_subscribe(call):
        bot.answer_callback_query(call.id)
        text = (
            "⭐ Тарифы:\n\n"
            "1 день — 15 звёзд\n"
            "Неделя — 50 звёзд\n"
            "Месяц — 100 звёзд\n"
            "Навсегда — 200 звёзд"
        )
        bot.send_message(call.message.chat.id, text, reply_markup=subscribe_menu())

    @bot.callback_query_handler(func=lambda call: call.data.startswith("buy_"))
    def process_buy(call):
        key = call.data.replace("buy_", "")
        t = TARIFFS.get(key)
        if not t:
            return
        uid = call.from_user.id
        if not get_user(uid):
            create_user(uid, call.from_user.username, call.from_user.first_name)
        prices = [types.LabeledPrice(label=t["label"], amount=t["stars"])]
        try:
            bot.send_invoice(
                chat_id=call.message.chat.id,
                title="Подписка " + t["label"],
                description="OSINT бот на " + t["label"],
                invoice_payload="sub_" + key,
                provider_token="",
                currency="XTR",
                prices=prices,
                start_parameter="sub"
            )
        except Exception as e:
            bot.send_message(call.message.chat.id, "[!] Ошибка: " + str(e)[:150])

    @bot.pre_checkout_query_handler(func=lambda q: True)
    def process_pre_checkout(query):
        bot.answer_pre_checkout_query(query.id, ok=True)

    @bot.message_handler(content_types=["successful_payment"])
    def process_successful_payment(message):
        payload = message.successful_payment.invoice_payload
        key = payload.replace("sub_", "")
        t = TARIFFS.get(key)
        if not t:
            return
        uid = message.from_user.id
        give_subscription(uid, t["days"])
        log_payment(uid, t["stars"], t["days"])
        bot.send_message(message.chat.id, "✅ Подписка активирована!")

    # ============ OSINT CALLBACKS ============
    @bot.callback_query_handler(func=lambda call: call.data == "os_phone")
    def cb_phone(call):
        bot.answer_callback_query(call.id)
        msg = bot.send_message(call.message.chat.id, "Введи номер:")
        bot.register_next_step_handler(msg, process_phone)

    def process_phone(message):
        uid = message.from_user.id
        if not has_subscription(uid) and not has_trial_left(uid):
            bot.send_message(message.chat.id, "❌ Нет подписки")
            return
        if not has_subscription(uid):
            use_trial(uid)

        bot.send_message(message.chat.id, "[*] Ищу...")
        r = analyze_phone(message.text.strip())
        if not r.get("valid"):
            bot.send_message(message.chat.id, "❌ Невалидный номер")
            return
        text = (
            "📱 НОМЕР: " + r["input"] + "\n\n"
            "🌍 Страна: " + str(r.get("country", "?")) + "\n"
            "📡 Оператор: " + str(r.get("operator", "?")) + "\n"
            "🏙 Регион: " + str(r.get("region", "?")) + "\n"
            "🕐 TZ: " + str(r.get("timezone", "?")) + "\n\n"
            "🔵 VK: " + str(r.get("vk", "?")) + "\n"
            "🔷 TG: " + str(r.get("telegram", "?")) + "\n"
            "📞 WA: " + str(r.get("whatsapp", "?"))
        )
        bot.send_message(message.chat.id, text)

    @bot.callback_query_handler(func=lambda call: call.data == "os_email")
    def cb_email(call):
        bot.answer_callback_query(call.id)
        msg = bot.send_message(call.message.chat.id, "Введи email:")
        bot.register_next_step_handler(msg, process_email)

    def process_email(message):
        uid = message.from_user.id
        if not has_subscription(uid) and not has_trial_left(uid):
            bot.send_message(message.chat.id, "❌ Нет подписки")
            return
        if not has_subscription(uid):
            use_trial(uid)

        r = analyze_email(message.text.strip())
        if not r.get("valid"):
            bot.send_message(message.chat.id, "❌ Невалидный email")
            return
        text = (
            "📧 EMAIL: " + r["input"] + "\n\n"
            "🌐 Домен: " + r.get("domain", "?") + "\n"
            "✅ Живой: " + ("Да" if r.get("alive") else "Нет") + "\n"
            "📮 Сервис: " + r.get("service", "?") + "\n\n"
            "🔍 Утечки: " + r.get("hibp", "?")
        )
        bot.send_message(message.chat.id, text)

    @bot.callback_query_handler(func=lambda call: call.data == "os_username")
    def cb_username(call):
        bot.answer_callback_query(call.id)
        msg = bot.send_message(call.message.chat.id, "Введи ник (без @):")
        bot.register_next_step_handler(msg, process_username)

    def process_username(message):
        uid = message.from_user.id
        if not has_subscription(uid) and not has_trial_left(uid):
            bot.send_message(message.chat.id, "❌ Нет подписки")
            return
        if not has_subscription(uid):
            use_trial(uid)

        r = check_username(message.text.strip())
        text = "👤 НИК: " + r["input"] + "\n\n"
        if r["platforms"]:
            for p, url in r["platforms"].items():
                text += "✅ " + p + ": " + url + "\n"
        else:
            text += "❌ Ничего не найдено"
        bot.send_message(message.chat.id, text)

    @bot.callback_query_handler(func=lambda call: call.data == "os_vkid")
    def cb_vkid(call):
        bot.answer_callback_query(call.id)
        msg = bot.send_message(call.message.chat.id, "Введи VK ID (число):")
        bot.register_next_step_handler(msg, process_vkid)

    def process_vkid(message):
        uid = message.from_user.id
        if not has_subscription(uid) and not has_trial_left(uid):
            bot.send_message(message.chat.id, "❌ Нет подписки")
            return
        if not has_subscription(uid):
            use_trial(uid)

        r = check_vk_id(message.text.strip())
        if not r.get("found"):
            bot.send_message(message.chat.id, "❌ Не найден")
            return
        text = (
            "🆔 VK ID: " + r["input"] + "\n\n"
            "👤 Имя: " + r.get("name", "?") + "\n"
            "🏙 Город: " + str(r.get("city", "?")) + "\n"
            "🎂 ДР: " + str(r.get("bdate", "?")) + "\n"
            "📅 Возраст: " + str(r.get("age", "?"))
        )
        bot.send_message(message.chat.id, text)

    @bot.callback_query_handler(func=lambda call: call.data == "os_fio")
    def cb_fio(call):
        bot.answer_callback_query(call.id)
        msg = bot.send_message(call.message.chat.id, "Введи ФИО:")
        bot.register_next_step_handler(msg, process_fio)

    def process_fio(message):
        uid = message.from_user.id
        if not has_subscription(uid) and not has_trial_left(uid):
            bot.send_message(message.chat.id, "❌ Нет подписки")
            return
        if not has_subscription(uid):
            use_trial(uid)

        r = search_fio(message.text.strip())
        text = "📛 ФИО: " + r["input"] + "\n\n"
        if r["results"]:
            for item in r["results"]:
                text += "• " + item["name"] + " — " + item["link"] + "\n"
        else:
            text += "❌ Ничего не найдено"
        bot.send_message(message.chat.id, text)

    # ============ ОБРАБОТКА ТЕКСТА ============
    @bot.message_handler(func=lambda m: m.text and not m.text.startswith("/"))
    def handle_text(message):
        # Просто игнорируем — работаем через кнопки
        pass


# ============ WEBHOOK ============
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        json_str = request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
    except Exception as e:
        print("[ERROR] webhook: " + str(e))
    return "OK", 200


def set_webhook():
    try:
        import requests as rq
        render_url = os.environ.get("RENDER_EXTERNAL_URL", "")
        if not render_url:
            print("[!] RENDER_EXTERNAL_URL не задан")
            return False
        webhook_url = render_url + "/webhook"
        resp = rq.post(
            "https://api.telegram.org/bot" + BOT_TOKEN + "/setWebhook",
            json={
                "url": webhook_url,
                "allowed_updates": ["message", "callback_query", "pre_checkout_query"]
            }
        )
        print("[+] Webhook: " + str(resp.json()))
        return True
    except Exception as e:
        print("[ERROR] set_webhook: " + str(e))
        return False


if __name__ == "__main__":
    print("[+] Запускаю Flask...")
    t = Thread(target=run_web)
    t.daemon = True
    t.start()

    if bot:
        time.sleep(3)
        print("[+] Устанавливаю webhook...")
        set_webhook()
        print("[+] Бот запущен")
        while True:
            time.sleep(60)
    else:
        print("[ERROR] Нет токена")
        while True:
            time.sleep(60)
