"""Публикация каталога в канал: меню с кнопками-категориями и по одному посту на раздел.

Новые прайсы не создают новых сообщений, а редактируют уже опубликованные посты.
Какой пост к какому разделу относится, бот хранит в state.json, а копию — в закреплённом
служебном сообщении в личном чате администратора с ботом. Если файл пропал (например,
хостинг пересобрал контейнер), состояние восстанавливается из этой копии.
Меню «Каталог» после каждого обновления пересоздаётся и всегда остаётся последним в канале.
"""
import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter, TelegramError, TimedOut

from catalog import CATEGORY_ORDER, _iphone_sort_key

log = logging.getLogger("tovarkamh_bot")

STATE_FILE = Path(os.getenv("STATE_FILE") or Path(__file__).with_name("state.json"))
MSK = timezone(timedelta(hours=3))
MENU_TITLE = "📋 <b>Каталог</b>"
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
ABSENT_MARK = " (нет)"
# Telegram пускает в канал около 20 сообщений в минуту: между записями в канал делаем паузу
PACE = float(os.getenv("PACE_SECONDS", "3"))


async def _retry(call, attempts: int = 4):
    """Выполняет запрос; при «Flood control» ждёт, сколько просит Telegram, при таймауте пробует снова."""
    for attempt in range(1, attempts + 1):
        try:
            return await call()
        except RetryAfter as exc:
            if attempt == attempts:
                raise
            ra = exc.retry_after
            wait = ra.total_seconds() if hasattr(ra, "total_seconds") else float(ra)
            log.warning("Flood control: жду %.0f с", wait + 1)
            await asyncio.sleep(wait + 1)
        except TimedOut:
            if attempt == attempts:
                raise
            log.warning("Таймаут, повторяю (%d)", attempt)
            await asyncio.sleep(2)



# ---------- Состояние ----------
def load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("menu_id", None)
            data.setdefault("sections", {})
            return data
    except (OSError, ValueError):
        pass
    return {"menu_id": None, "sections": {}}


def save_state(state: dict) -> None:
    try:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except OSError as exc:
        log.error("Не удалось сохранить состояние: %s", exc)


def reset_state() -> None:
    try:
        STATE_FILE.unlink()
    except OSError:
        pass


def is_empty(state: dict) -> bool:
    return not state.get("menu_id") and not state.get("sections")


# ---------- Ссылки ----------
def link_base(channel: str) -> str:
    channel = channel.strip()
    if channel.startswith("-100"):
        return f"https://t.me/c/{channel[4:]}"
    return f"https://t.me/{channel.lstrip('@')}"


def message_url(channel: str, message_id: int) -> str:
    return f"{link_base(channel)}/{message_id}"


PART_RE = re.compile(r"^(?P<base>.*?)(?: \((?P<n>\d+)/\d+\))?$")


def key_from_title(title: str) -> str:
    m = PART_RE.match(title)
    return f"{m.group('base')}#{m.group('n') or 1}"


BACKUP_HEAD = "🗄 Служебное сообщение бота: список постов каталога. Не удаляйте.\n"


def _backup_text(state: dict):
    data = {
        "m": state.get("menu_id"),
        "s": {k: [v["message_id"], v.get("title", k), v.get("category", "")] for k, v in state["sections"].items()},
    }
    text = BACKUP_HEAD + json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return text if len(text) <= 4000 else None


async def backup_state(bot, chat_id, state: dict) -> None:
    """Копия состояния в закреплённом сообщении личного чата администратора."""
    text = _backup_text(state)
    if text is None:
        log.warning("Состояние слишком большое для копии в чате")
        return
    try:
        if state.get("backup_id"):
            try:
                await bot.edit_message_text(chat_id=chat_id, message_id=state["backup_id"], text=text)
                return
            except BadRequest as exc:
                if "not modified" in str(exc).lower():
                    return
                if not ("not found" in str(exc).lower() or "can't be edited" in str(exc).lower()):
                    raise
        sent = await bot.send_message(chat_id=chat_id, text=text, disable_notification=True)
        state["backup_id"] = sent.message_id
        save_state(state)
        await bot.pin_chat_message(chat_id=chat_id, message_id=sent.message_id, disable_notification=True)
    except TelegramError as exc:
        log.warning("Не удалось сохранить копию состояния в чате %s: %s", chat_id, exc)


