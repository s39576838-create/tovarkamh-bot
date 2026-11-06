"""Telegram-бот для канала @tovarkamh.

1. Вы отправляете боту пост поставщика -> бот собирает готовый пост (правила в catalog.py),
   показывает предпросмотр и после нажатия «Опубликовать» выкладывает его в канал.
2. Под постом одна кнопка «Заказать». Клиент нажимает её, открывается чат с ботом, и бот
   сразу пишет всем менеджерам. Первый, кто нажал «Беру», забирает заказ, остальные получают уведомление.
"""

import asyncio
import html
import logging
import os
import secrets
import time
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
# httpx пишет в лог полный адрес запроса вместе с токеном бота — глушим
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("tovarkamh_bot")


def _parse_ids(name: str) -> set:
    return {int(x) for x in os.getenv(name, "").replace(" ", "").split(",") if x.isdigit()}


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL = os.getenv("CHANNEL", "@tovarkamh").strip()
ADMIN_IDS = _parse_ids("ADMIN_IDS")  # кто может публиковать посты
# Кому приходят заказы. Если MANAGER_IDS не задан, заказы получают все из ADMIN_IDS.
MANAGER_IDS = _parse_ids("MANAGER_IDS") or ADMIN_IDS

CLIENT_REPLY = "Спасибо! Менеджер скоро свяжется с вами."
CLIENT_REPLY_REPEAT = "Ваша заявка уже отправлена. Менеджер скоро свяжется с вами."
ORDER_COOLDOWN = 120  # секунд: повторное нажатие «Заказать» за это время не создаёт новый заказ

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
MAX_PENDING = 20   # сколько подготовленных постов держим в памяти
MAX_ORDERS = 300   # сколько последних заказов помним для кнопки «Беру»

_take_lock = asyncio.Lock()  # чтобы два менеджера не забрали один заказ одновременно


def order_keyboard(bot_username: str) -> InlineKeyboardMarkup:
    """Одна кнопка «Заказать» под постом: открывает чат с ботом."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Заказать", url=f"https://t.me/{bot_username}?start=order")]]
    )


def is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in ADMIN_IDS)


def person_name(user) -> str:
    name = user.full_name or user.username or str(user.id)
    return f"{name} (@{user.username})" if user.username and user.full_name else name


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


# ─────────────────────────── /start, /id ───────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if args and args[0] == "order":
        await handle_order(update, context)
        return

    user_id = update.effective_user.id
    if is_admin(update):
        text = (
            "Привет! Перешлите мне пост поставщика (или вставьте его текст).\n"
            "Я отфильтрую товары, посчитаю цены и покажу готовый пост. "
            f"После вашего подтверждения опубликую его в {CHANNEL}.\n\n"
            "/id — показать ваш Telegram ID"
        )
        if user_id in MANAGER_IDS:
            text += "\n\nВы также получаете сюда заказы от клиентов."
    elif user_id in MANAGER_IDS:
        text = "Вы подключены как менеджер. Заказы от клиентов будут приходить сюда."
    else:
        text = f"Здравствуйте! Чтобы сделать заказ, нажмите кнопку «Заказать» под постом в канале {CHANNEL}."
    await update.message.reply_text(text)


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"Ваш Telegram ID: {update.effective_user.id}")


# ─────────────────────────── заказы ───────────────────────────
def order_text(order: dict, status: str = "") -> str:
    who = f'<a href="tg://user?id={order["client_id"]}">{html.escape(order["name"])}</a>'
    if order["username"]:
        who += f' (@{html.escape(order["username"])})'
    return "\n".join(
        ["🛒 <b>Новый заказ</b>", f"Клиент: {who}", status or "Нажмите «Беру», чтобы забрать заказ."]
    )


async def handle_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Клиент нажал «Заказать» в канале и «Старт» в боте."""
    msg, user = update.message, update.effective_user
    if not MANAGER_IDS:
        log.error("Не заданы MANAGER_IDS/ADMIN_IDS: некому отправлять заказы")
        await msg.reply_text("Заказы временно недоступны. Попробуйте позже.")
        return

    last_at: dict = context.bot_data.setdefault("last_order_at", {})
    now = time.time()
    if now - last_at.get(user.id, 0) < ORDER_COOLDOWN:
        await msg.reply_text(CLIENT_REPLY_REPEAT)
        return
    last_at[user.id] = now

    order_id = secrets.token_hex(4)
    order = {
        "client_id": user.id,
        "name": user.full_name or user.username or str(user.id),
        "username": user.username,
        "taken_by": None,
        "taken_by_name": "",
        "messages": {},  # id менеджера -> id сообщения с уведомлением
    }

    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✋ Беру", callback_data=f"take:{order_id}")]])
    failed = []
    for manager_id in sorted(MANAGER_IDS):
        try:
            sent = await context.bot.send_message(
                chat_id=manager_id, text=order_text(order), parse_mode=ParseMode.HTML, reply_markup=keyboard
            )
            order["messages"][manager_id] = sent.message_id
        except TelegramError as exc:
            log.warning("Не удалось отправить заказ менеджеру %s: %s", manager_id, exc)
            failed.append(manager_id)

    if not order["messages"]:
        last_at.pop(user.id, None)
        await msg.reply_text("Не получилось передать заявку менеджеру. Попробуйте позже.")
        return

    orders: dict = context.bot_data.setdefault("orders", {})
    while len(orders) >= MAX_ORDERS:
        orders.pop(next(iter(orders)))
    orders[order_id] = order

    await msg.reply_text(CLIENT_REPLY)

    for manager_id in failed:  # предупредим тех, кому доставилось
        for reachable in order["messages"]:
            try:
                await context.bot.send_message(
                    chat_id=reachable,
                    text=f"⚠️ Менеджеру с ID {manager_id} уведомление не доставлено: он ещё не нажал «Старт» в боте.",
                )
            except TelegramError:
                pass


