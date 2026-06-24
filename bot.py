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

# ──────────────────────────────────────────────────────────────────────────────
# Веб-сервер для проверки живости (Render требует слушать порт)
# ──────────────────────────────────────────────────────────────────────────────
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

# ──────────────────────────────────────────────────────────────────────────────
# Промпты и форматтеры (без изменений)
# ──────────────────────────────────────────────────────────────────────────────
def build_system_prompt(lang_name: str) -> str:
    return f"""Ты — эксперт мирового уровня, объединяющий знания:
- Master Sommelier и Master of Wine с 30-летним стажем
- Сертифицированный пивной судья (BJCP Grand Master)
- Профессионал по крепкому алкоголю (WSET Diploma, WSG)
- Сертифицированный сигарный сомелье
- Шеф-повар мишленовского ресторана
- Историк искусства, специализирующийся на художниках винных этикеток

ЖЁСТКИЕ ПРАВИЛА:
0. ТЫ ВОЗВРАЩАЕШЬ ТОЛЬКО ЧИСТЫЙ JSON. НИКАКОГО ТЕКСТА ДО ИЛИ ПОСЛЕ. НИКАКОГО MARKDOWN. ТОЛЬКО {{ ... }}.
1. ВСЕ описательные поля (criticReview, aromaticProfile, foodPairing, tastingNotes, description, tasteProfile, aromaProfile, finish, brandLegend, dishDescription, awards) ОБЯЗАТЕЛЬНО заполняются на {lang_name} языке.
2. НИКОГДА не используй "N/A", "—", "Unknown", "Not available". Если поле неизвестно — ставь пустую строку "".
3. Food pairing должен быть КОНКРЕТНЫМ: не "мясо и сыр", а "grilled rib-eye with truffle sauce, aged Comté, duck confit with orange glaze".
4. Для крепкого алкоголя ВСЕГДА заполняй rawMaterial, distillationMethod, agingInfo, aromaProfile, finish.
5. Для сигар ВСЕГДА заполняй wrapper, binder, filler, strength, size, tastingNotes.
6. Для пива ВСЕГДА заполняй ibu, style, description с деталями вкуса, аромата и текстуры.
7. Для водки: tasteProfile не должен быть пустым — описывай текстуру, mouthfeel, зерновой характер, финиш.
8. Художник этикетки (artist): для Château Mouton Rothschild сверяйся с годом урожая. 2011 = Xu Bing, 2010 = Jeff Koons, 2009 = Bernar Venet, 2008 = Annette Messager, 2007 = Lucian Freud, 2006 = Marlene Dumas, 2005 = Giuseppe Penone. Если видишь этикетку Mouton — определяй художника по году или видимым элементам artwork.
9. Поле "type" определяет категорию продукта — выбирай СТРОГО одно из: wine | beer | spirit | food | cigar

ФОРМАТ JSON ПО ТИПАМ:

Вино (wine):
{{"type":"wine","name":"","producer":"","region":"","country":"","grapeVariety":"","vintage":"","alcoholPercent":"","body":"light|medium|full","tannin":"low|medium|high","acidity":"low|medium|high","sweetness":"dry|off-dry|sweet","parkerRating":"","sucklingRating":"","robinsonRating":"","worldRating":"","criticReview":"","aromaticProfile":"","foodPairing":"","artist":"","holdingGroup":""}}

Пиво (beer):
{{"type":"beer","name":"","brewery":"","style":"","country":"","abv":"","ibu":"","originalGravity":"","finalGravity":"","srm":"","description":"","foodPairing":"","awards":""}}

Крепкий алкоголь (spirit):
{{"type":"spirit","name":"","producer":"","spiritType":"","subtype":"","region":"","country":"","alcoholPercent":"","rawMaterial":"","distillationMethod":"","agingInfo":"","aromaProfile":"","tasteProfile":"","finish":"","criticsAvgScore":"","foodPairing":"","brandLegend":"","sweetnessLevel":"","baseSpirit":"","ricePolishingRatio":"","sakeType":""}}

Еда (food):
{{"type":"food","name":"","producer":"","country":"","ingredients":"","isVegan":false,"calories":"","protein":"","fat":"","carbs":"","healthRating":"","drinkPairing":"","dishDescription":"","cocoaPercent":"","origin":""}}

Сигара/сигарилла (cigar):
{{"type":"cigar","name":"","producer":"","country":"","cigarType":"cigar|cigarillo|cigarello","wrapper":"","binder":"","filler":"","strength":"mild|medium|full","length":"","ringGauge":"","smokingTime":"","tastingNotes":"","pairingDrinks":""}}

ВОЗВРАЩАЙ ТОЛЬКО ОДИН JSON-ОБЪЕКТ. БЕЗ ПРЕДИСЛОВИЙ, БЕЗ ЗАКЛЮЧЕНИЙ."""


def build_user_prompt(lang_name: str) -> str:
    return f"""Внимательно изучи это изображение. Определи, что на нём.

Шаг 1: Определи тип продукта (wine / beer / spirit / food / cigar).
Шаг 2: Прочитай ВЕСЬ текст на этикетке/упаковке — каждое слово имеет значение.
Шаг 3: Используй свои экспертные знания, чтобы заполнить ВСЕ возможные поля.

Критические инструкции:
- Если это ВОДКА: опиши зерновой/картофельный характер, вязкость, согревающий эффект, длину финиша и подходит ли она для шотов или коктейлей.
- Если это ПИВО: оцени IBU по стилю, если не указан на этикетке. Опиши пену, цвет (SRM), хмелевой/солодовый баланс, конкретные блюда для pairing.
- Если это КАЛЬВАДОС или БРЕНДИ: опиши сорта яблок/винограда, тип бочек (лимузенский дуб, экс-бурбон), окислительные ноты, сравнение с аналогичными продуктами AOC.
- Если это СИГАРА/СИГАРИЛЛА: определи происхождение табака (Никарагуа, Куба, Гондурас, Доминикана), цвет покровного листа (claro, colorado, maduro), опиши впечатления от курения.
- Если это ШОКОЛАД: определи происхождение какао (Кот-д'Ивуар, Эквадор, Мадагаскар), метод обработки, процент какао.
- Для ВСЕХ крепких напитков: brandLegend должен содержать год основания и 1-2 исторически важных факта.
- foodPairing должен называть МИНИМУМ 3 конкретных блюда, а не общие категории.

ВСЕ описательные поля заполняй на {lang_name} языке.
Верни ТОЛЬКО JSON. Ничего до, ничего после."""


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

# ── Форматтеры (format_wine, format_beer, format_spirit, format_food, format_cigar) ──
# (Они полностью идентичны тому, что было в предыдущем коде. Я опускаю их здесь для краткости,
#  но они должны остаться в файле bot.py)

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
    # Запускаем health-сервер в отдельном потоке
    Thread(target=start_health_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("post", post_to_channel))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    print("✅ Vinito Bot запущен...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
