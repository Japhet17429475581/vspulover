import re
import unicodedata
from dataclasses import dataclass, replace
from typing import Literal, Protocol

Decision = Literal["auto_approved", "manual_review", "rejected"]


@dataclass(frozen=True)
class ModerationResult:
    decision: Decision
    # Heuristics deliberately provide no probability estimate.
    confidence: float | None
    reasons: tuple[str, ...]
    flags: tuple[str, ...]


class ModerationClassifier(Protocol):
    def classify(self, text: str, original: str) -> ModerationResult: ...


SAFE_PHRASES = frozenset(
    {
        "маму и папу",
        "когда отменяют пары",
        "свою группу",
        "осенний волгоград",
        "тёплый чай после пар",
    }
)
URL = re.compile(
    r"(?:https?://|www\.|t\s*\.\s*me/|telegram\s*\.\s*me/|tg://|"
    r"\b[\w-]+\.(?:ru|com|net|org|рф|me|io|xyz|online|site|top)\b)",
    re.I,
)
EMAIL = re.compile(r"[\w.+-]+\s*@\s*[\w-]+(?:\s*\.\s*[\w-]+)+", re.I)
PHONE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{5,}\d(?!\w)")
HANDLE = re.compile(r"(?<!\w)@[a-z\d_]{3,}", re.I)
AD = re.compile(r"подпиш|заработ|промокод|скидк|купить|покупай|регистрируй|реклам", re.I)
THREAT = re.compile(r"\b(?:убью|убей|убить|зарежу|изобью|расстрелять|уничтожим)\b", re.I)
INSULT = re.compile(r"\b(?:тварь|мразь|шлюха|урод|идиот|дебил|ненавижу)\w*", re.I)
BULLYING = re.compile(r"(?:травите|затравим|унижайте|бейте|избейте)\b", re.I)
PERSON = re.compile(
    r"\b(?:студент\w*|преподавател\w*|девушк\w*|парн\w*|парень|одногрупп\w*|"
    r"иван\w*|мария|маш[ауе]|анн[ауе]|саш[ауе]|дмитри\w*|ольг\w*|наст[яюе])\b",
    re.I,
)
NAME = re.compile(r"\b[А-ЯЁ][а-яё]{2,}(?:\s+[А-ЯЁ][а-яё]{2,})+\b")


class RuleBasedClassifier:
    def classify(self, text: str, original: str) -> ModerationResult:
        probe = unicodedata.normalize("NFKC", text).casefold()
        probe = "".join(char for char in probe if unicodedata.category(char) != "Cf")
        flags: list[str] = []
        reasons: list[str] = []
        reject = False

        def flag(code: str, reason: str):
            flags.append(code)
            reasons.append(reason)

        if any(unicodedata.category(c) == "Cf" for c in original):
            flag("unusual_unicode", "невидимые или управляющие символы; нужна проверка")
        if any(re.search("[a-z]", word) and re.search("[а-яё]", word) for word in probe.split()):
            flag("mixed_scripts", "смешение алфавитов; возможен обход фильтров")
        if URL.search(probe):
            flag("link", "обнаружена ссылка или приглашение")
            if AD.search(probe) or "t.me/+" in probe or "joinchat" in probe:
                reject = True
                flag("advertising", "рекламная ссылка или приглашение")
        has_phone = any(sum(c.isdigit() for c in m.group()) >= 7 for m in PHONE.finditer(probe))
        if EMAIL.search(probe) or HANDLE.search(probe) or has_phone:
            flag("contact_data", "контактные данные требуют проверки согласия владельца")
            if re.search(
                r"(?:её|его|чужой|чужие|телефон\s+\w+|звоните|пишите\s+ей|пишите\s+ему)", probe
            ):
                reject = True
                flag("third_party_data", "возможное раскрытие контактов третьего лица")
        if THREAT.search(probe):
            if re.search(r"\b(?:я|мы|тебя|его|её|их|всех|давайте)\b", probe):
                reject = True
                flag("threat", "явная угроза или призыв к насилию")
            else:
                flag("possible_threat", "возможная угроза; требуется контекст")
        if BULLYING.search(probe):
            reject = True
            flag("bullying", "призыв к травле или насилию")
        if INSULT.search(probe):
            flag("possible_insult", "возможное оскорбление; требуется контекст")
        if PERSON.search(probe) or NAME.search(text):
            flag("person_reference", "упоминание конкретного человека или третьих лиц")
        if reject:
            return ModerationResult("rejected", None, tuple(reasons), tuple(flags))
        if not flags and probe in SAFE_PHRASES:
            return ModerationResult(
                "auto_approved",
                None,
                ("точное совпадение со строгим списком безопасных фраз",),
                ("safe_whitelist",),
            )
        if not reasons:
            reasons.append("нет совпадения со строгим списком; требуется ручная проверка")
        return ModerationResult("manual_review", None, tuple(reasons), tuple(flags))


def apply_policy(result: ModerationResult, enabled: bool, threshold: float) -> ModerationResult:
    if result.decision != "auto_approved":
        return result
    allowed = enabled and (
        "safe_whitelist" in result.flags
        or (result.confidence is not None and result.confidence >= threshold)
    )
    if allowed:
        return result
    return replace(
        result,
        decision="manual_review",
        reasons=(*result.reasons, "автоподтверждение отключено или недостаточно уверенности"),
    )