async def on_take(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Менеджер нажал «Беру»."""
    query, user = update.callback_query, update.effective_user
    if user.id not in MANAGER_IDS:
        await query.answer("Нет доступа", show_alert=True)
        return

    order_id = (query.data or "").partition(":")[2]
    orders: dict = context.bot_data.setdefault("orders", {})

    async with _take_lock:  # внутри блока заказ может забрать только один человек
        order = orders.get(order_id)
        if order is None:
            await query.answer(
                "Заказ устарел: бот перезапускался. Решите между собой, кто пишет клиенту.", show_alert=True
            )
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except TelegramError:
                pass
            return
        if order["taken_by"] is not None:
            taker = order["taken_by_name"]
            await query.answer(f"Заказ уже взял {taker}", show_alert=True)
            try:
                await query.edit_message_text(
                    order_text(order, f"✅ Заказ взял: {html.escape(taker)}"), parse_mode=ParseMode.HTML
                )
            except TelegramError:
                pass
            return
        order["taken_by"] = user.id
        order["taken_by_name"] = person_name(user)

    await query.answer("Вы взяли заказ")
    taker = html.escape(order["taken_by_name"])
    for manager_id, message_id in order["messages"].items():
        status = "✅ <b>Вы взяли этот заказ.</b> Напишите клиенту." if manager_id == user.id else f"✅ Заказ взял: {taker}"
        try:
            await context.bot.edit_message_text(
                chat_id=manager_id, message_id=message_id, text=order_text(order, status), parse_mode=ParseMode.HTML
            )
        except TelegramError as exc:
            log.warning("Не удалось обновить уведомление у %s: %s", manager_id, exc)


# ─────────────────────────── публикация прайса ───────────────────────────
async def on_price_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not ADMIN_IDS:
        await msg.reply_text(
            "В настройках пока не указан ADMIN_IDS. Отправьте /id, впишите число в настройки "
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
            reply_markup=order_keyboard(context.bot.username) if last else None,
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
                reply_markup=order_keyboard(context.bot.username) if last else None,
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
    app.add_handler(CallbackQueryHandler(on_take, pattern=r"^take:"))
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
        raise SystemExit("Не задан BOT_TOKEN. Впишите токен от @BotFather в настройки (config.env или переменные окружения).")
    log.info(
        "Бот запущен. Канал: %s, администраторов: %d, менеджеров: %d",
        CHANNEL, len(ADMIN_IDS), len(MANAGER_IDS),
    )
    build_app().run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
