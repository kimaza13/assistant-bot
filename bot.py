import os
import json
import logging
import tempfile
from datetime import datetime, timedelta
import httpx
import groq
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_TOKEN_JSON = os.environ["GOOGLE_TOKEN_JSON"]
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")

groq_client = groq.Groq(api_key=GROQ_API_KEY)

SYSTEM_PROMPT = """Ты умный ассистент для экспортёра автомобилей из Кореи в СНГ.
Анализируй запрос и отвечай ТОЛЬКО валидным JSON без markdown.

=== 1. КАЛЬКУЛЯТОР ИМПОРТА в Россию (физлицо) ===
Курсы: $1=1380р, $1=1350 вон, 1 евро=1500р

Таможня от цены в евро, до 3 лет:
- до 8500 евро: 54%, мин 2.5 евро/см3
- 8500-16700 евро: 48%, мин 3.5 евро/см3
- свыше 16700 евро: 48%, мин 5.5 евро/см3
3-5 лет: 48%, мин 3.5 евро/см3
старше 5 лет: 48%, мин 1.4-7.5 евро/см3

Утилсбор: до3л до1000см3=19482р, до3л 1000-2000=54808р, до3л свыше2000=93500р, 3-5л до1000=96900р, 3-5л 1000-2000=144840р, 3-5л свыше2000=238680р, старше5л умножай на 2.5

Фиксированные: брокер 25000р + логистика 150000р + услуги 100000р = 275000р
Итого = цена_руб + таможня + утилсбор + 275000

Формат ответа:
{"intent":"calc","reply":"итог текстом","data":{"car":"название","price_krw":число,"price_rub":число,"price_eur":число,"customs_rub":число,"util_rub":число,"broker_rub":25000,"logistics_rub":150000,"service_rub":100000,"total_rub":число}}

=== 2. КАРТОЧКА ДИЛЕРА ===
Когда называют цену авто, медоби, торг и залог — считай остаток дилеру.
Формула: остаток_база = цена - залог - торг, итого = остаток_база + медоби

Формат ответа:
{"intent":"dealer","reply":"карточка готова","data":{"car":"название если есть","price_krw":число,"medobi_krw":число,"torg_krw":число,"zalog_krw":число}}

=== 3. КАЛЕНДАРЬ ===
Сегодня: {today}, день недели: {weekday}.
Понимай: завтра, послезавтра, в пятницу, через N дней, конкретные даты.

Формат ответа:
{"intent":"calendar","reply":"подтверждение","data":{"title":"название","date":"YYYY-MM-DD","time":"HH:MM","duration_hours":1}}

=== 4. УТОЧНЕНИЕ ===
{"intent":"clarify","reply":"какой вопрос задать"}

=== 5. ЧАТ ===
{"intent":"chat","reply":"ответ"}"""


def get_calendar_service():
    token_data = json.loads(GOOGLE_TOKEN_JSON)
    creds = Credentials(
        token=token_data.get("token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri=token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=token_data.get("client_id"),
        client_secret=token_data.get("client_secret"),
        scopes=token_data.get("scopes", ["https://www.googleapis.com/auth/calendar"]),
    )
    return build("calendar", "v3", credentials=creds)


async def transcribe_voice(file_path: str) -> str:
    with open(file_path, "rb") as f:
        result = groq_client.audio.transcriptions.create(
            file=("voice.ogg", f),
            model="whisper-large-v3",
            language="ru",
        )
    return result.text.strip()


async def ask_claude(user_message: str) -> dict:
    now = datetime.now()
    weekdays = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    system = SYSTEM_PROMPT.format(
        today=now.strftime("%d.%m.%Y"),
        weekday=weekdays[now.weekday()]
    )
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1024,
                "system": system,
                "messages": [{"role": "user", "content": user_message}],
            },
        )
        data = resp.json()
        raw = data["content"][0]["text"].strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())


