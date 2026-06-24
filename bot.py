import asyncio
import requests
import base64
import json
import re
import hashlib
import os
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

BOT_TOKEN = "8838770649:AAFytEaWcGTDtOURYI7GSVkOzBi_8LHBnaI"
CHANNEL_ID = "@winesandl"
OPENROUTER_API_KEY = "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"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "google/gemini-1.5-flash"

LANG_MAP = {
    "ru": "Russian", "kk": "Kazakh", "pt": "Portuguese",
    "en": "English", "fr": "French", "de": "German",
    "es": "Spanish", "it": "Italian", "uk": "Ukrainian",
}

response_cache = {}
cache_lock = asyncio.Lock()
api_semaphore = asyncio.Semaphore(3)
last_result = {}

# ── Веб-сервер для Render ──────────────────────────────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def start_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"Health server running on port {port}")
    server.serve_forever()

# ── Промпты ────────────────────────────────────────────────────────────────
def build_system_prompt(lang_name: str) -> str:
    return f"""Ты — эксперт мирового уровня... (весь текст как в предыдущем ответе)"""

def build_user_prompt(lang_name: str) -> str:
    return f"""Внимательно изучи это изображение... (весь текст как в предыдущем ответе)"""

def safe_parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r'^```\w*\n?', '', text)
        text = re.sub(r'\n?```$', '', text)
        text = text.strip()
    match = re.search(r'\{[\s\S]*\}', text)
    if match:
        text = match.group(0)
    text = re.sub(r"(?<!\\)'", '"', text)
    text = re.sub(r",\s*}", "}", text)
    text = re.sub(r",\s*]", "]", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"type": "wine", "name": "Не удалось распознать", "criticReview": f"Ответ ИИ (не JSON): {text[:300]}"}

# ── Форматтеры ─────────────────────────────────────────────────────────────
def format_wine(w: dict) -> str:
    lines = [f"🍷 *{w.get('name', 'Вино')}*"]
    if w.get("producer"): lines.append(f"🏭 {w['producer']}")
    if w.get("holdingGroup"): lines.append(f"🏢 Холдинг: {w['holdingGroup']}")
    loc = " · ".join(filter(None, [w.get("region"), w.get("country")]))
    if loc: lines.append(f"📍 {loc}")
    if w.get("grapeVariety"): lines.append(f"🍇 Сорт: {w['grapeVariety']}")
    if w.get("vintage"): lines.append(f"📅 Год: {w['vintage']}")
    if w.get("alcoholPercent"): lines.append(f"🍸 Крепость: {w['alcoholPercent']}")
    chars = []
    if w.get("body"): chars.append(f"Тело: {w['body']}")
    if w.get("tannin"): chars.append(f"Танины: {w['tannin']}")
    if w.get("acidity"): chars.append(f"Кислотность: {w['acidity']}")
    if w.get("sweetness"): chars.append(f"Сладость: {w['sweetness']}")
    if chars: lines.append("📝 " + " · ".join(chars))
    ratings = []
    if w.get("parkerRating"): ratings.append(f"  🔴 Паркер: {w['parkerRating']}")
    if w.get("sucklingRating"): ratings.append(f"  🔵 Саклинг: {w['sucklingRating']}")
    if w.get("robinsonRating"): ratings.append(f"  🟣 Дж. Робинсон: {w['robinsonRating']}")
    if w.get("worldRating") and not ratings:
        ratings.append(f"  ⭐ Общая: {w['worldRating']}")
    if ratings:
        lines.append("🏅 *Рейтинги критиков:*")
        lines.extend(ratings)
    if w.get("artist"): lines.append(f"🎨 Художник этикетки: *{w['artist']}*")
    if w.get("criticReview"): lines.append(f"\n✍️ *Обзор сомелье:*\n{w['criticReview']}")
    if w.get("aromaticProfile"): lines.append(f"\n👃 *Ароматический профиль:*\n{w['aromaticProfile']}")
    if w.get("foodPairing"): lines.append(f"\n🍴 *Гастрономия:*\n{w['foodPairing']}")
    return "\n".join(lines)

def format_beer(b: dict) -> str:
    lines = [f"🍺 *{b.get('name', 'Пиво')}*"]
    if b.get("brewery"): lines.append(f"🏭 Пивоварня: {b['brewery']}")
    if b.get("style"): lines.append(f"📌 Стиль: {b['style']}")
    if b.get("country"): lines.append(f"📍 Страна: {b['country']}")
    stats = []
    if b.get("abv"): stats.append(f"ABV: {b['abv']}")
    if b.get("ibu"): stats.append(f"IBU: {b['ibu']}")
    if b.get("originalGravity"): stats.append(f"OG: {b['originalGravity']}")
    if b.get("srm"): stats.append(f"SRM: {b['srm']}")
    if stats: lines.append("📊 " + " · ".join(stats))
    if b.get("description"): lines.append(f"\n🍻 *Вкус и аромат:*\n{b['description']}")
    if b.get("foodPairing"): lines.append(f"\n🍴 *Гастрономия:*\n{b['foodPairing']}")
    if b.get("awards"): lines.append(f"🏆 Награды: {b['awards']}")
    return "\n".join(lines)

