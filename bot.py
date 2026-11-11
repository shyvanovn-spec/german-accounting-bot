import os
import io
import json
import sqlite3
import threading
import asyncio
import pandas as pd
import requests
import telebot
from telebot import types
from fastapi import FastAPI, Request, HTTPException, Header
import uvicorn
import google.generativeai as genai

# --- CONFIGURATION & KEYS ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "YOUR_GOOGLE_GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)

TAKENOS_API_KEY = os.getenv("TAKENOS_API_KEY", "YOUR_TAKENOS_API_KEY")
TAKENOS_WEBHOOK_SECRET = os.getenv("TAKENOS_WEBHOOK_SECRET", "YOUR_WEBHOOK_SECRET_TOKEN")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
app = FastAPI()

# --- DATABASE SETUP (SQLite WAL-mode) ---
DB_FILE = "bot_german_accounting.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL;")
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            balance INTEGER DEFAULT 50,
            export_format TEXT DEFAULT 'datev_csv',
            language TEXT DEFAULT 'de',
            consent_accepted INTEGER DEFAULT 0
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            date TEXT,
            merchant TEXT,
            total REAL,
            vat_amount REAL,
            vat_rate REAL,
            skr_account TEXT,
            ust_idnr TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

def get_user_data(user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT balance, export_format, language, consent_accepted FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    
    if not row:
        cursor.execute("INSERT INTO users (user_id, balance, export_format, language, consent_accepted) VALUES (?, 50, 'datev_csv', 'de', 0)", (user_id,))
        conn.commit()
        data = {"balance": 50, "export_format": "datev_csv", "language": "de", "consent_accepted": 0}
    else:
        data = {"balance": row[0], "export_format": row[1], "language": row[2], "consent_accepted": row[3]}
    conn.close()
    return data

def update_user_field(user_id, field, value):
    get_user_data(user_id)
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(f"UPDATE users SET {field} = ? WHERE user_id = ?", (value, user_id))
    conn.commit()
    conn.close()

def update_balance(user_id, amount_to_add):
    data = get_user_data(user_id)
    new_bal = data["balance"] + amount_to_add
    update_user_field(user_id, "balance", new_bal)

def deduct_balance(user_id):
    data = get_user_data(user_id)
    new_bal = max(0, data["balance"] - 1)
    update_user_field(user_id, "balance", new_bal)

def save_receipt_db(user_id, date, merchant, total, vat_amount, vat_rate, skr_account, ust_idnr):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO receipts (user_id, date, merchant, total, vat_amount, vat_rate, skr_account, ust_idnr)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (user_id, date, merchant, total, vat_amount, vat_rate, skr_account, ust_idnr))
    conn.commit()
    conn.close()

