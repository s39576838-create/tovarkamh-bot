"""Telegram-бот для канала @tovarkamh.

Вы отправляете боту пост поставщика -> бот собирает готовый пост (правила в catalog.py),
показывает предпросмотр и после нажатия «Опубликовать» выкладывает его в канал с кнопками «Заказать».
"""

import logging
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from catalog import build_post, split_for_telegram

load_dotenv(Path(__file__).with_name("config.env"))
logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("tovarkamh_bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL = os.getenv("CHANNEL", "@tovarkamh").strip()
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}

# Кнопки под постом в канале: (текст, ссылка). Одна кнопка не может вести в два чата,
# поэтому их две — по одной на каждый чат.
ORDER_BUTTONS = [
    ("Заказать · @maloysy", "https://t.me/maloysy"),
    ("Заказать · @lelik4501", "https://t.me/lelik4501"),
]

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
MAX_PENDING = 20  # сколько подготовленных постов держим в памяти


def order_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(text, url=url) for text, url in ORDER_BUTTONS]])


def is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in ADMIN_IDS)


def make_report(res, limit: int = 12) -> str:
    """Короткий отчёт для администратора (в канал не попадает)."""

    def section(title, rows):
        if not rows:
            return ""
        shown = "\n".join(f"  • {r}" for r in rows[:limit])
        more = f"\n  … и ещё {len(rows) - limit}" if len(rows) > limit else ""
        return f"\n\n{title} ({len(rows)}):\n{shown}{more}"

    text = f"В пост попало товаров: {res.kept}"
    text += section("Исключено по правилам", [f"{line}  [{why}]" for line, why in res.excluded])
    text += section("Не прочитана цена", res.unparsed)
    text += section("Бренд не из вашего списка", res.unmatched)
    return text


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Перешлите мне пост поставщика (или вставьте его текст).\n"
        "Я отфильтрую товары, посчитаю цены и покажу готовый пост. "
        f"После вашего подтверждения опубликую его в {CHANNEL}.\n\n"
        "/id — показать ваш Telegram ID"
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"Ваш Telegram ID: {update.effective_user.id}")


async def on_price_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not ADMIN_IDS:
        await msg.reply_text(
            "В config.env пока не указан ADMIN_IDS. Отправьте /id, впишите число в config.env "
            "и перезапустите бота."
        )
        return
    if not is_admin(update):
        await msg.reply_text("Этот бот работает только для администратора.")
        return

    text = msg.text or msg.caption or ""
    if "▫" not in text:
        await msg.reply_text("Не вижу строк с маркером ▫️. Пришлите пост поставщика целиком.")
        return

    res = build_post(text)
    chunks = split_for_telegram(res.blocks)
    if not chunks:
        await msg.reply_text("Подходящих товаров не нашлось." + make_report(res))
        return

    # Предпросмотр: ровно то, что уйдёт в канал.
    await msg.reply_text("👇 Так будет выглядеть пост:")
    for i, chunk in enumerate(chunks):
        last = i == len(chunks) - 1
        await msg.reply_text(
            chunk,
            parse_mode=ParseMode.HTML,
            link_preview_options=NO_PREVIEW,
            reply_markup=order_keyboard() if last else None,
        )

    pending: dict = context.bot_data.setdefault("pending", {})
    while len(pending) >= MAX_PENDING:
        pending.pop(next(iter(pending)))
    token = secrets.token_hex(4)
    pending[token] = chunks

    await msg.reply_text(
        make_report(res) + f"\n\nОпубликовать в {CHANNEL}?",
        reply_markup=InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Опубликовать", callback_data=f"pub:{token}"),
                InlineKeyboardButton("❌ Отмена", callback_data=f"cancel:{token}"),
            ]]
        ),
    )


async def on_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin(update):
        return

    action, _, token = (query.data or "").partition(":")
    pending: dict = context.bot_data.setdefault("pending", {})
    chunks = pending.get(token)
    if chunks is None:
        await query.edit_message_text("Этот пост уже обработан, или бот перезапускался. Пришлите прайс заново.")
        return

    if action == "cancel":
        pending.pop(token, None)
        await query.edit_message_text("Отменено. Ничего не опубликовано.")
        return

    sent = 0
    try:
        for i, chunk in enumerate(chunks):
            last = i == len(chunks) - 1
            await context.bot.send_message(
                chat_id=CHANNEL,
                text=chunk,
                parse_mode=ParseMode.HTML,
                link_preview_options=NO_PREVIEW,
                reply_markup=order_keyboard() if last else None,
            )
            sent += 1
    except TelegramError as exc:
        log.exception("Не удалось опубликовать")
        extra = f" Уже отправлено частей: {sent} из {len(chunks)}." if sent else ""
        await query.edit_message_text(
            f"❌ Не получилось опубликовать: {exc}.{extra}\n"
            f"Проверьте, что бот добавлен администратором в {CHANNEL} с правом публиковать сообщения."
        )
        return

    pending.pop(token, None)
    await query.edit_message_text(f"✅ Опубликовано в {CHANNEL} (сообщений: {sent}).")


def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CallbackQueryHandler(on_decision, pattern=r"^(pub|cancel):"))
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & (filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
            on_price_list,
        )
    )
    return app


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN. Впишите токен от @BotFather в файл config.env.")
    log.info("Бот запущен. Канал: %s, администраторов: %d", CHANNEL, len(ADMIN_IDS))
    build_app().run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
