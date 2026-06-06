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
# Память диалогов (chat_id -> список сообщений)
conversation_history = {}
MAX_HISTORY = 10


async def get_exchange_rates() -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get("https://open.er-api.com/v6/latest/USD")
            data = resp.json()
            usd_to_krw = data["rates"]["KRW"]
            usd_to_rub = data["rates"]["RUB"]
            usd_to_eur = 1 / data["rates"]["EUR"]
            return {
                "usd_krw": usd_to_krw,
                "usd_rub": usd_to_rub,
                "eur_rub": usd_to_eur * usd_to_rub,
            }
    except:
        return {"usd_krw": 1350, "usd_rub": 1380, "eur_rub": 1500}

SYSTEM_PROMPT = """Ты умный ассистент для экспортёра автомобилей из Кореи в СНГ.
Анализируй запрос и отвечай ТОЛЬКО валидным JSON без markdown.

=== 1. КАЛЬКУЛЯТОР ИМПОРТА в Россию (физлицо) ===
Курсы: $1=1380р, $1=1350 вон, 1 евро=1500р

ТАМОЖНЯ (физлицо, ЕТС):
Берётся МАКСИМУМ из двух значений: процентная ставка И минимальная ставка за см³.

До 3 лет:
- до 8500 евро: макс(54% от цены_евро, 2.5€×см³)
- 8500-16700 евро: макс(48% от цены_евро, 3.5€×см³)
- свыше 16700 евро: макс(48% от цены_евро, 5.5€×см³)

3-5 лет: макс(48% от цены_евро, 3.5€×см³)
5-7 лет: макс(48% от цены_евро, 3.5€×см³)
старше 7 лет (зависит от объёма):
- до 1000см³: макс(48% от цены_евро, 1.4€×см³)
- 1000-1500см³: макс(48% от цены_евро, 1.5€×см³)
- 1500-1800см³: макс(48% от цены_евро, 1.7€×см³)
- 1800-2300см³: макс(48% от цены_евро, 2.5€×см³)
- 2300-3000см³: макс(48% от цены_евро, 2.7€×см³)
- свыше 3000см³: макс(48% от цены_евро, 3.0€×см³)

Плюс таможенное оформление: 4924₽ (фиксировано)

Таможня итого = макс(процент, минимум) × курс_евро_рублей + 4924

УТИЛЬСБОР (физлицо, первая машина):
до 3 лет: 20000 × коэффициент
- до 1000см³: 20000 × 0.26 = 5200₽
- 1000-2000см³: 20000 × 2.74 = 54800₽
- свыше 2000см³: 20000 × 5.63 = 112600₽

3-5 лет: 20000 × коэффициент
- до 1000см³: 20000 × 0.26 = 5200₽
- 1000-2000см³: 20000 × 2.74 = 54800₽
- свыше 2000см³: 20000 × 5.63 = 112600₽

старше 5 лет: 20000 × коэффициент
- до 1000см³: 20000 × 0.26 = 5200₽
- 1000-2000см³: 20000 × 5.63 = 112600₽
- свыше 2000см³: 20000 × 8.45 = 169000₽

Фиксированные: брокер 25000р + логистика 150000р + услуги 100000р = 275000р
Итого = цена_руб + таможня + утилсбор + 275000

Формат ответа:
{"intent":"calc","reply":"итог текстом","data":{"car":"название","price_krw":число,"price_rub":число,"price_eur":число,"customs_rub":число,"util_rub":число,"broker_rub":25000,"logistics_rub":150000,"service_rub":100000,"total_rub":число,"usd_krw":число,"usd_rub":число,"eur_rub":число}}

Если запрос содержит цену авто И расходы по Корее (фрахт) — используй intent "full_calc":
{"intent":"full_calc","reply":"итог","data":{"car":"название","year":число,"age":"new|3-5|5-7|7+","engine_cc":число,"engine_type":"бензин|дизель|гибрид|электро","price_krw":число,"korea_expenses_krw":число,"total_krw":число,"price_usd":число,"price_rub":число,"customs_rub":число,"util_rub":число,"delivery_msk_rub":число,"usd_krw":число,"usd_rub":число,"eur_rub":число}}

Если не хватает данных для full_calc (нет объёма или возраста) — используй intent "clarify".

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


def calculate_customs(price_krw: float, engine_cc: int, age: str, engine_type: str, rates: dict) -> dict:
    usd_krw = rates["usd_krw"]
    usd_rub = rates["usd_rub"]
    eur_rub = rates["eur_rub"]
    
    price_usd = price_krw / usd_krw
    price_eur = price_usd / 1.09
    
    # Таможенная пошлина (единая ставка за см³)
    if age == "new":  # до 3 лет
        if price_eur <= 8500:
            rate_eur = 2.5
        elif price_eur <= 16700:
            rate_eur = 3.5
        else:
            rate_eur = 5.5
    elif age in ["3-5", "5-7"]:
        rate_eur = 2.5
    else:  # старше 7 лет
        if engine_cc <= 1000:
            rate_eur = 1.4
        elif engine_cc <= 1500:
            rate_eur = 1.5
        elif engine_cc <= 1800:
            rate_eur = 1.7
        elif engine_cc <= 2300:
            rate_eur = 2.5
        elif engine_cc <= 3000:
            rate_eur = 2.7
        else:
            rate_eur = 3.0
    
    customs = round(rate_eur * engine_cc * eur_rub + 4924)
    
    # Утильсбор (физлицо, первая машина)
    util = 5200
    
    return {
        "customs_rub": customs,
        "util_rub": util,
        "price_usd": round(price_usd),
        "price_rub": round(price_usd * usd_rub),
        "price_eur": round(price_eur),
    }


async def ask_claude(user_message: str, chat_id: int) -> dict:
    now = datetime.now()
    weekdays = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    system = SYSTEM_PROMPT.replace("{today}", now.strftime("%d.%m.%Y")).replace("{weekday}", weekdays[now.weekday()])

    if chat_id not in conversation_history:
        conversation_history[chat_id] = []

    conversation_history[chat_id].append({"role": "user", "content": user_message})

    if len(conversation_history[chat_id]) > MAX_HISTORY * 2:
        conversation_history[chat_id] = conversation_history[chat_id][-MAX_HISTORY * 2:]

    rates = await get_exchange_rates()
    rates_info = f"Актуальные курсы: $1={rates['usd_krw']:.0f}₩, $1={rates['usd_rub']:.2f}₽, €1={rates['eur_rub']:.2f}₽"
    system = system + f"\n\nИСПОЛЬЗУЙ ЭТИ АКТУАЛЬНЫЕ КУРСЫ ДЛЯ РАСЧЁТА: {rates_info}"

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
                "messages": conversation_history[chat_id],
            },
        )
        data = resp.json()
        logger.info(f"Anthropic API response: {data}")
        if "error" in data:
            raise Exception(f"API error: {data['error']}")
        raw = data["content"][0]["text"].strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        logger.info(f"Claude raw response: {raw}")
        parsed = json.loads(raw.strip())
        logger.info(f"Claude parsed: {parsed}")
        conversation_history[chat_id].append({"role": "assistant", "content": raw})
        return parsed


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
    price_krw = data.get("price_krw", 0)
    price_usd = round(price_krw / 1350)
    price_eur = round(price_usd * 0.92)
    price_rub = data.get("price_rub", 0)
    customs_rub = data.get("customs_rub", 0)
    util_rub = data.get("util_rub", 0)
    total_rub = data.get("total_rub", 0)
    lines = [
        f"🚗 *{car}*",
        f"",
        f"📊 *Курсы:* $1 = {data.get('usd_krw', 1350):.0f}₩ | $1 = {data.get('usd_rub', 1380):.2f}₽ | €1 = {data.get('eur_rub', 1500):.2f}₽",
        f"",
        f"💰 Цена авто: {fmt(price_krw)}₩ → ~${fmt(price_usd)} → {fmt(price_rub)}₽",
        f"🛃 Таможенная пошлина: {fmt(customs_rub)}₽",
        f"♻️ Утильсбор: {fmt(util_rub)}₽",
        f"📋 Брокер: 25 000₽",
        f"🚢 Логистика: 150 000₽",
        f"🏢 Услуги компании: 100 000₽",
        f"",
        f"✅ *Итого под ключ: {fmt(total_rub)}₽*",
    ]
    return "\n".join(lines)


def format_full_calc(data: dict) -> str:
    def fmt(n):
        return f"{int(n):,}".replace(",", " ")
    
    car = data.get("car", "Авто")
    price_krw = data.get("price_krw", 0)
    korea_exp = data.get("korea_expenses_krw", 0)
    total_krw = price_krw + korea_exp
    usd_krw = data.get("usd_krw", 1350)
    usd_rub = data.get("usd_rub", 1380)
    eur_rub = data.get("eur_rub", 1500)
    total_usd = round(total_krw / usd_krw)
    total_rub = round(total_usd * usd_rub)
    customs = data.get("customs_rub", 0)
    util = data.get("util_rub", 0)
    broker = 110000
    contract = 100000
    delivery = data.get("delivery_msk_rub", 0)
    total_vldk = total_rub + customs + util + broker + contract
    total_msk = total_vldk + delivery

    lines = [
        f"🚗 *{car}*",
        f"",
        f"📊 *Курсы:* $1 = {usd_krw:.0f}₩ | $1 = {usd_rub:.2f}₽ | €1 = {eur_rub:.2f}₽",
        f"",
        f"🇰🇷 *Корея*",
        f"Цена авто: {fmt(price_krw)}₩",
        f"Расходы + фрахт: {fmt(korea_exp)}₩",
        f"Итого KRW: {fmt(total_krw)}₩ → ~${fmt(total_usd)} → {fmt(total_rub)}₽",
        f"",
        f"🇷🇺 *Россия*",
        f"Таможня: {fmt(customs)}₽",
        f"Утильсбор: {fmt(util)}₽",
        f"Брокерские: {fmt(broker)}₽",
        f"Договор за услугу: {fmt(contract)}₽",
        f"",
        f"📦 *Total ВДК: {fmt(total_vldk)}₽*",
    ]
    if delivery > 0:
        lines.append(f"🚛 Доставка ВДК→МСК: {fmt(delivery)}₽")
        lines.append(f"🏁 *Total МСК: {fmt(total_msk)}₽*")
    
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
        result = await ask_claude(text, update.message.chat_id)
        intent = result.get("intent")
        reply = result.get("reply", "")
        data = result.get("data", {})

        if intent == "calc":
            rates = await get_exchange_rates()
            calc = calculate_customs(
                price_krw=data.get("price_krw", 0),
                engine_cc=data.get("engine_cc", 1600),
                age=data.get("age", "3-5"),
                engine_type=data.get("engine_type", "бензин"),
                rates=rates,
            )
            data.update(calc)
            data["usd_krw"] = rates["usd_krw"]
            data["usd_rub"] = rates["usd_rub"]
            data["eur_rub"] = rates["eur_rub"]
            msg = format_calc_result(data, data.get("reply", ""))
            await update.message.reply_text(msg, parse_mode="Markdown")

        elif intent == "full_calc":
            rates = await get_exchange_rates()
            calc = calculate_customs(
                price_krw=data.get("total_krw", data.get("price_krw", 0)),
                engine_cc=data.get("engine_cc", 1600),
                age=data.get("age", "3-5"),
                engine_type=data.get("engine_type", "бензин"),
                rates=rates,
            )
            data.update(calc)
            data["usd_krw"] = rates["usd_krw"]
            data["usd_rub"] = rates["usd_rub"]
            data["eur_rub"] = rates["eur_rub"]
            msg = format_full_calc(data)
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
