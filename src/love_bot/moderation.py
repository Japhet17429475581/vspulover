import re
import unicodedata
from dataclasses import dataclass, replace
from typing import Literal, Protocol

from love_bot.blocklist import WordBlocklist

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
BULLYING = re.compile(r"(?:травите|затравим|унижайте|бейте|избейте)\b", re.I)


class RuleBasedClassifier:
    def __init__(self, blocklist: WordBlocklist | None = None):
        self.blocklist = blocklist if blocklist is not None else WordBlocklist()

    def classify(self, text: str, original: str) -> ModerationResult:
        probe = unicodedata.normalize("NFKC", text).casefold()
        probe = "".join(char for char in probe if unicodedata.category(char) != "Cf")
        flags: list[str] = []
        reasons: list[str] = []
        reject = False

        def flag(code: str, reason: str):
            flags.append(code)
            reasons.append(reason)

        if self.blocklist.matches(text) or self.blocklist.matches(original):
            reject = True
            flag("blocked_word", "обнаружен мат или выражение из запрещённого списка")
        if URL.search(probe):
            flag("link", "обнаружена ссылка или приглашение")
            if AD.search(probe) or "t.me/+" in probe or "joinchat" in probe:
                reject = True
                flag("advertising", "рекламная ссылка или приглашение")
        has_phone = any(sum(c.isdigit() for c in m.group()) >= 7 for m in PHONE.finditer(probe))
        if EMAIL.search(probe) or has_phone:
            flag("contact_data", "контактные данные требуют проверки согласия владельца")
            if re.search(
                r"(?:её|его|чужой|чужие|телефон\s+\w+|звоните|пишите\s+ей|пишите\s+ему)", probe
            ):
                reject = True
                flag("third_party_data", "возможное раскрытие контактов третьего лица")
        threat_probe = re.sub(r"\bубить\s+время\b", "", probe)
        if THREAT.search(threat_probe):
            if re.search(r"\b(?:я|мы|тебя|его|её|их|всех|давайте)\b", threat_probe):
                reject = True
                flag("threat", "явная угроза или призыв к насилию")
            else:
                flag("possible_threat", "возможная угроза; требуется контекст")
        if BULLYING.search(probe):
            reject = True
            flag("bullying", "призыв к травле или насилию")
        if HANDLE.search(probe) and AD.search(probe):
            reject = True
            flag("advertising_contact", "рекламный контакт")
        if reject:
            return ModerationResult("rejected", None, tuple(reasons), tuple(flags))
        if not flags:
            return ModerationResult(
                "auto_approved",
                None,
                ("запрещённых слов и других ограничивающих признаков не обнаружено",),
                ("rules_passed",),
            )
        return ModerationResult("manual_review", None, tuple(reasons), tuple(flags))


def apply_policy(result: ModerationResult, enabled: bool, threshold: float) -> ModerationResult:
    if result.decision != "auto_approved":
        return result
    allowed = enabled and (
        "rules_passed" in result.flags
        or (result.confidence is not None and result.confidence >= threshold)
    )
    if allowed:
        return result
    return replace(
        result,
        decision="manual_review",
        reasons=(*result.reasons, "автоподтверждение отключено или недостаточно уверенности"),
    )