def format_spirit(s: dict) -> str:
    name = s.get("name", "Напиток")
    spirit_type = s.get("spiritType", s.get("type", "")).lower()
    emoji = {"whisky": "🥃", "whiskey": "🥃", "cognac": "🥃",
             "rum": "🍹", "vodka": "🫙", "gin": "🍸",
             "tequila": "🌵", "mezcal": "🌵", "calvados": "🍎",
             "armagnac": "🥃", "brandy": "🥃", "sake": "🍶",
             "liqueur": "🍬"}.get(spirit_type, "🥃")
    lines = [f"{emoji} *{name}*"]
    if s.get("producer"): lines.append(f"🏭 Производитель: {s['producer']}")
    type_parts = []
    if s.get("spiritType"): type_parts.append(s["spiritType"])
    if s.get("subtype"): type_parts.append(s["subtype"])
    if type_parts: lines.append(f"📌 Тип: {' · '.join(type_parts)}")
    loc = " · ".join(filter(None, [s.get("region"), s.get("country")]))
    if loc: lines.append(f"📍 {loc}")
    if s.get("alcoholPercent"): lines.append(f"🔥 Крепость: {s['alcoholPercent']}")
    if s.get("rawMaterial"): lines.append(f"🌾 Сырьё: {s['rawMaterial']}")
    if s.get("distillationMethod"): lines.append(f"⚗️ Дистилляция: {s['distillationMethod']}")
    if s.get("agingInfo"): lines.append(f"🪵 Выдержка: {s['agingInfo']}")
    if s.get("ricePolishingRatio"): lines.append(f"🌾 Шлифовка риса: {s['ricePolishingRatio']}")
    if s.get("sakeType"): lines.append(f"🍶 Тип: {s['sakeType']}")
    if s.get("baseSpirit"): lines.append(f"🧪 Основа: {s['baseSpirit']}")
    if s.get("sweetnessLevel"): lines.append(f"🍬 Сладость: {s['sweetnessLevel']}")
    if s.get("criticsAvgScore"): lines.append(f"🏅 Оценка критиков: {s['criticsAvgScore']}")
    if s.get("brandLegend"): lines.append(f"\n📖 *История бренда:*\n{s['brandLegend']}")
    if s.get("aromaProfile"): lines.append(f"\n👃 *Аромат:*\n{s['aromaProfile']}")
    if s.get("tasteProfile"): lines.append(f"\n👅 *Вкус:*\n{s['tasteProfile']}")
    if s.get("finish"): lines.append(f"\n✨ *Послевкусие:*\n{s['finish']}")
    if s.get("foodPairing"): lines.append(f"\n🍴 *Гастрономия:*\n{s['foodPairing']}")
    return "\n".join(lines)

def format_food(f: dict) -> str:
    lines = [f"🍽️ *{f.get('name', 'Продукт')}*"]
    if f.get("producer"): lines.append(f"🏭 Производитель: {f['producer']}")
    if f.get("country"): lines.append(f"📍 Страна: {f['country']}")
    if f.get("cocoaPercent"): lines.append(f"🍫 Какао: {f['cocoaPercent']}")
    if f.get("origin"): lines.append(f"🌍 Происхождение какао: {f['origin']}")
    if f.get("ingredients"): lines.append(f"📋 Состав: {f['ingredients']}")
    if f.get("isVegan") is True or str(f.get("isVegan", "")).lower() == "true":
        lines.append("🌱 Веганский продукт")
    kbju = []
    if f.get("calories"): kbju.append(f"⚡ {f['calories']} ккал")
    if f.get("protein"):  kbju.append(f"💪 {f['protein']} г белка")
    if f.get("fat"):      kbju.append(f"🧈 {f['fat']} г жиров")
    if f.get("carbs"):    kbju.append(f"🍞 {f['carbs']} г углев.")
    if kbju: lines.append("📊 КБЖУ (100г): " + " · ".join(kbju))
    if f.get("healthRating"): lines.append(f"💚 Рейтинг полезности: {f['healthRating']}")
    if f.get("dishDescription"): lines.append(f"\n🍴 *Описание блюда:*\n{f['dishDescription']}")
    if f.get("drinkPairing"): lines.append(f"\n🍷 *Пейринг с напитками:*\n{f['drinkPairing']}")
    return "\n".join(lines)

