"""Превращает список товаров поставщика (канал ilublino) в пост для канала tovarkamh.

Здесь вся логика: фильтры, наценка, округление, категории, группировка iPhone по моделям.
Файл не зависит от Telegram, поэтому его можно проверять отдельно (см. test_catalog.py).
"""

import html
import re
from dataclasses import dataclass, field

# ---------- Настройки (меняйте здесь) ----------
MIN_PRICE = 4_000          # товары дешевле этой исходной цены не публикуем
MARKUP_THRESHOLD = 30_000  # граница: ниже — малая наценка, от неё и выше — большая
MARKUP_LOW = 2_000         # наценка для цены < 30 000
MARKUP_HIGH = 3_000        # наценка для цены >= 30 000
ROUND_TO = 1_000           # округление вверх до тысяч
TELEGRAM_LIMIT = 4_000     # безопасный размер одного сообщения (лимит Telegram — 4096)
BUTTON_MARKER = "[Кнопка: Заказать]"

# Порядок категорий в посте
CATEGORY_ORDER = [
    "iPhone", "iPad", "AirPods", "Apple Watch", "MacBook",
    "Samsung", "Galaxy",
    "Dyson", "PlayStation", "Marshall", "Canon", "Ray-Ban",
    "Блоки питания", "Кабели",
]

# Дополнительные категории по ключевым словам в названии (проверяются после всех брендов).
# Чтобы добавить новую: допишите строку сюда и название в CATEGORY_ORDER выше.
EXTRA_CATEGORIES = [
    ("Блоки питания", r"блок\s+питани|адаптер|зарядн|power\s*adapter|charger|\bmagsafe\b|\bpd\s?\d{2,3}\s?w|\b\d{2,3}\s?w\b"),
    ("Кабели", r"кабел|провод|\bcable\b|шнур|\blightning\b|\busb[-\s]?c\b.*\bto\b"),
]

# ---------- Фильтры ----------
# ASIS, CPO, Active, любая форма слов «Проблема» и «Уценка» (регистр не важен)
EXCLUDE_RE = re.compile(r"\b(?:as[\s-]?is|cpo|active)\b|проблем|уцен", re.IGNORECASE)

# Строка товара: «▫️ название – 12 500₽»
LINE_RE = re.compile(
    r"^\s*▫️?\s*(?P<body>.+?)\s*[–—−-]\s*(?P<price>\d[\d\s  .,]*)\s*₽"
)

# В скобках оставляем только eSim / 2 Sim
PAREN_RE = re.compile(r"\(([^()]*)\)")
KEEP_IN_PAREN_RE = re.compile(r"e-?sim|2\s*sim", re.IGNORECASE)

# ---------- Категории ----------
GALAXY_DEVICE_RE = re.compile(r"\b(?:watch|buds|tab|ring|book)\b", re.IGNORECASE)
SAMSUNG_PHONE_RE = re.compile(
    r"^(?:samsung\s+)?(?:galaxy\s+)?"
    r"(?:[samf]\d{1,3}[a-z]{0,3}\b|z\s?(?:fold|flip)|fold|flip|note\s?\d+)",
    re.IGNORECASE,
)


@dataclass
class Item:
    name: str
    price: int
    category: str
    group: str  # для iPhone — "iPhone 15" и т.п., для остальных = категория


@dataclass
class BuildResult:
    blocks: list = field(default_factory=list)      # готовые абзацы поста
    kept: int = 0                                   # сколько товаров попало в пост
    excluded: list = field(default_factory=list)    # отсеяно фильтрами (строка, причина)
    unmatched: list = field(default_factory=list)   # бренд не из списка — не публикуем
    unparsed: list = field(default_factory=list)    # строка с «▫️», но без цены


