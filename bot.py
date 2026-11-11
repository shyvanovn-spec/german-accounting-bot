import os
import threading
from fastapi import FastAPI
import telebot
import uvicorn

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
bot = telebot.TeleBot(TOKEN) if TOKEN else None

app = FastAPI()

@app.get("/")
def home():
    return {"status": "German Accounting Bot is running!"}

@bot.message_handler(commands=['start'])
def send_welcome(message):
    if bot:
        bot.reply_to(message, "Willkommen! Send your receipt photo for German accounting.")

def run_bot():
    if bot:
        bot.infinity_polling()

if __name__ == "__main__":
    # Запускаем телеграм-бота в отдельном потоке
    if bot:
        t = threading.Thread(target=run_bot)
        t.start()
    
    # Запускаем веб-сервер для Render на порту
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