async def recover_from_backup(bot, chat_ids) -> dict:
    for chat_id in chat_ids:
        try:
            chat = await bot.get_chat(chat_id)
        except TelegramError:
            continue
        pinned = getattr(chat, "pinned_message", None)
        text = (getattr(pinned, "text", None) or "") if pinned else ""
        if not text.startswith(BACKUP_HEAD):
            continue
        try:
            data = json.loads(text[len(BACKUP_HEAD):])
            sections = {k: {"message_id": v[0], "title": v[1], "category": v[2]} for k, v in data["s"].items()}
        except (ValueError, KeyError, IndexError, TypeError):
            continue
        log.info("Состояние восстановлено из копии в чате %s: разделов %d", chat_id, len(sections))
        return {"menu_id": data.get("m"), "sections": sections, "backup_id": pinned.message_id}
    return {"menu_id": None, "sections": {}}


async def recover_state(bot, channel: str) -> dict:
    """Запасной вариант: восстановление по закреплённому меню канала (старые версии)."""
    state = {"menu_id": None, "sections": {}}
    try:
        chat = await bot.get_chat(channel)
    except TelegramError as exc:
        log.warning("Не удалось прочитать канал для восстановления: %s", exc)
        return state
    pinned = getattr(chat, "pinned_message", None)
    if not pinned or not (pinned.text or "").startswith("📋 Каталог"):
        return state
    markup = getattr(pinned, "reply_markup", None)
    if not markup:
        return state
    state["menu_id"] = pinned.message_id
    for row in markup.inline_keyboard:
        for btn in row:
            m = re.search(r"/(\d+)$", btn.url or "")
            if not m:
                continue
            title = btn.text.removesuffix(ABSENT_MARK)
            category = next((c for c in CATEGORY_ORDER if title.startswith(c)), "")
            state["sections"][key_from_title(title)] = {
                "message_id": int(m.group(1)),
                "title": title,
                "category": category,
            }
    log.info("Состояние восстановлено по закреплённому меню: разделов %d", len(state["sections"]))
    return state


# ---------- План ----------
def make_plan(sections, state: dict) -> dict:
    known = state.get("sections", {})
    new_keys = {s.key for s in sections}
    return {
        "update": [s for s in sections if s.key in known],
        "create": [s for s in sections if s.key not in known],
        "absent": [k for k in known if k not in new_keys],
    }


def _sort_key(entry):
    category, title = entry
    idx = CATEGORY_ORDER.index(category) if category in CATEGORY_ORDER else len(CATEGORY_ORDER)
    sub = _iphone_sort_key(title.split(" (")[0]) if category == "iPhone" else (0, title)
    return idx, sub, title


def stamp() -> str:
    return datetime.now(MSK).strftime("%d.%m %H:%M")


# ---------- Публикация ----------
async def _upsert(bot, channel, message_id, text, markup):
    """Редактирует сообщение; если его нет (удалено) — отправляет новое. Возвращает id."""
    if message_id:
        try:
            await _retry(lambda: bot.edit_message_text(
                chat_id=channel, message_id=message_id, text=text,
                parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW, reply_markup=markup,
            ))
            await asyncio.sleep(PACE)
            return message_id
        except BadRequest as exc:
            msg = str(exc).lower()
            if "not modified" in msg:
                return message_id
            if not ("not found" in msg or "can't be edited" in msg or "message to edit" in msg):
                raise
            log.warning("Сообщение %s не найдено, отправляю новое: %s", message_id, exc)
    sent = await _retry(lambda: bot.send_message(
        chat_id=channel, text=text, parse_mode=ParseMode.HTML,
        link_preview_options=NO_PREVIEW, reply_markup=markup,
    ))
    await asyncio.sleep(PACE)
    return sent.message_id


def _menu_markup(channel: str, state: dict, absent: set) -> InlineKeyboardMarkup:
    entries = sorted(
        ((v.get("category", ""), v.get("title", k), k, v["message_id"]) for k, v in state["sections"].items()),
        key=lambda e: _sort_key((e[0], e[1])),
    )
    buttons = []
    for _, title, key, mid in entries:
        label = title + (ABSENT_MARK if key in absent else "")
        buttons.append(InlineKeyboardButton(label, url=message_url(channel, mid)))
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)


