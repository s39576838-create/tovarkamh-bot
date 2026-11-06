"""Публикация каталога в канал: меню с кнопками-категориями и по одному посту на раздел.

Новые прайсы не создают новых сообщений, а редактируют уже опубликованные посты.
Какой пост к какому разделу относится, бот хранит в state.json. Если файл пропал
(например, хостинг пересобрал контейнер), состояние восстанавливается по закреплённому
меню: в его кнопках лежат ссылки на посты.
"""
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError

from catalog import CATEGORY_ORDER, _iphone_sort_key

log = logging.getLogger("tovarkamh_bot")

STATE_FILE = Path(os.getenv("STATE_FILE") or Path(__file__).with_name("state.json"))
MSK = timezone(timedelta(hours=3))
MENU_TITLE = "📋 <b>Каталог</b>"
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
ABSENT_MARK = " (нет)"


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


async def recover_state(bot, channel: str) -> dict:
    """Восстанавливает состояние по закреплённому меню канала."""
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
            await bot.edit_message_text(
                chat_id=channel, message_id=message_id, text=text,
                parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW, reply_markup=markup,
            )
            return message_id
        except BadRequest as exc:
            msg = str(exc).lower()
            if "not modified" in msg:
                return message_id
            if not ("not found" in msg or "can't be edited" in msg or "message to edit" in msg):
                raise
            log.warning("Сообщение %s не найдено, отправляю новое: %s", message_id, exc)
    sent = await bot.send_message(
        chat_id=channel, text=text, parse_mode=ParseMode.HTML,
        link_preview_options=NO_PREVIEW, reply_markup=markup,
    )
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


async def publish(bot, channel: str, sections, order_markup, state: dict) -> dict:
    """Обновляет посты и меню. Возвращает отчёт: сколько обновлено/создано/нет в наличии."""
    report = {"updated": 0, "created": 0, "absent": 0, "pinned": None, "errors": []}
    known = state["sections"]
    plan = make_plan(sections, state)
    stamp_line = f"\n\n<i>Обновлено {stamp()}</i>"

    for sec in sections:
        old = known.get(sec.key, {}).get("message_id")
        try:
            mid = await _upsert(bot, channel, old, sec.text + stamp_line, order_markup)
        except TelegramError as exc:
            report["errors"].append(f"{sec.title}: {exc}")
            continue
        known[sec.key] = {"message_id": mid, "title": sec.title, "category": sec.category}
        report["created" if old is None or old != mid else "updated"] += 1
        save_state(state)

    for key in plan["absent"]:
        entry = known[key]
        text = f"<b>{entry['title']}</b>\n\nНет в наличии.{stamp_line}"
        try:
            await _upsert(bot, channel, entry["message_id"], text, None)
            report["absent"] += 1
        except TelegramError as exc:
            report["errors"].append(f"{entry['title']}: {exc}")

    absent = set(plan["absent"])
    menu_text = f"{MENU_TITLE}\nВыберите категорию 👇\n\n<i>Цены и наличие обновлены {stamp()}</i>"
    first_menu = not state.get("menu_id")
    try:
        state["menu_id"] = await _upsert(
            bot, channel, state.get("menu_id"), menu_text, _menu_markup(channel, state, absent)
        )
    except TelegramError as exc:
        report["errors"].append(f"меню: {exc}")
    else:
        save_state(state)
        if first_menu:
            try:
                await bot.pin_chat_message(chat_id=channel, message_id=state["menu_id"], disable_notification=True)
                report["pinned"] = True
            except TelegramError as exc:
                report["pinned"] = False
                log.warning("Не удалось закрепить меню: %s", exc)
    return report
