"""Разбор PDF-накладной (формат типа "ZEN OPT": группы, строки, названия внизу страницы)."""
import io
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from pypdf import PdfReader

MONEY = re.compile(r"^\d+,\d{2}$")
GROUP_RE = re.compile(r"^\d+\s*шт\.\s+\S")
ROW_START = re.compile(r"^\d+(\s|$)")
UNIT_RE = re.compile(r"^[^\W\d_]+\.?$")


@dataclass
class Item:
    name: str
    qty: Decimal
    price: Decimal


@dataclass
class ParseResult:
    items: list
    doc_total: Optional[Decimal]
    doc_qty: Optional[int]
    named_ok: bool

    @property
    def calc_total(self) -> Decimal:
        return sum((i.price * i.qty for i in self.items), Decimal(0))


def _money(s: str) -> Decimal:
    return Decimal(s.replace("\xa0", "").replace(" ", "").replace(",", "."))


def _try_row(text: str):
    t = text.split()
    if len(t) < 5 or not t[0].isdigit():
        return None
    if not (MONEY.match(t[-1]) and re.fullmatch(r"\d+([.,]\d+)?", t[-3])
            and MONEY.match(t[-4]) and UNIT_RE.match(t[-2])):
        return None
    total = _money(t[-1])
    qty = Decimal(t[-3].replace(",", "."))
    price = _money(t[-4])
    name_tokens = t[1:-4]
    if abs(price * qty - total) > Decimal("0.02"):
        # возможно, в цене есть разделитель тысяч: "1 255,00"
        if len(t) >= 6 and re.fullmatch(r"\d{1,3}", t[-5]):
            p2 = _money(t[-5] + t[-4])
            if abs(p2 * qty - total) <= Decimal("0.02"):
                price, name_tokens = p2, t[1:-5]
            else:
                return None
        else:
            return None
    return " ".join(name_tokens).strip() or None, qty, price


def _balanced(s: str) -> bool:
    return s.count("(") == s.count(")")


def _assemble_names(lines, keys):
    """Новая запись начинается со строки, где встречается ключ группы (бренд)."""
    entries, cur = [], ""
    for l in lines:
        starts = any(k in l.lower() for k in keys)
        if starts and cur and _balanced(cur):
            entries.append(cur)
            cur = l
        else:
            cur = (cur + " " + l).strip()
    if cur:
        entries.append(cur)
    return entries


def _parse_page(text: str):
    rows, name_lines = [], []
    group, buf, seen_group, in_names = None, None, False, False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("Итого"):
            continue
        if buf is not None:
            buf += " " + line
            r = _try_row(buf)
            if r:
                rows.append((group, *r))
                buf = None
            elif len(buf) > 400:
                buf = None
            continue
        if GROUP_RE.match(line):
            group = re.sub(r"^\d+\s*шт\.\s+", "", line)
            seen_group, in_names = True, False
            continue
        if not seen_group:
            continue
        if not in_names and ROW_START.match(line):
            r = _try_row(line)
            if r:
                rows.append((group, *r))
            else:
                buf = line
            continue
        in_names = True
        name_lines.append(line)

    keys = {r[0].split()[0].lower() for r in rows if r[0]}
    entries = _assemble_names(name_lines, keys)
    nameless = [i for i, r in enumerate(rows) if r[1] is None]
    ok = len(nameless) == len(entries)
    items = []
    k = 0
    for g, name, qty, price in rows:
        if name is None:
            if ok:
                name = entries[k]
            else:
                name = f"{g} (без названия)"
            k += 1
        items.append(Item(name=name, qty=qty, price=price))
    return items, ok


def parse_pdf(data: bytes) -> ParseResult:
    reader = PdfReader(io.BytesIO(data))
    items, all_ok = [], True
    full = ""
    for page in reader.pages:
        text = page.extract_text() or ""
        full += text + "\n"
        its, ok = _parse_page(text)
        items += its
        all_ok = all_ok and ok
    doc_total = doc_qty = None
    m = re.search(r"Итого:\s*(\d+)\s+([\d\s]+(?:,\d{2})?)", full)
    if m:
        doc_qty = int(m.group(1))
        try:
            doc_total = _money(m.group(2).strip() if "," in m.group(2) else m.group(2).strip() + ",00")
        except Exception:
            pass
    return ParseResult(items, doc_total, doc_qty, all_ok)
