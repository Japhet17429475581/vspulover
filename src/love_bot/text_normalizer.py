"""Pure formatting functions. No moderation, network calls or spelling changes."""

import hashlib
import re
import unicodedata

OPEN_QUOTES = "«“„\"'‘‹"
CLOSE_QUOTES = "»”‟“\"'’›"
PREFIX = re.compile(r"^[" + re.escape(OPEN_QUOTES) + r"]*\s*люблю(?![\w\u0300-\u036f])", re.I)
INVISIBLE = re.compile("[\u00ad\u200b\u200c\u200e\u200f\u202a-\u202e\u2060-\u206f\ufeff]")


class TextValidationError(ValueError):
    """A safe, user-facing validation message."""


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    text = INVISIBLE.sub("", text)
    text = "\n".join(re.sub(r"[^\S\n]+", " ", line).strip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    match = PREFIX.match(text)
    if match:
        quote_count = sum(char in OPEN_QUOTES for char in match.group())
        remainder = text[match.end() :]
        # Only strip closing quotes belonging to the removed prefix, not a quoted continuation.
        for _ in range(quote_count):
            if remainder and remainder[0] in CLOSE_QUOTES:
                remainder = remainder[1:]
        text = re.sub(r"^[\s,:;.!?…—–-]+", "", remainder).strip()
    return text


def validate_text(text: str, max_length: int = 500) -> None:
    if not text or not any(char.isalpha() for char in text):
        raise TextValidationError("напиши продолжение фразы — хотя бы несколько слов ♡")
    if len(text) > max_length:
        raise TextValidationError(f"чуть короче, пожалуйста — до {max_length} символов ♡")
    if any(unicodedata.category(char) == "Cc" and char not in "\n\t" for char in text):
        raise TextValidationError(
            "не удалось прочитать текст. попробуй написать его обычными буквами ♡"
        )
    if re.search(r"([^\s])\1{7,}", text, re.I):
        raise TextValidationError("попробуй написать мысль словами, без длинных повторов ♡")


def prepare_text(original: str, max_length: int = 500) -> str:
    if len(original) > 4096:
        raise TextValidationError("сообщение слишком длинное ♡")
    result = normalize_text(original)
    validate_text(result, max_length)
    return result


def fingerprint(text: str) -> str:
    canonical = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text).casefold()).strip()
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
