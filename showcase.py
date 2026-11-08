"""Раздел «Разное»: товары приходят из учёта (Google Apps Script).

В канале раздел — это страницы-списки «1. Название – цена» с кнопками-номерами; их публикует
бот учёта от имени этого бота (без звука). Номер ведёт в чат с ботом (t.me/<бот>?start=p<номер>),
где бот показывает фото, цену, характеристики и кнопку «Заказать». Здесь бот раз в минуту
спрашивает у учёта, что нового, и

  * показывает раздел кнопкой в меню «Каталог»;
  * запоминает товары для карточек;
  * после того как страницы перестали меняться, снова отправляет меню в самый низ канала.

Файл не зависит от Telegram, поэтому логику решения можно проверять отдельно.
"""

import asyncio
import json
import html
import os
import re
import urllib.parse
import urllib.request

SYNC_URL = os.getenv("SYNC_URL", "").strip()  # адрес веб-приложения учёта (заканчивается на /exec)
SYNC_KEY = os.getenv("SYNC_KEY", "").strip()  # общий пароль, такой же в свойствах скрипта учёта
SYNC_EVERY = int(os.getenv("SYNC_SECONDS", "60"))  # как часто спрашивать учёт
QUIET = int(os.getenv("SYNC_QUIET_SECONDS", "120"))  # сколько секунд учёт должен молчать, прежде чем обновлять меню
RETRY_AFTER_FAIL = 300  # пауза после сбоя


def enabled() -> bool:
    return bool(SYNC_URL and SYNC_KEY)


def _fetch_sync() -> dict:
    query = urllib.parse.urlencode({"action": "showcase", "key": SYNC_KEY})
    url = f"{SYNC_URL}{'&' if '?' in SYNC_URL else '?'}{query}"
    with urllib.request.urlopen(url, timeout=20) as resp:  # Apps Script отвечает через перенаправление, urllib его проходит
        return json.loads(resp.read().decode("utf-8"))


async def fetch_info():
    """Ответ учёта или None, если ключ не подошёл. Ошибки сети поднимаются наружу."""
    data = await asyncio.to_thread(_fetch_sync)
    return data if isinstance(data, dict) and data.get("ok") else None


def decide(info: dict, state: dict, quiet: int = QUIET):
    """Что делать с меню. Возвращает (extra, changed, refresh).

    extra   — кнопка раздела для меню ({"hub": id, "title": ...}) или None, если раздела ещё нет;
    changed — кнопка раздела отличается от той, что сейчас в меню;
    refresh — пора заново отправить меню: меню уже есть в канале, раздел изменился
              или карточки оказались ниже него, и учёт молчит не меньше quiet секунд.
    """
    hub = int(info.get("hub") or 0)
    extra = {"hub": hub, "title": str(info.get("title") or "Разное")} if hub else None
    changed = extra != state.get("extra")
    menu_id = int(state.get("menu_id") or 0)
    buried = bool(menu_id) and int(info.get("last") or 0) > menu_id
    calm = int(info.get("since") or 0) >= quiet
    return extra, changed, bool(menu_id) and (changed or buried) and calm


START_RE = re.compile(r"^p(\d{1,9})$")


def parse_start_arg(arg):
    """'p12' -> 12 (ссылка из списка в канале), иначе None."""
    m = START_RE.match(arg or "")
    return int(m.group(1)) if m else None


def remember(bot_data: dict, info: dict) -> None:
    """Запоминает товары из ответа учёта: {id: {...}}. Полный список заменяет прежний."""
    items = {}
    for it in info.get("items") or []:
        try:
            items[int(it["id"])] = it
        except (KeyError, TypeError, ValueError):
            continue
    bot_data["showcase_items"] = items
    bot_data["showcase_title"] = str(info.get("title") or "Разное")


def price_text(price) -> str:
    return f"{int(round(float(price or 0))):,}".replace(",", " ") + "₽"


def item_text(it: dict) -> str:
    """Подпись карточки: название, цена, характеристики (HTML)."""
    text = f"<b>{html.escape(str(it.get('name', '')))}</b>\n{price_text(it.get('price'))}"
    if it.get("text"):
        text += "\n\n" + html.escape(str(it["text"]))
    return text
