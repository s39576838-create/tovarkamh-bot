"""Telegram-бот для канала @tovarkamh.

1. Вы отправляете боту полный прайс поставщика -> бот собирает разделы (правила в catalog.py),
   показывает, что будет обновлено, и после «Опубликовать» редактирует посты в канале
   (меню «Каталог» с кнопками-категориями + по посту на раздел; новые сообщения не плодятся).
2. Под каждым постом кнопка «Заказать»: клиент нажимает её прямо в канале, бот сразу пишет
   всем менеджерам. Первый, кто нажал «Беру», забирает заказ, остальные получают уведомление.
"""

import asyncio
import html
import re
import logging
import os
import secrets
import time
from pathlib import Path
from types import SimpleNamespace

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
import showcase
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
            "/notify — вручную уведомить подписчиков об обновлении цен\n"
            "/off — отметить раздел «Нет в наличии»\n"
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
    lines = ["🛒 <b>Новый заказ</b>", f"Клиент: {who}"]
    if order.get("item"):
        lines.append(f"Товар: <b>{html.escape(order['item'])}</b>")
    if order.get("section"):
        lines.append(f"Раздел: <b>{html.escape(order['section'])}</b>")
    lines.append(status or "Нажмите «Беру», чтобы забрать заказ.")
    return "\n".join(lines)


async def create_order(context: ContextTypes.DEFAULT_TYPE, user, section_key=None, item=None) -> str:
    """Создаёт заказ и пишет менеджерам. Возвращает: ok | repeat | fail | unavailable.

    item — название товара из карточки раздела «Разное»: повтор считается отдельно по каждому товару."""
    if not MANAGER_IDS:
        log.error("Не заданы MANAGER_IDS/ADMIN_IDS: некому отправлять заказы")
        return "unavailable"

    last_at: dict = context.bot_data.setdefault("last_order_at", {})
    now = time.time()
    cool_key = (user.id, item) if item else user.id  # как раньше: один клиент — одна заявка в 2 минуты; карточки считаются по товару
    if now - last_at.get(cool_key, 0) < ORDER_COOLDOWN:
        return "repeat"
    last_at[cool_key] = now

    order_id = secrets.token_hex(4)
    order = {
        "client_id": user.id,
        "name": user.full_name or user.username or str(user.id),
        "username": user.username,
        "section": None,
        "item": item,
        "taken_by": None,
        "taken_by_name": "",
        "messages": {},  # id менеджера -> id сообщения с уведомлением
    }

    if section_key:
        state = await get_state(context)
        order["section"] = state["sections"].get(section_key, {}).get("title")

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
        last_at.pop(cool_key, None)
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


def card_label(message) -> str:
    """Название и цена товара из подписи карточки в канале («Отвёртка — 500₽»)."""
    text = (getattr(message, "caption", None) or getattr(message, "text", None) or "").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return " — ".join(lines[:2])


async def on_order_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Клиент нажал «Заказать» под постом в канале: ответ всплывает прямо там."""
    query = update.callback_query
    _, _, section_key = (query.data or "").partition(":")
    if section_key == "c":  # карточка товара из раздела «Разное»: название берём из её подписи
        item = card_label(query.message) or "товар из раздела «Разное»"
        status = await create_order(context, update.effective_user, item=item)
    else:
        status = await create_order(context, update.effective_user, section_key or None)
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
            + ".\nЕсли какой-то раздел закончился, уберите его командой /off."
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
        text += "\n\nПосты опубликованы без звука. Подписчикам можно отправить уведомление вручную."
        retry_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔔 Уведомить подписчиков", callback_data="notify")]])
    await query.edit_message_text(text, reply_markup=retry_markup)


DEFAULT_NOTICE = "🔔 <b>Обновили цены и наличие</b>\nАктуальный каталог — в меню ниже 👇"


async def cmd_notify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/notify [текст] — вручную отправить подписчикам уведомление об обновлении (со звуком)."""
    if not is_admin(update):
        return
    custom = " ".join(context.args or []).strip()
    context.bot_data["notice_text"] = html.escape(custom) if custom else DEFAULT_NOTICE
    await update.message.reply_text(
        "Отправить подписчикам канала уведомление (со звуком)?\n\n" + re.sub(r"</?b>", "", context.bot_data["notice_text"]),
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Да, отправить", callback_data="notify_yes"),
            InlineKeyboardButton("Нет", callback_data="notify_no"),
        ]]),
    )


async def on_notify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await safe_answer(query)
    if not is_admin(update):
        return
    if query.data == "notify":  # кнопка под отчётом о публикации
        context.bot_data["notice_text"] = DEFAULT_NOTICE
        await query.edit_message_text(
            "Отправить подписчикам канала уведомление (со звуком)?\n\n" + re.sub(r"</?b>", "", DEFAULT_NOTICE),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Да, отправить", callback_data="notify_yes"),
                InlineKeyboardButton("Нет", callback_data="notify_no"),
            ]]),
        )
        return
    if query.data == "notify_no":
        await query.edit_message_text("Уведомление не отправлено.")
        return
    await query.edit_message_text("⏳ Отправляю уведомление…")
    async with _publish_lock:
        state = await get_state(context)
        try:
            await channel_mod.notify_update(
                context.bot, CHANNEL, state, context.bot_data.get("notice_text", DEFAULT_NOTICE),
                backup_chat=update.effective_user.id,
            )
        except TelegramError as exc:
            await query.edit_message_text(f"❌ Не получилось отправить уведомление: {exc}")
            return
    await query.edit_message_text("✅ Уведомление отправлено, меню снова внизу канала.")


