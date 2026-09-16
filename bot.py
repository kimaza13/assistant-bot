import os
import logging
import tempfile

import groq
import httpx
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

groq_client = groq.Groq(api_key=GROQ_API_KEY)

# Память диалогов (chat_id -> список сообщений)
conversation_history = {}
MAX_HISTORY = 10

SYSTEM_PROMPT = (
    "Ты — умный личный ассистент Азамата. Отвечай естественно и по делу, "
    "как человек, а не как справочник. Азамат работает в экспорте авто из "
    "Кореи в Россию и СНГ, а также развивает бизнес по оптовым продажам "
    "корейской косметики (K-beauty) — используй этот контекст, когда вопрос "
    "касается его дел, но одинаково свободно отвечай на любые другие темы. "
    "Отвечай на том языке, на котором пишет собеседник, если явно не "
    "попросили иначе. Пиши обычным текстом, без markdown-разметки и без JSON."
)


async def transcribe_voice(file_path: str) -> str:
    with open(file_path, "rb") as f:
        result = groq_client.audio.transcriptions.create(
            file=("voice.ogg", f),
            model="whisper-large-v3",
            language="ru",
        )
    return result.text.strip()


async def ask_claude(user_message: str, chat_id: int) -> str:
    if chat_id not in conversation_history:
        conversation_history[chat_id] = []

    conversation_history[chat_id].append({"role": "user", "content": user_message})

    if len(conversation_history[chat_id]) > MAX_HISTORY * 2:
        conversation_history[chat_id] = conversation_history[chat_id][-MAX_HISTORY * 2:]

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 1024,
                "system": SYSTEM_PROMPT,
                "messages": conversation_history[chat_id],
            },
        )
        data = resp.json()
        if "error" in data:
            raise Exception(f"API error: {data['error']}")
        reply = data["content"][0]["text"].strip()
        conversation_history[chat_id].append({"role": "assistant", "content": reply})
        return reply


async def process_message(update: Update, text: str):
    try:
        reply = await ask_claude(text, update.message.chat_id)
        await update.message.reply_text(reply)
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text("❌ Что-то пошло не так. Попробуй ещё раз.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    await update.message.chat.send_action("typing")
    await process_message(update, text)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action("typing")
    voice = update.message.voice
    file = await context.bot.get_file(voice.file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        text = await transcribe_voice(tmp.name)
    await update.message.reply_text(f"🎤 _{text}_", parse_mode="Markdown")
    await process_message(update, text)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 Привет! Я твой личный AI-ассистент.\n\n"
        "Пиши текстом или голосом — отвечу на любой вопрос, помогу "
        "разобраться в делах по авто-экспорту, K-beauty или чём угодно ещё.\n\n"
        "/reset — очистить историю диалога"
    )
    await update.message.reply_text(msg)


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    if chat_id in conversation_history:
        conversation_history[chat_id] = []
    await update.message.reply_text("🔄 История диалога очищена")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    if WEBHOOK_URL:
        app.run_webhook(
            listen="0.0.0.0",
            port=int(os.environ.get("PORT", 8000)),
            webhook_url=f"{WEBHOOK_URL}/webhook",
            url_path="/webhook",
        )
    else:
        app.run_polling()


if __name__ == "__main__":
    main()