# ---------- Цена ----------
def retail_price(source_price: int) -> int:
    """Наценка + округление вверх до тысяч."""
    markup = MARKUP_HIGH if source_price >= MARKUP_THRESHOLD else MARKUP_LOW
    total = source_price + markup
    return -(-total // ROUND_TO) * ROUND_TO


def format_price(value: int) -> str:
    return f"{value:,}".replace(",", " ") + "₽"


# ---------- Название ----------
def clean_name(body: str) -> str:
    def _paren(match):
        kept = []
        for token in KEEP_IN_PAREN_RE.findall(match.group(1)):
            kept.append("eSim" if "e" in token.lower() else "2 Sim")
        return f"({', '.join(kept)})" if kept else ""

    name = PAREN_RE.sub(_paren, body)
    return re.sub(r"\s+", " ", name).strip()


# ---------- Категория ----------
def iphone_group(name: str) -> str:
    """Группа iPhone по поколению: iPhone 13, iPhone 15, iPhone 16e, iPhone SE, iPhone X..."""
    m = re.search(r"iphone\s*(\d{1,2}e?)(?!\d)", name, re.IGNORECASE)
    if m:
        return f"iPhone {m.group(1).lower()}"
    m = re.search(r"iphone\s*duo\b", name, re.IGNORECASE)
    if m:
        return "iPhone Duo"
    m = re.search(r"iphone\s*(se|air)\b", name, re.IGNORECASE)
    if m:
        return f"iPhone {m.group(1).upper() if m.group(1).lower() == 'se' else 'Air'}"
    if re.search(r"iphone\s*x[rs]?\b", name, re.IGNORECASE):
        return "iPhone X"
    return "iPhone"


def _iphone_sort_key(group: str):
    """iPhone SE, X, 11, 12 ... 16, 16e, 17, Air — по возрастанию."""
    tail = group[len("iPhone"):].strip().lower()
    m = re.match(r"(\d+)(e?)$", tail)
    if m:
        return (int(m.group(1)), m.group(2))
    return ({"se": 9, "x": 10, "air": 18, "duo": 19}.get(tail, 0), "")


def classify(name: str):
    """Возвращает (категория, группа) или (None, None), если бренд не из списка."""
    low = name.lower()

    if re.search(r"\biphone\b|айфон", low):
        return "iPhone", iphone_group(name)
    if re.search(r"\bipad\b", low):
        return "iPad", "iPad"
    if re.search(r"air\s?pods", low):
        return "AirPods", "AirPods"
    if re.search(r"mac\s?book", low):
        return "MacBook", "MacBook"
    if re.search(r"apple\s*watch", low):
        return "Apple Watch", "Apple Watch"

    # Планшеты Samsung без слова Galaxy в названии: «Tab S9 FE», «Tab A9+»
    if re.search(r"\btab\s?[sa]\s?\d", low):
        return "Galaxy", "Galaxy"

    if re.search(r"galaxy|samsung", low):
        if GALAXY_DEVICE_RE.search(name):
            return "Galaxy", "Galaxy"
        if SAMSUNG_PHONE_RE.match(name):
            return "Samsung", "Samsung"
        return "Galaxy", "Galaxy"
    if SAMSUNG_PHONE_RE.match(name):
        return "Samsung", "Samsung"

    if re.search(r"\bdyson\b", low):
        return "Dyson", "Dyson"
    if re.search(r"playstation|\bps\s?[45]\b|dualsense", low):
        return "PlayStation", "PlayStation"
    if re.search(r"\bmarshall\b", low):
        return "Marshall", "Marshall"
    if re.search(r"\bcanon\b", low):
        return "Canon", "Canon"
    if re.search(r"ray[\s-]?ban", low):
        return "Ray-Ban", "Ray-Ban"
    if re.match(r"\s*watch\b", low):
        return "Apple Watch", "Apple Watch"
    for extra, pattern in EXTRA_CATEGORIES:
        if re.search(pattern, low):
            return extra, extra
    return None, None


# ---------- Сборка поста ----------
def _collect(text: str):
    """Разбирает прайс: возвращает (BuildResult без блоков, {категория: {группа: [Item]}})."""
    result = BuildResult()
    by_category = {}  # категория -> {группа: [Item, ...]} (порядок появления сохраняется)

    for raw_line in text.splitlines():
        if not re.match(r"^\s*▫", raw_line):
            continue
        line = raw_line.strip().replace("`", "")  # поставщик иногда оформляет строки как код

        m = LINE_RE.match(line)
        if not m:
            result.unparsed.append(line)
            continue

        digits = re.sub(r"\D", "", m.group("price"))
        if not digits:
            result.unparsed.append(line)
            continue
        source_price = int(digits)
        body = m.group("body")

        if EXCLUDE_RE.search(line):  # смотрим всю строку, в т.ч. пометки после цены
            result.excluded.append((line, "ASIS/CPO/Active/Проблема/Уценка"))
            continue
        if source_price < MIN_PRICE:
            result.excluded.append((line, f"цена ниже {format_price(MIN_PRICE)}"))
            continue

        name = clean_name(body)
        category, group = classify(name)
        if category is None:
            result.unmatched.append(line)
            continue

        item = Item(name, retail_price(source_price), category, group)
        by_category.setdefault(category, {}).setdefault(group, []).append(item)
        result.kept += 1
    return result, by_category


def build_post(text: str) -> BuildResult:
    result, by_category = _collect(text)
    for category in CATEGORY_ORDER:
        groups = by_category.get(category)
        if not groups:
            continue
        names = list(groups)
        if category == "iPhone":
            names.sort(key=_iphone_sort_key)  # iPhone 13, 14, 15, 16, 16e ...
        for group in names:
            items = groups[group]
            lines = [f"<b>{html.escape(group, quote=False)}</b>"]
            for it in items:  # товары — в исходном порядке
                lines.append(
                    f"▫️ {html.escape(it.name, quote=False)} – {format_price(it.price)}"
                )
            result.blocks.append("\n".join(lines))
    return result


def render_post(blocks, with_button_marker: bool = False) -> str:
    text = "\n\n".join(blocks)
    if with_button_marker:
        text += "\n\n" + BUTTON_MARKER
    return text


def _split_long_block(block: str, limit: int):
    """Режет слишком длинный абзац по строкам, повторяя заголовок в каждой части."""
    header, *rows = block.split("\n")
    parts, current = [], header
    for row in rows:
        if len(current) + 1 + len(row) > limit:
            parts.append(current)
            current = header
        current += "\n" + row
    parts.append(current)
    return parts


def split_for_telegram(blocks, limit: int = TELEGRAM_LIMIT):
    """Склеивает абзацы в сообщения не длиннее лимита, не разрывая абзацы."""
    chunks, current = [], ""
    for block in blocks:
        parts = [block] if len(block) <= limit else _split_long_block(block, limit)
        for part in parts:
            if not current:
                current = part
            elif len(current) + 2 + len(part) <= limit:
                current += "\n\n" + part
            else:
                chunks.append(current)
                current = part
    if current:
        chunks.append(current)
    return chunks


# ---------- Разделы для канала (этап 2) ----------
SECTION_LIMIT = 3800  # запас под строку «Обновлено …» и заголовок


@dataclass
class Section:
    key: str       # стабильный ключ: «iPhone 15#1»; по нему бот находит свой пост в канале
    title: str     # заголовок поста, например «Samsung A» или «Samsung A (1/2)»
    category: str
    items: list
    text: str      # готовый HTML поста без строки «Обновлено»


def _samsung_series(name: str) -> str:
    low = name.lower()
    if re.search(r"z\s?(?:fold|flip)|\bfold|\bflip", low):
        return "Samsung Z"
    m = re.search(r"\b([asmf])\d", low)
    if m:
        return f"Samsung {m.group(1).upper()}"
    if re.search(r"note", low):
        return "Samsung Note"
    return "Samsung"


def _galaxy_series(name: str) -> str:
    m = re.search(r"\b(watch|buds|tab|ring|book)\b", name, re.IGNORECASE)
    return f"Galaxy {m.group(1).capitalize()}" if m else "Galaxy"


SERIES_FUNCS = {"Samsung": _samsung_series, "Galaxy": _galaxy_series}


def _item_line(it: Item) -> str:
    return f"▫️ {html.escape(it.name, quote=False)} – {format_price(it.price)}"


def _fits(title: str, items) -> bool:
    body = "\n".join(_item_line(i) for i in items)
    return len(title) + 8 + len(body) <= SECTION_LIMIT


def _pack(title: str, category: str, items) -> list:
    """Делит товары на части по размеру: «Название (1/2)», «Название (2/2)»."""
    parts, current, size = [], [], 0
    budget = SECTION_LIMIT - len(title) - 20
    for it in items:
        add = len(_item_line(it)) + 1
        if current and size + add > budget:
            parts.append(current)
            current, size = [], 0
        current.append(it)
        size += add
    if current:
        parts.append(current)
    total = len(parts)
    result = []
    for n, chunk in enumerate(parts, 1):
        shown = title if total == 1 else f"{title} ({n}/{total})"
        body = "\n".join(_item_line(i) for i in chunk)
        result.append(Section(f"{title}#{n}", shown, category, chunk,
                              f"<b>{html.escape(shown, quote=False)}</b>\n{body}"))
    return result


def build_sections(text: str):
    """Прайс -> (список разделов для постов канала, BuildResult с отчётом)."""
    result, by_category = _collect(text)
    sections = []
    for category in CATEGORY_ORDER:
        groups = by_category.get(category)
        if not groups:
            continue
        if category == "iPhone":
            for group in sorted(groups, key=_iphone_sort_key):
                sections += _pack(group, category, groups[group])
            continue
        items = [it for g in groups.values() for it in g]
        series_of = SERIES_FUNCS.get(category)
        if series_of is None:
            sections += _pack(category, category, items)
            continue
        by_series = {}  # Samsung и Galaxy всегда делятся по сериям, чтобы названия постов не менялись
        for it in items:
            by_series.setdefault(series_of(it.name), []).append(it)
        for series in sorted(by_series):
            sections += _pack(series, category, by_series[series])
    result.kept = sum(len(sec.items) for sec in sections)
    return sections, result