def get_user_receipts(user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT date, merchant, total, vat_amount, vat_rate, skr_account, ust_idnr FROM receipts WHERE user_id = ?", (user_id,))
    rows = cursor.fetchall()
    conn.close()
    
    receipts = []
    for r in rows:
        receipts.append({
            "date": r[0], "merchant": r[1], "total": r[2],
            "vat_amount": r[3], "vat_rate": r[4], "skr_account": r[5], "ust_idnr": r[6]
        })
    return receipts

# --- LOCALIZATION STRINGS (DE / EN) ---
LANG = {
    "de": {
        "welcome": "Guten Tag! 🇩🇪 Ich bin Ihr KI-Buchhaltungsassistent für Deutschland!\n\n📖 **Funktionsweise:** Senden Sie mir Rechnungen als Foto oder PDF. Ich extrahiere alle Daten nach GoBD & § 14 UStG.\n\n🎁 **Willkommensbonus:** 50 Gratis-Scans!\n\n📊 Guthaben: **{balance}** Scans\n📁 Format: **{format_name}**",
        "consent_text": "🔒 **Datenschutz & Rechtliches (DSGVO / GoBD)**\n\nZur Nutzung dieses Bots und zur Einhaltung der Datenschutzvorgaben (DSGVO) sowie GoBD bestätigen Sie bitte die Nutzungsbedingungen.",
        "accept_btn": "✅ Akzeptieren & Fortfahren",
        "menu_buy": "💳 Scans kaufen",
        "menu_report": "📊 DATEV-Export abrufen",
        "menu_format": "⚙️ Format",
        "menu_lang": "🇬🇧 Switch to English",
        "lang_switched": "Sprache auf Deutsch gewechselt 🇩🇪",
        "no_consent": "❌ **Hinweis:** Bitte akzeptieren Sie zuerst die Nutzungsbedingungen über /start.",
        "no_balance": "❌ **Guthaben aufgebraucht.** Bitte laden Sie Ihr Konto auf.",
        "analyzing": "🔍 *Analysiere Beleg mit Gemini Flash (SKR03/04)...*",
        "success": "✅ **Beleg erfolgreich verarbeitet!**\n\n🏪 **Lieferant:** {merchant}\n📅 **Datum:** {date}\n💵 **Brutto:** €{total:.2f}\n🏛 **MwSt:** €{vat} ({rate}%)\n🏷 **SKR-Konto:** {skr}\n\n📉 Guthaben: **{bal}** Scans"
    },
    "en": {
        "welcome": "Hello! 🇩🇪 I am your German AI Bookkeeping Assistant!\n\n📖 **How it works:** Send me photos or PDFs of your invoices. I extract data according to GoBD & § 14 UStG.\n\n🎁 **Welcome Bonus:** 50 free scans!\n\n📊 Balance: **{balance}** scans\n📁 Format: **{format_name}**",
        "consent_text": "🔒 **Compliance & Privacy (GDPR / GoBD)**\n\nTo use this bot and comply with German data protection standards, please accept the Terms of Service.",
        "accept_btn": "✅ I Accept & Continue",
        "menu_buy": "💳 Buy Scans",
        "menu_report": "📊 Get DATEV Export",
        "menu_format": "⚙️ Format",
        "menu_lang": "🇩🇪 Auf Deutsch wechseln",
        "lang_switched": "Language switched to English 🇬🇧",
        "no_consent": "❌ **Compliance Notice:** You must accept Terms via /start before sending invoices.",
        "no_balance": "❌ **Insufficient scan balance.** Please top up your account.",
        "analyzing": "🔍 *Analyzing document with Gemini Flash (SKR03/04)...*",
        "success": "✅ **Invoice Processed Successfully!**\n\n🏪 **Merchant:** {merchant}\n📅 **Date:** {date}\n💵 **Gross:** €{total:.2f}\n🏛 **VAT:** €{vat} ({rate}%)\n🏷 **SKR Account:** {skr}\n\n📉 Balance: **{bal}** scans"
    }
}

FORMAT_NAMES = {
    "datev_csv": "🇩🇪 DATEV ASCII / CSV Format",
    "lexware_csv": "💼 Lexware Office CSV",
    "simple_csv": "📄 Simple Excel / CSV"
}

PACKAGES = {
    "pkg_300": {"name": "300 Invoices Package", "amount_usd": 45, "scans": 300},
    "pkg_500": {"name": "500 Invoices Package", "amount_usd": 50, "scans": 500},
    "pkg_1000": {"name": "1,000 Invoices Package", "amount_usd": 90, "scans": 1000},
}

def create_takenos_payment_link(user_id: int, pkg_key: str) -> str:
    pkg = PACKAGES[pkg_key]
    headers = {"Authorization": f"Bearer {TAKENOS_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "title": pkg["name"], "amount": pkg["amount_usd"], "currency": "USD",
        "metadata": {"telegram_id": str(user_id), "scans_to_add": str(pkg["scans"])}
    }
    try:
        response = requests.post("https://api.takenos.com/v1/payment-links", json=payload, headers=headers, timeout=5)
        return response.json().get("payment_url", f"https://takenos.com/pay?user={user_id}&pkg={pkg_key}")
    except Exception:
        return f"https://takenos.com/pay?user={user_id}&pkg={pkg_key}"

# --- TELEGRAM HANDLERS ---
@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.chat.id
    user_data = get_user_data(user_id)
    lang = user_data["language"]
    t = LANG[lang]
    
    if user_data["consent_accepted"] == 0:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(t["accept_btn"], callback_data="accept_consent"))
        bot.reply_to(message, t["consent_text"], parse_mode='Markdown', reply_markup=markup)
        return

    show_main_menu(message.chat.id, lang=lang)

