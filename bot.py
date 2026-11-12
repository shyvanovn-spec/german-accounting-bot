import os
import telebot
from PIL import Image
import google.generativeai as genai
from flask import Flask, request

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

bot = telebot.TeleBot(TOKEN, threaded=False)
app = Flask(__name__)

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_model = genai.GenerativeModel("gemini-1.5-flash")
else:
    gemini_model = None

@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    json_string = request.get_data().decode("utf-8")
    update = telebot.types.Update.de_json(json_string)
    bot.process_new_updates([update])
    return "OK", 200

@app.route("/", methods=["GET"])
def index():
    return "Bot is running!"

@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "Willkommen! Senden Sie Ihren Beleg als Foto oder Dokument.")

@bot.message_handler(content_types=['photo', 'document'])
def handle_receipt(message):
    if not gemini_model:
        bot.reply_to(message, "⚠️ Fehler: GEMINI_API_KEY ist nicht konfiguriert.")
        return
    
    processing_msg = bot.reply_to(message, "⏳ Analysiere Beleg (GoBD/DSGVO)...")
    
    try:
        if message.photo:
            file_info = bot.get_file(message.photo[-1].file_id)
        else:
            file_info = bot.get_file(message.document.file_id)
            
        downloaded_file = bot.download_file(file_info.file_path)
        
        temp_filename = "/tmp/receipt.jpg"
        with open(temp_filename, 'wb') as f:
            f.write(downloaded_file)
            
        img = Image.open(temp_filename)
        
        prompt = (
            "Analyze this receipt for German accounting (GoBD compliant). "
            "Extract: 1. Vendor Name, 2. Date, 3. Total Amount, "
            "4. Tax breakdown (MwSt 7% and 19%). Answer clearly in German."
        )
        
        response = gemini_model.generate_content([img, prompt])
        
        if os.path.exists(temp_filename):
            os.remove(temp_filename)
            
        bot.edit_message_text(response.text, chat_id=message.chat.id, message_id=processing_msg.message_id)
        
    except Exception as e:
        bot.edit_message_text(f"❌ Fehler: {str(e)}", chat_id=message.chat.id, message_id=processing_msg.message_id)
