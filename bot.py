"""Telegram-бот для канала @tovarkamh.

1. Вы отправляете боту полный прайс поставщика -> бот собирает разделы (правила в catalog.py),
   показывает, что будет обновлено, и после «Опубликовать» редактирует посты в канале
   (меню «Каталог» с кнопками-категориями + по посту на раздел; новые сообщения не плодятся).
2. Под каждым постом кнопка «Заказать»: клиент нажимает её прямо в канале, бот сразу пишет
   всем менеджерам. Первый, кто нажал «Беру», забирает заказ, остальные получают уведомление.
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

import channel as channel_mod
from catalog import build_sections

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

_take_lock = asyncio.Lock()
_publish_lock = asyncio.Lock()  # чтобы два менеджера не забрали один заказ одновременно


def order_keyboard(bot_username: str = "") -> InlineKeyboardMarkup:
    """Одна кнопка под постом. Нажатие обрабатывается прямо в канале, клиент никуда не переходит."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("Заказать", callback_data="order")]])


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

    text = f"В каталог попало товаров: {res.kept}"
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
            "/id — показать ваш Telegram ID\n"
            "/reset — забыть посты каталога в канале (создать заново)"
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


async def create_order(context: ContextTypes.DEFAULT_TYPE, user) -> str:
    """Создаёт заказ и пишет менеджерам. Возвращает: ok | repeat | fail | unavailable."""
    if not MANAGER_IDS:
        log.error("Не заданы MANAGER_IDS/ADMIN_IDS: некому отправлять заказы")
        return "unavailable"

    last_at: dict = context.bot_data.setdefault("last_order_at", {})
    now = time.time()
    if now - last_at.get(user.id, 0) < ORDER_COOLDOWN:
        return "repeat"
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
        return "fail"

    orders: dict = context.bot_data.setdefault("orders", {})
    while len(orders) >= MAX_ORDERS:
        orders.pop(next(iter(orders)))
    orders[order_id] = order

    for manager_id in failed:  # предупредим тех, кому доставилось
        for reachable in order["messages"]:
            try:
                await context.bot.send_message(
                    chat_id=reachable,
                    text=f"⚠️ Менеджеру с ID {manager_id} уведомление не доставлено: он ещё не нажал «Старт» в боте.",
                )
            except TelegramError:
                pass
    return "ok"


REPLIES = {
    "ok": CLIENT_REPLY,
    "repeat": CLIENT_REPLY_REPEAT,
    "fail": "Не получилось передать заявку менеджеру. Попробуйте позже.",
    "unavailable": "Заказы временно недоступны. Попробуйте позже.",
}


async def handle_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Старые посты: клиент перешёл в бота по ссылке и нажал «Старт»."""
    status = await create_order(context, update.effective_user)
    await update.message.reply_text(REPLIES[status])


async def on_order_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Клиент нажал «Заказать» под постом в канале: ответ всплывает прямо там."""
    query = update.callback_query
    status = await create_order(context, update.effective_user)
    await safe_answer(query, REPLIES[status], show_alert=True)