def create_calendar_event(title: str, date: str, time: str, duration_hours: float = 1) -> str:
    service = get_calendar_service()
    start_dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
    end_dt = start_dt + timedelta(hours=duration_hours)
    event = {
        "summary": title,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "Asia/Seoul"},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": "Asia/Seoul"},
        "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 15}]},
    }
    result = service.events().insert(calendarId="primary", body=event).execute()
    return result.get("htmlLink", "")


def format_dealer_card(data: dict) -> str:
    def fmt(n):
        return f"{int(n):,}".replace(",", ",")
    price = data["price_krw"]
    medobi = data["medobi_krw"]
    torg = data["torg_krw"]
    zalog = data["zalog_krw"]
    ostatok_base = price - zalog - torg
    ostatok_total = ostatok_base + medobi
    lines = [
        f"💳 *Карточка авто*",
        f"",
        f"Цена авто: по сайту {fmt(price)}",
        f"Медоби: {fmt(medobi)}",
        f"Торг: {fmt(torg)}",
        f"Залог: {fmt(zalog)}",
        f"",
        f"*Остаток дилеру: {fmt(ostatok_base)} + {fmt(medobi)} = {fmt(ostatok_total)}*",
    ]
    return "\n".join(lines)


def format_calc_result(data: dict, reply: str) -> str:
    def fmt(n):
        return f"{int(n):,}".replace(",", " ")
    car = data.get("car", "Авто")
    lines = [
        f"🚗 *{car}*",
        f"",
        f"💰 Цена авто: {fmt(data['price_rub'])}₽  (~{fmt(data['price_krw'])}₩)",
        f"🛃 Таможня: {fmt(data['customs_rub'])}₽",
        f"♻️ Утилсбор: {fmt(data['util_rub'])}₽",
        f"📋 Брокер: {fmt(data.get('broker_rub', 25000))}₽",
        f"🚢 Логистика: {fmt(data.get('logistics_rub', 150000))}₽",
        f"🏢 Услуги: {fmt(data.get('service_rub', 100000))}₽",
        f"",
        f"✅ *Итого под ключ: {fmt(data['total_rub'])}₽*",
    ]
    return "\n".join(lines)


def format_calendar_result(data: dict, reply: str) -> str:
    date_str = data.get("date", "")
    time_str = data.get("time", "")
    title = data.get("title", "")
    try:
        dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        weekdays = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
        date_fmt = f"{dt.strftime('%d.%m.%Y')} ({weekdays[dt.weekday()]})"
    except:
        date_fmt = f"{date_str} {time_str}"
    return f"📅 *{title}*\n🕐 {date_fmt} в {time_str}\n\n✅ Добавлено в календарь"


async def process_message(update: Update, text: str):
    try:
        result = await ask_claude(text)
        intent = result.get("intent")
        reply = result.get("reply", "")
        data = result.get("data", {})

        if intent == "calc":
            msg = format_calc_result(data, reply)
            await update.message.reply_text(msg, parse_mode="Markdown")

        elif intent == "dealer":
            msg = format_dealer_card(data)
            await update.message.reply_text(msg, parse_mode="Markdown")

        elif intent == "calendar":
            try:
                create_calendar_event(
                    title=data["title"],
                    date=data["date"],
                    time=data["time"],
                    duration_hours=data.get("duration_hours", 1),
                )
                msg = format_calendar_result(data, reply)
                await update.message.reply_text(msg, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Calendar error: {e}")
                await update.message.reply_text(f"❌ Ошибка календаря: {e}")

        elif intent == "clarify":
            await update.message.reply_text(f"🤔 {reply}")

        else:
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
        "👋 Привет! Я твой ассистент.\n\n"
        "Пиши или говори голосом:\n"
        "🚗 *Калькулятор импорта* — стоимость авто под ключ в Россию\n"
        "💳 *Карточка дилера* — остаток дилеру по цене, медоби, торгу и залогу\n"
        "📅 *Календарь* — добавить встречу или созвон\n\n"
        "Примеры:\n"
        "• _Sonata 2022, 2.0л, 150лс, 25 млн вон_\n"
        "• _Мини Купер, цена 43400000, медоби 440000, торг 440000, залог 1000000_\n"
        "• _Завтра в 10 созвон с Рашитом_"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
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