def show_main_menu(chat_id, message_id=None, lang="de"):
    user_data = get_user_data(chat_id)
    t = LANG[lang]
    format_name = FORMAT_NAMES.get(user_data["export_format"], "DATEV CSV")
    
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton(t["menu_buy"], callback_data="buy_credits"),
        types.InlineKeyboardButton(t["menu_report"], callback_data="get_report"),
        types.InlineKeyboardButton(t["menu_format"], callback_data="open_format_menu"),
        types.InlineKeyboardButton(t["menu_lang"], callback_data="toggle_lang")
    )
    
    text = t["welcome"].format(balance=user_data["balance"], format_name=format_name)
    if message_id:
        try:
            bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, parse_mode='Markdown', reply_markup=markup)
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, parse_mode='Markdown', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    user_id = call.message.chat.id
    user_data = get_user_data(user_id)
    lang = user_data["language"]
    t = LANG[lang]
    
    if call.data == "accept_consent":
        update_user_field(user_id, "consent_accepted", 1)
        bot.answer_callback_query(call.id, "Accepted!")
        show_main_menu(user_id, call.message.message_id, lang=lang)
        
    elif call.data == "toggle_lang":
        new_lang = "en" if lang == "de" else "de"
        update_user_field(user_id, "language", new_lang)
        bot.answer_callback_query(call.id, LANG[new_lang]["lang_switched"])
        show_main_menu(user_id, call.message.message_id, lang=new_lang)
        
    elif call.data == "buy_credits":
        markup = types.InlineKeyboardMarkup(row_width=1)
        for key, pkg in PACKAGES.items():
            markup.add(types.InlineKeyboardButton(f"📦 {pkg['name']} — ${pkg['amount_usd']} USD", callback_data=f"get_link_{key}"))
        bot.edit_message_text("💳 Select invoice credit package:", chat_id=user_id, message_id=call.message.message_id, parse_mode="Markdown", reply_markup=markup)
        
    elif call.data.startswith("get_link_"):
        pkg_key = call.data.replace("get_link_", "")
        pay_url = create_takenos_payment_link(user_id, pkg_key)
        pkg = PACKAGES[pkg_key]
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(f"👉 Pay ${pkg['amount_usd']} via Takenos", url=pay_url))
        bot.send_message(user_id, f"Click below to complete payment for **{pkg['name']}**:", reply_markup=markup)
        
    elif call.data == "open_format_menu":
        show_format_menu(user_id, call.message.message_id, lang)
        
    elif call.data.startswith("set_fmt_"):
        fmt_key = call.data.replace("set_fmt_", "")
        if fmt_key in FORMAT_NAMES:
            update_user_field(user_id, "export_format", fmt_key)
            bot.answer_callback_query(call.id, "Format saved!")
            show_format_menu(user_id, call.message.message_id, lang)
            
    elif call.data == "get_report":
        generate_report_file(user_id)

