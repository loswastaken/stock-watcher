"""Small parsing helpers shared by the generic and Apple checkers."""
from __future__ import annotations

import html as html_lib
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Iterator
from urllib.parse import urljoin

_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")


def clean_text(s: Any) -> str:
    """Unescape entities, turn <br> into spaces, strip tags and collapse whitespace."""
    if s is None:
        return ""
    s = str(s)
    s = re.sub(r"<br\s*/?>", " ", s, flags=re.I)
    s = _TAG_RE.sub("", s)
    s = html_lib.unescape(s).replace("\xa0", " ").replace("​", "")
    return _WS_RE.sub(" ", s).strip()


def absolutize(url: Any, base: str) -> str | None:
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if not url or url.startswith("data:"):
        return None
    return urljoin(base, url)


CURRENCY_SYMBOLS = {
    "USD": "$",
    "CAD": "CA$",
    "AUD": "A$",
    "NZD": "NZ$",
    "HKD": "HK$",
    "SGD": "S$",
    "MXN": "MX$",
    "EUR": "€",
    "GBP": "£",
    "JPY": "¥",
    "CNY": "CN¥",
    "INR": "₹",
    "KRW": "₩",
}
_ZERO_DECIMAL = {"JPY", "KRW"}


def parse_amount(value: Any) -> Decimal | None:
    """Parse numbers like 1099, "1,099.00", "1.099,00", "$1,099", "USD 12.5"."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    s = re.sub(r"[^\d.,-]", "", str(value))
    if not s or not re.search(r"\d", s):
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):  # 1.099,00 (European)
            s = s.replace(".", "").replace(",", ".")
        else:  # 1,099.00
            s = s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = s.replace(",", "") if len(tail) == 3 else head.replace(",", "") + "." + tail
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return d if d >= 0 else None


def format_price(amount: Any, currency: Any = None) -> str | None:
    d = parse_amount(amount)
    if d is None:
        return None
    cur = str(currency).strip().upper() if currency else ""
    if cur in _ZERO_DECIMAL:
        num = f"{d:,.0f}"
    else:
        num = f"{d:,.2f}"
    if not cur:
        return f"${num}" if _looks_dollar(amount) else num
    sym = CURRENCY_SYMBOLS.get(cur)
    return f"{sym}{num}" if sym else f"{num} {cur}"


def _looks_dollar(v: Any) -> bool:
    return isinstance(v, str) and "$" in v


def loads_lenient(text: str) -> Any:
    """json.loads that tolerates common JSON-LD sloppiness (comments, CDATA, trailing commas,
    raw control characters). Returns None when it can't be parsed."""
    if text is None:
        return None
    t = text.strip()
    if not t:
        return None
    t = re.sub(r"^\s*(<!--|//\s*<!\[CDATA\[|<!\[CDATA\[)", "", t)
    t = re.sub(r"(-->|//\s*\]\]>|\]\]>)\s*$", "", t).strip()
    try:
        return json.loads(t, strict=False)
    except ValueError:
        pass
    t2 = re.sub(r",\s*([}\]])", r"\1", t)
    try:
        return json.loads(t2, strict=False)
    except ValueError:
        pass
    # Several objects concatenated without an enclosing array.
    try:
        dec = json.JSONDecoder(strict=False)
        out, i = [], 0
        while i < len(t2):
            while i < len(t2) and t2[i] in " \t\r\n;,":
                i += 1
            if i >= len(t2):
                break
            obj, i = dec.raw_decode(t2, i)
            out.append(obj)
        return out or None
    except ValueError:
        return None


def extract_balanced(text: str, start: int) -> str | None:
    """Return the balanced {...} or [...] literal starting at text[start] (string-aware)."""
    if start >= len(text) or text[start] not in "{[":
        return None
    open_ch = text[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_str: str | None = None
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == in_str:
                in_str = None
            continue
        if c in "\"'":
            in_str = c
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def walk(obj: Any) -> Iterator[Any]:
    """Depth-first iteration over every dict/list node in a JSON structure."""
    stack = [obj]
    seen = 0
    while stack:
        cur = stack.pop()
        seen += 1
        if seen > 200_000:  # safety valve
            return
        yield cur
        if isinstance(cur, dict):
            stack.extend(reversed(list(cur.values())))
        elif isinstance(cur, list):
            stack.extend(reversed(cur))