async def cmd_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/off — показать разделы кнопками; выбранный получит «Нет в наличии»."""
    if not is_admin(update):
        return
    state = await get_state(context)
    rows = [
        [InlineKeyboardButton(v.get("title", k), callback_data=f"off:{k}")]
        for k, v in state["sections"].items()
        if not v.get("absent") and len(f"off:{k}".encode("utf-8")) <= 64
    ]
    if not rows:
        await update.message.reply_text("Сейчас нет разделов в наличии.")
        return
    rows.append([InlineKeyboardButton("Отмена", callback_data="off_cancel")])
    await update.message.reply_text(
        "Какой раздел закончился? Он получит «Нет в наличии». Вернётся в наличие, когда вы пришлёте его прайс снова.",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def on_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await safe_answer(query)
    if not is_admin(update):
        return
    if query.data == "off_cancel":
        await query.edit_message_text("Отменено.")
        return
    key = query.data.split(":", 1)[1]
    await query.edit_message_text("⏳ Обновляю канал…")
    async with _publish_lock:
        state = await get_state(context)
        if key not in state["sections"]:
            await query.edit_message_text("Такого раздела уже нет.")
            return
        title = state["sections"][key].get("title", key)
        try:
            rep = await channel_mod.publish(
                context.bot, CHANNEL, [], None, state,
                backup_chat=update.effective_user.id, absent_keys=[key],
            )
        except TelegramError as exc:
            await query.edit_message_text(f"❌ Не получилось: {exc}")
            return
    extra = f"\n⚠️ {'; '.join(rep['errors'][:3])}" if rep["errors"] else ""
    await query.edit_message_text(f"✅ «{title}» теперь «Нет в наличии».{extra}")


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


# ─────────────────────────── раздел «Разное» (товары из учёта) ───────────────────────────
async def showcase_tick(ctx) -> None:
    """Один опрос учёта: обновляет кнопку раздела в меню и возвращает меню в самый низ канала."""
    info = await showcase.fetch_info()
    if info is None:
        log.warning("Раздел «Разное»: учёт не принял SYNC_KEY")
        return
    state = await get_state(ctx)
    extra, changed, refresh = showcase.decide(info, state)
    if not state.get("menu_id"):  # каталога в канале ещё нет: кнопка раздела появится при первой публикации
        if changed:
            state["extra"] = extra
            channel_mod.save_state(state)
        return
    if not refresh:
        return

    async with _publish_lock:
        state = await get_state(ctx)
        before_menu, before_extra = state.get("menu_id"), state.get("extra")
        state["extra"] = extra
        backup_chat = min(ADMIN_IDS) if ADMIN_IDS else None
        try:
            await channel_mod.publish(ctx.bot, CHANNEL, [], None, state, backup_chat=backup_chat)
        except Exception:
            state["extra"] = before_extra  # меню не обновилось: при следующем опросе попробуем снова
            raise
        if state.get("menu_id") == before_menu:  # публикация не упала, но новое меню не отправилось
            state["extra"] = before_extra
            ctx.bot_data["showcase_retry_at"] = time.time() + showcase.RETRY_AFTER_FAIL
            log.warning("Раздел «Разное»: меню отправить не удалось, повторю позже")
            return
        channel_mod.save_state(state)
    log.info("Раздел «Разное»: меню обновлено")


async def showcase_loop(app: Application) -> None:
    ctx = SimpleNamespace(bot_data=app.bot_data, bot=app.bot)  # get_state нужны только эти два поля
    while True:
        await asyncio.sleep(showcase.SYNC_EVERY)
        if time.time() < app.bot_data.get("showcase_retry_at", 0):
            continue
        try:
            await showcase_tick(ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # адрес с ключом в журнал не пишем, только тип ошибки
            log.warning("Раздел «Разное»: сбой синхронизации (%s)", type(exc).__name__)
            app.bot_data["showcase_retry_at"] = time.time() + showcase.RETRY_AFTER_FAIL


async def on_startup(app: Application) -> None:
    if showcase.enabled():
        app.bot_data["showcase_task"] = asyncio.create_task(showcase_loop(app))
        log.info("Раздел «Разное»: слежу за учётом каждые %d с", showcase.SYNC_EVERY)
    else:
        log.info("Раздел «Разное» выключен: не заданы SYNC_URL и SYNC_KEY")


def build_app() -> Application:
    app = (
        Application.builder().token(BOT_TOKEN)
        .connect_timeout(15).read_timeout(30).write_timeout(30).pool_timeout(15)
        .post_init(on_startup)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("notify", cmd_notify))
    app.add_handler(CommandHandler("off", cmd_off))
    app.add_handler(CallbackQueryHandler(on_notify, pattern=r"^notify"))
    app.add_handler(CallbackQueryHandler(on_off, pattern=r"^off"))
    app.add_handler(CallbackQueryHandler(on_order_button, pattern=r"^order(:|$)"))
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
