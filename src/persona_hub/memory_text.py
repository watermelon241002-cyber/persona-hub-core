"""Small text helpers shared by the memory pipeline and the desire context."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
import math
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Credentials may stay in historical storage for audit or recovery, but they
# must never be surfaced into a model prompt. Apply this at every boundary
# where stored text becomes prompt text.
_CREDENTIAL_PATTERNS = (
    re.compile(r"(?i)authorization\s*[:=]\s*bearer\s+[^\s,;]+"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|token|secret|password|密钥|令牌|密码)"
        r"\s*(?:是|为|[:=])\s*[^\s,;，。]+"
    ),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
)
REDACTED = "[redacted]"


def redact_credentials(text: str) -> str:
    value = str(text or "")
    for pattern in _CREDENTIAL_PATTERNS:
        value = pattern.sub(REDACTED, value)
    return value


def trim(text: str, max_chars: int) -> str:
    text = str(text or "")
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."


def clean_quote(value: Any, max_chars: int = 300) -> str:
    """Quotes must stay verbatim, but markdown emphasis and line breaks make them dirty.

    Remove formatting only; never touch the words.
    """

    text = str(value or "").replace("**", "").replace("*", "")
    return trim(re.sub(r"\s+", " ", text).strip(), max_chars)


def clean_tags(value: Any, *, limit: int = 12) -> list[str]:
    if not isinstance(value, list):
        return []
    tags = [str(item).strip()[:40] for item in value if str(item).strip()]
    return tags[:limit]


def float_range(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    if math.isnan(number):
        number = default
    return max(minimum, min(maximum, number))


def optional_float_range(value: Any, minimum: float, maximum: float) -> float | None:
    if value is None or value == "":
        return None
    return float_range(value, 0.0, minimum, maximum)


def parse_timezone(value: str | None) -> tzinfo:
    """Accept an IANA name ("Asia/Shanghai") or a fixed offset ("+08:00")."""

    text = (value or "").strip()
    if not text or text.upper() == "UTC":
        return timezone.utc
    match = re.fullmatch(r"([+-])(\d{1,2}):?(\d{2})?", text)
    if match:
        sign = 1 if match.group(1) == "+" else -1
        offset = timedelta(hours=int(match.group(2)), minutes=int(match.group(3) or 0))
        return timezone(sign * offset)
    try:
        return ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown timezone: {text}") from exc


def parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def local_time_text(value: Any, tz: tzinfo, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Render a stored UTC timestamp in the people's own time zone.

    Feeding raw UTC to a summarizer makes it read UTC as local time, so every
    "afternoon" and "late at night" in a scene shifts by the offset. A scene
    anchored to the wrong hour is no anchor at all.
    """

    parsed = parse_timestamp(value)
    if parsed is None:
        return str(value or "")
    return parsed.astimezone(tz).strftime(fmt)


def _similarity_terms(text: str) -> set[str]:
    lowered = str(text or "").lower()
    terms = set(re.findall(r"[a-z0-9_]{2,}", lowered))
    for chunk in re.findall(r"[一-鿿]+", lowered):
        if len(chunk) <= 6:
            terms.add(chunk)
        for size in (2, 3, 4):
            for index in range(0, max(0, len(chunk) - size + 1)):
                terms.add(chunk[index : index + size])
    return {term for term in terms if term.strip()}


def text_similarity(left: str, right: str) -> float:
    """Overlap of words and CJK n-grams, normalised to 0..1."""

    left_terms = _similarity_terms(left)
    right_terms = _similarity_terms(right)
    if not left_terms or not right_terms:
        return 0.0
    return len(left_terms & right_terms) / math.sqrt(len(left_terms) * len(right_terms))