def _post_markup(order_markup, menu_url, with_order=True):
    """Кнопки под постом: «Заказать» (если товар есть) и «К меню»."""
    rows = list(order_markup.inline_keyboard) if with_order else []
    if menu_url:
        rows.append([InlineKeyboardButton("⬅️ К меню", url=menu_url)])
    return InlineKeyboardMarkup(rows) if rows else None


async def publish(bot, channel: str, sections, order_markup, state: dict, backup_chat=None) -> dict:
    """Обновляет посты и меню. Возвращает отчёт: сколько обновлено/создано/нет в наличии.

    Порядок: 1) создаём недостающие посты; 2) отправляем новое меню (оно должно быть последним
    сообщением и знать ссылки на все посты); 3) правим тексты и кнопки «К меню» у постов,
    так как ссылка ведёт на новое меню; 4) удаляем старое меню.
    """
    report = {"updated": 0, "created": 0, "absent": 0, "errors": []}
    known = state["sections"]
    plan = make_plan(sections, state)
    stamp_line = f"\n\n<i>Обновлено {stamp()}</i>"
    absent = set(plan["absent"])
    old_menu = state.get("menu_id")
    menu_url = lambda: message_url(channel, state["menu_id"]) if state.get("menu_id") else None  # noqa: E731

    async def send_menu() -> bool:
        text = f"{MENU_TITLE}\nВыберите категорию 👇\n\n<i>Цены и наличие обновлены {stamp()}</i>"
        markup = _menu_markup(channel, state, absent)
        try:
            sent = await _retry(lambda: bot.send_message(
                chat_id=channel, text=text, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW,
                reply_markup=markup, disable_notification=True,
            ))
        except TelegramError as exc:
            report["errors"].append(f"меню: {exc}")
            return False
        prev = state.get("menu_id")
        state["menu_id"] = sent.message_id
        save_state(state)
        if prev:  # старое меню убираем, чтобы новое всегда было в самом низу
            try:
                await _retry(lambda: bot.delete_message(chat_id=channel, message_id=prev))
            except TelegramError as exc:
                log.warning("Не удалось удалить старое меню %s: %s", prev, exc)
                report["errors"].append("старое меню не удалилось (удалите его вручную)")
        return True

    # 1. новые посты
    for sec in sections:
        if sec.key in known:
            continue
        try:
            mid = await _upsert(bot, channel, None, sec.text + stamp_line, _post_markup(order_markup, menu_url()))
        except TelegramError as exc:
            report["errors"].append(f"{sec.title}: {exc}")
            continue
        known[sec.key] = {"message_id": mid, "title": sec.title, "category": sec.category}
        report["created"] += 1
        save_state(state)

    # 2. новое меню
    menu_ok = await send_menu()

    # 3. тексты и кнопки «К меню» у всех постов
    recreated = []
    for sec in sections:
        if sec.key not in known:
            continue  # пост не создался: меню о нём не знает
        entry = known[sec.key]
        try:
            mid = await _upsert(bot, channel, entry["message_id"], sec.text + stamp_line,
                                _post_markup(order_markup, menu_url()))
        except TelegramError as exc:
            report["errors"].append(f"{sec.title}: {exc}")
            continue
        if mid != entry["message_id"]:
            entry["message_id"] = mid
            recreated.append(sec.key)
            report["created"] += 1
        elif sec.key in {s.key for s in plan["update"]}:
            report["updated"] += 1
        save_state(state)

    for key in plan["absent"]:
        entry = known[key]
        text = f"<b>{entry['title']}</b>\n\nНет в наличии.{stamp_line}"
        try:
            await _upsert(bot, channel, entry["message_id"], text, _post_markup(order_markup, menu_url(), False))
            report["absent"] += 1
        except TelegramError as exc:
            report["errors"].append(f"{entry['title']}: {exc}")

    # пост, удалённый вручную, пересоздан уже после меню: обновим меню и его кнопку
    if recreated and menu_ok:
        save_state(state)
        if await send_menu():
            for key in recreated:
                sec = next(x for x in sections if x.key == key)
                try:
                    await _upsert(bot, channel, known[key]["message_id"], sec.text + stamp_line,
                                  _post_markup(order_markup, menu_url()))
                except TelegramError as exc:
                    report["errors"].append(f"{sec.title}: {exc}")

    if backup_chat:
        await backup_state(bot, backup_chat, state)
    return report