def show_format_menu(user_id, message_id, lang):
    user_data = get_user_data(user_id)
    current_fmt = FORMAT_NAMES.get(user_data["export_format"], "DATEV ASCII")
    markup = types.InlineKeyboardMarkup(row_width=1)
    for key, name in FORMAT_NAMES.items():
        prefix = "✅ " if key == user_data["export_format"] else ""
        markup.add(types.InlineKeyboardButton(f"{prefix}{name}", callback_data=f"set_fmt_{key}"))
    bot.edit_message_text(f"⚙️ **Export Format**\nCurrent: **{current_fmt}**", chat_id=user_id, message_id=message_id, parse_mode="Markdown", reply_markup=markup)

def generate_report_file(user_id):
    user_data = get_user_data(user_id)
    fmt = user_data["export_format"]
    receipts = get_user_receipts(user_id)
    
    if not receipts:
        bot.send_message(user_id, "📂 Keine verarbeiteten Belege gefunden. / No processed invoices found.")
        return

    df = pd.DataFrame(receipts)
    
    if fmt == "lexware_csv":
        df_out = pd.DataFrame({
            "Datum": df["date"],
            "Lieferant": df["merchant"],
            "Gesamtbetrag": df["total"],
            "Mehrwertsteuer": df["vat_amount"],
            "SKR_Konto": df["skr_account"]
        })
        filename = "lexware_export.csv"
        df_out.to_csv(filename, index=False, sep=";", encoding="utf-8-sig")
        
    elif fmt == "simple_csv":
        filename = "belege_summary.csv"
        df.to_csv(filename, index=False, encoding="utf-8-sig")
        
    else:
        # DATEV-konformes CSV (Standard ASCII/CSV Struktur)
        df_out = pd.DataFrame({
            "Umsatz (ohne Soll/Haben-Kz)": df["total"],
            "Soll/Haben-Kennzeichen": "S",
            "WKZ": "EUR",
            "Belegdatum": df["date"].str.replace("-", ""),
            "Buchungstext": df["merchant"],
            "Konto": df["skr_account"],
            "Gegenkonto": "1200",
            "USt-IdNr": df["ust_idnr"].fillna("")
        })
        filename = "DATEV_Buchungsstapel.csv"
        df_out.to_csv(filename, index=False, sep=";", encoding="utf-8-sig")

    caption_text = (
        f"📊 **DATEV / Buchhaltungs-Export**\n"
        f"Format: `{FORMAT_NAMES[fmt]}`\n"
        f"Anzahl Belege: `{len(receipts)}`"
    )

    with open(filename, "rb") as file:
        bot.send_document(user_id, file, caption=caption_text, parse_mode="Markdown")

# --- RECEIPT PROCESSING MODULE (Google Gemini with SKR03/SKR04 & § 14 UStG) ---
def process_gemini_sync(downloaded_file, mime_type):
    model = genai.GenerativeModel('gemini-1.5-flash')
    
    prompt = """
    Analyze this German invoice or receipt (Rechnung / Kassenbon). 
    Extract details into a strict JSON object with these exact keys:
    {
      "is_receipt": true,
      "date": "YYYY-MM-DD",
      "merchant": "Company Name",
      "total": 0.00,
      "vat_amount": 0.00,
      "vat_rate": 19.0,
      "skr_account": "Choose an appropriate account from SKR03 or SKR04, e.g., '4900' for general admin costs, '4380' for office supplies, etc.",
      "ust_idnr": "DE123456789 or null if not found"
    }
    
    Rules:
    1. Check for German tax compliance elements (§ 14 UStG like USt-IdNr, gross/net amounts, VAT rate).
    2. VAT rate is usually 19%, 7%, or 0%. Calculate vat_amount if missing but totals are clear.
    3. Return ONLY valid JSON without any markdown formatting or backticks.
    """
    
    image_part = {"mime_type": mime_type, "data": downloaded_file}
    response = model.generate_content([prompt, image_part])
    content = response.text.strip()
    
    if content.startswith("```"):
        content = content.replace("```json", "").replace("```", "").strip()
        
    return json.loads(content)

