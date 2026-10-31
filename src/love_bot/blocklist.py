"""Editable word filter with Unicode, common homoglyphs and separated-letter handling."""

import re
import unicodedata
from importlib.resources import files

# Used only for matching, never to change the author's confirmed text.
HOMOGLYPHS = str.maketrans(
    {
        "a": "а",
        "b": "в",
        "c": "с",
        "e": "е",
        "h": "н",
        "k": "к",
        "m": "м",
        "o": "о",
        "p": "р",
        "t": "т",
        "x": "х",
        "y": "у",
    }
)


def filter_probe(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in folded if unicodedata.category(c) not in {"Cf", "Mn"})


class WordBlocklist:
    def __init__(self, entries=None):
        if entries is None:
            entries = (
                files("love_bot")
                .joinpath("data/blocked_words.txt")
                .read_text(encoding="utf-8")
                .splitlines()
            )
        patterns = []
        for entry in entries:
            entry = filter_probe(entry.strip())
            if not entry or entry.startswith("#"):
                continue
            stem = entry.endswith("*")
            word = entry[:-1] if stem else entry
            if not word or not any(c.isalpha() for c in word):
                raise ValueError("Blocked entries must contain a word")
            # Whole-word boundaries avoid matching 'хуй' inside 'подстрахуй'.
            # Punctuation/spacing between letters catches 'х.у.й' and 'п и з д е ц'.
            letters = r"[\W_]*".join(re.escape(c) for c in word)
            patterns.append(r"(?<!\w)" + letters + (r"\w*" if stem else "") + r"(?!\w)")
        self.pattern = re.compile("|".join(patterns)) if patterns else None

    def matches(self, text: str) -> bool:
        if self.pattern is None:
            return False
        probe = filter_probe(text)
        return bool(self.pattern.search(probe) or self.pattern.search(probe.translate(HOMOGLYPHS)))