async def on_take(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Менеджер нажал «Беру»."""
    query, user = update.callback_query, update.effective_user
    if user.id not in MANAGER_IDS:
        await safe_answer(query, "Нет доступа", show_alert=True)
        return

    order_id = (query.data or "").partition(":")[2]
    orders: dict = context.bot_data.setdefault("orders", {})

    async with _take_lock:  # внутри блока заказ может забрать только один человек
        order = orders.get(order_id)
        if order is None:
            await safe_answer(query, 
                "Заказ устарел: бот перезапускался. Решите между собой, кто пишет клиенту.", show_alert=True
            )
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except TelegramError:
                pass
            return
        if order["taken_by"] is not None:
            taker = order["taken_by_name"]
            await safe_answer(query, f"Заказ уже взял {taker}", show_alert=True)
            try:
                await query.edit_message_text(
                    order_text(order, f"✅ Заказ взял: {html.escape(taker)}"), parse_mode=ParseMode.HTML
                )
            except TelegramError:
                pass
            return
        order["taken_by"] = user.id
        order["taken_by_name"] = person_name(user)

    await safe_answer(query, "Вы взяли заказ")
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
async def safe_answer(query, text=None, **kw) -> None:
    """Ответ на нажатие кнопки; если нажатие слишком старое, Telegram откажет — это не страшно."""
    try:
        await query.answer(text, **kw)
    except TelegramError as exc:
        log.warning("Не удалось ответить на нажатие кнопки: %s", exc)


async def get_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    """Состояние каталога: из памяти, из файла или (если файл пропал) из закреплённого меню."""
    state = context.bot_data.get("state")
    if state is None:
        state = channel_mod.load_state()
        if channel_mod.is_empty(state):
            state = await channel_mod.recover_from_backup(context.bot, sorted(ADMIN_IDS))
            if channel_mod.is_empty(state):
                state = await channel_mod.recover_state(context.bot, CHANNEL)
            if not channel_mod.is_empty(state):
                channel_mod.save_state(state)
        context.bot_data["state"] = state
    return state


def plan_text(sections, plan, state) -> str:
    def line(sec):
        return f"  • {sec.title} — {len(sec.items)} шт."

    text = f"Разделов в этом сообщении: {len(sections)}"
    if plan["update"]:
        text += f"\n\n🔄 Обновлю посты ({len(plan['update'])}):\n" + "\n".join(line(x) for x in plan["update"])
    if plan["create"]:
        text += f"\n\n🆕 Создам новые посты ({len(plan['create'])}):\n" + "\n".join(line(x) for x in plan["create"])
    others = [state["sections"][k].get("title", k) for k in plan["untouched"] if not state["sections"][k].get("absent")]
    if others:
        text += (
            f"\n\nОстальные разделы ({len(others)}) не изменятся: " + ", ".join(others)
            + ".\nЕсли это полный прайс, нажмите «Полный прайс»: они получат «Нет в наличии»."
        )
    return text


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

    sections, res = build_sections(text)
    if not sections:
        await msg.reply_text("Подходящих товаров не нашлось." + make_report(res))
        return

    state = await get_state(context)
    plan = channel_mod.make_plan(sections, state, mark_absent=False)

    pending: dict = context.bot_data.setdefault("pending", {})
    while len(pending) >= MAX_PENDING:
        pending.pop(next(iter(pending)))
    token = secrets.token_hex(4)
    pending[token] = sections

    first = (
        "\n\nℹ️ Каталога в канале ещё нет: бот создаст посты и меню «Каталог» (оно всегда будет последним сообщением)."
        if not state.get("menu_id") else ""
    )
    await msg.reply_text(
        plan_text(sections, plan, state) + first + "\n\n" + make_report(res)
        + f"\n\nОбновить каталог в {CHANNEL}?",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ Опубликовать эти разделы", callback_data=f"pub:{token}")],
                [InlineKeyboardButton("📋 Полный прайс (остальных нет)", callback_data=f"full:{token}")],
                [InlineKeyboardButton("❌ Отмена", callback_data=f"cancel:{token}")],
            ]
        ),
    )


async def on_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await safe_answer(query)
    if not is_admin(update):
        return

    action, _, token = (query.data or "").partition(":")
    pending: dict = context.bot_data.setdefault("pending", {})
    sections = pending.get(token)
    if sections is None:
        await query.edit_message_text("Этот прайс уже обработан, или бот перезапускался. Пришлите прайс заново.")
        return

    if action == "cancel":
        pending.pop(token, None)
        await query.edit_message_text("Отменено. Ничего не изменено.")
        return

    await query.edit_message_text("⏳ Обновляю каталог в канале… Это займёт минуту: Telegram ограничивает скорость публикации.")
    async with _publish_lock:
        state = await get_state(context)
        try:
            rep = await channel_mod.publish(
                context.bot, CHANNEL, sections, order_keyboard(context.bot.username), state,
                backup_chat=update.effective_user.id, mark_absent=(action == "full"),
            )
        except TelegramError as exc:
            log.exception("Не удалось обновить каталог")
            await query.edit_message_text(
                f"❌ Не получилось обновить каталог: {exc}\n"
                f"Проверьте, что бот администратор {CHANNEL} с правом публиковать и редактировать сообщения."
            )
            return

    text = (
        f"✅ Каталог в {CHANNEL} обновлён.\n"
        f"Обновлено постов: {rep['updated']}, создано: {rep['created']}, «нет в наличии»: {rep['absent']}."
    )
    retry_markup = None
    if rep["errors"]:
        shown = "\n".join(f"  • {e}" for e in rep["errors"][:8])
        text += (
            f"\n\n⚠️ Не удалось ({len(rep['errors'])}):\n{shown}\n"
            "Нажмите «Повторить»: бот доделает только недостающее."
        )
        retry_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔁 Повторить", callback_data=f"{action}:{token}")]])
    else:
        pending.pop(token, None)
    await query.edit_message_text(text, reply_markup=retry_markup)


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Забыть созданные посты: следующая публикация создаст каталог с нуля."""
    if not is_admin(update):
        return
    channel_mod.reset_state()
    context.bot_data["state"] = {"menu_id": None, "sections": {}}
    await update.message.reply_text(
        "Бот забыл свои посты в канале. Удалите старые сообщения в канале вручную, "
        "затем пришлите прайс: каталог будет создан заново."
    )


def build_app() -> Application:
    app = (
        Application.builder().token(BOT_TOKEN)
        .connect_timeout(15).read_timeout(30).write_timeout(30).pool_timeout(15)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CallbackQueryHandler(on_order_button, pattern=r"^order$"))
    app.add_handler(CallbackQueryHandler(on_take, pattern=r"^take:"))
    app.add_handler(CallbackQueryHandler(on_decision, pattern=r"^(pub|full|cancel):"))
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