async def async_gemini_call(downloaded_file, mime_type):
    return await asyncio.to_thread(process_gemini_sync, downloaded_file, mime_type)

@bot.message_handler(content_types=['photo', 'document'])
def handle_receipt_media(message):
    user_id = message.chat.id
    user_data = get_user_data(user_id)
    lang = user_data["language"]
    t = LANG[lang]
    
    if user_data["consent_accepted"] == 0:
        bot.reply_to(message, t["no_consent"], parse_mode="Markdown")
        return

    if user_data["balance"] <= 0:
        bot.reply_to(message, t["no_balance"], parse_mode="Markdown")
        return

    status_msg = bot.reply_to(message, t["analyzing"], parse_mode="Markdown")

    try:
        if message.content_type == 'photo':
            file_info = bot.get_file(message.photo[-1].file_id)
            mime_type = "image/jpeg"
        else:
            file_info = bot.get_file(message.document.file_id)
            mime_type = message.document.mime_type or "application/pdf"

        downloaded_file = bot.download_file(file_info.file_path)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        data = loop.run_until_complete(async_gemini_call(downloaded_file, mime_type))

        if not data.get("is_receipt", True):
            bot.edit_message_text("⚠️ File does not appear to be a valid invoice.", chat_id=user_id, message_id=status_msg.message_id)
            return

        save_receipt_db(
            user_id,
            data.get("date", "N/A"),
            data.get("merchant", "Unknown"),
            float(data.get("total", 0.0)),
            float(data.get("vat_amount", 0.0)),
            float(data.get("vat_rate", 19.0)),
            data.get("skr_account", "4900"),
            data.get("ust_idnr")
        )
        deduct_balance(user_id)
        new_bal = get_user_data(user_id)['balance']

        reply = t["success"].format(
            merchant=data.get('merchant'),
            date=data.get('date'),
            total=float(data.get('total', 0)),
            vat=float(data.get('vat_amount', 0)),
            rate=float(data.get('vat_rate', 19)),
            skr=data.get('skr_account', '4900'),
            bal=new_bal
        )
        bot.edit_message_text(reply, chat_id=user_id, message_id=status_msg.message_id, parse_mode="Markdown")

    except Exception as e:
        bot.edit_message_text(f"⚠️ Error processing file: {str(e)}", chat_id=user_id, message_id=status_msg.message_id)

# --- TAKENOS FASTAPI WEBHOOK ---
@app.post("/takenos-webhook")
async def takenos_webhook(request: Request, x_webhook_secret: str = Header(None)):
    if TAKENOS_WEBHOOK_SECRET and TAKENOS_WEBHOOK_SECRET != "YOUR_WEBHOOK_SECRET_TOKEN":
        if x_webhook_secret != TAKENOS_WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

    payload = await request.json()
    event_type = payload.get("event")
    payment_data = payload.get("data", {})

    if event_type == "payment.succeeded" or payment_data.get("status") == "COMPLETED":
        metadata = payment_data.get("metadata", {})
        telegram_id = metadata.get("telegram_id")
        scans_to_add = metadata.get("scans_to_add")

        if telegram_id and scans_to_add:
            user_id = int(telegram_id)
            amount = int(scans_to_add)
            update_balance(user_id, amount)
            user_data = get_user_data(user_id)

            bot.send_message(
                user_id,
                f"🎉 **Payment Confirmed via Takenos!**\nAdded **{amount}** scans. Total Balance: **{user_data['balance']}**.",
                parse_mode="Markdown",
            )
    return {"status": "ok"}

if __name__ == "__main__":
    threading.Thread(target=lambda: bot.infinity_polling(skip_pending=True), daemon=True).start()
    print("German Accounting Telegram bot started with SQLite WAL, DATEV Export & Async Gemini!")
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
import os
from fastapi import FastAPI
import uvicorn

app = FastAPI()

@app.get("/")
def home():
    return {"status": "Bot is running!"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