def format_cigar(c: dict) -> str:
    cigar_type = c.get("cigarType", "cigar").lower()
    emoji = "💨" if "cigarillo" in cigar_type or "cigarello" in cigar_type else "🚬"
    type_name = "Сигарилла" if "cigarillo" in cigar_type or "cigarello" in cigar_type else "Сигара"
    lines = [f"{emoji} *{c.get('name', type_name)}*"]
    if c.get("producer"): lines.append(f"🏭 Производитель: {c['producer']}")
    if c.get("country"): lines.append(f"📍 Страна: {c['country']}")
    lines.append(f"📌 Тип: {type_name}")
    leaves = []
    if c.get("wrapper"): leaves.append(f"Покров: {c['wrapper']}")
    if c.get("binder"):  leaves.append(f"Связка: {c['binder']}")
    if c.get("filler"):  leaves.append(f"Наполнитель: {c['filler']}")
    if leaves: lines.append("🍃 " + " · ".join(leaves))
    if c.get("strength"): lines.append(f"💪 Крепость: {c['strength']}")
    size_parts = []
    if c.get("length"):    size_parts.append(f"длина {c['length']}")
    if c.get("ringGauge"): size_parts.append(f"кольцо {c['ringGauge']}")
    if size_parts: lines.append(f"📏 Размер: {', '.join(size_parts)}")
    if c.get("smokingTime"):  lines.append(f"⏱️ Время курения: {c['smokingTime']}")
    if c.get("tastingNotes"): lines.append(f"\n👃 *Вкус и аромат:*\n{c['tastingNotes']}")
    if c.get("pairingDrinks"):lines.append(f"\n🥃 *Пейринг с напитками:*\n{c['pairingDrinks']}")
    return "\n".join(lines)

# ── Обработчики Telegram ────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🍷 *Vinito Scanner Bot*\n\nЭксперт мирового уровня по вину, пиву, крепкому алкоголю, еде и сигарам.\n\n📸 Отправь фото — получи полный профессиональный анализ.",
        parse_mode='Markdown'
    )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_result
    await update.message.reply_text("🔍 Анализирую этикетку...")
    user_lang = update.effective_user.language_code or "ru"
    lang_name = LANG_MAP.get(user_lang[:2], "Russian")
    try:
        photo_file = await update.message.photo[-1].get_file()
        photo_bytes = await photo_file.download_as_bytearray()
        b64_image = base64.b64encode(photo_bytes).decode('utf-8')
        cache_key = hashlib.sha256(b64_image.encode()).hexdigest()[:64]

        async with cache_lock:
            if cache_key in response_cache:
                result_text = response_cache[cache_key]
                await update.message.reply_text(result_text, parse_mode='Markdown')
                last_result[update.effective_chat.id] = result_text
                return

        async with api_semaphore:
            payload = {
                "model": MODEL,
                "messages": [
                    {"role": "system", "content": build_system_prompt(lang_name)},
                    {"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_image}"}},
                        {"type": "text", "text": build_user_prompt(lang_name)}
                    ]}
                ],
                "max_tokens": 2000, "temperature": 0.1
            }
            response = requests.post(OPENROUTER_URL, headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "HTTP-Referer": "https://t.me/vinitoscan_bot",
                "X-Title": "Vinito Scanner"
            }, json=payload, timeout=90)

            if response.status_code != 200:
                await update.message.reply_text(f"❌ Ошибка API ({response.status_code}):\n{response.text[:300]}")
                return

            data = response.json()
            content = data["choices"][0]["message"]["content"]
            parsed = safe_parse_json(content)
            product_type = parsed.get("type", "wine").lower()

            if product_type == "wine": result_text = format_wine(parsed)
            elif product_type == "beer": result_text = format_beer(parsed)
            elif product_type in ("spirit","whisky","whiskey","vodka","rum","gin","cognac","brandy","calvados","tequila","mezcal","sake","liqueur","armagnac"): result_text = format_spirit(parsed)
            elif product_type in ("cigar","cigarillo","cigarello","tobacco"): result_text = format_cigar(parsed)
            elif product_type == "food": result_text = format_food(parsed)
            else: result_text = format_food(parsed)

            async with cache_lock:
                response_cache[cache_key] = result_text

            await update.message.reply_text(result_text, parse_mode='Markdown')
            last_result[update.effective_chat.id] = result_text
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def post_to_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in last_result:
        try:
            await context.bot.send_message(chat_id=CHANNEL_ID, text=last_result[chat_id], parse_mode='Markdown')
            await update.message.reply_text("✅ Отправлено в канал!")
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")
    else:
        await update.message.reply_text("Сначала отправь фото для анализа.")

def main():
    Thread(target=start_health_server, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("post", post_to_channel))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    print("✅ Vinito Bot запущен...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
