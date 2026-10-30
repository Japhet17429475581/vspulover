import pytest

from love_bot.moderation import SAFE_PHRASES, RuleBasedClassifier, apply_policy


@pytest.mark.parametrize("text", sorted(SAFE_PHRASES))
def test_safe_list_requires_explicit_enable(text):
    result = RuleBasedClassifier().classify(text, text)
    assert result.confidence is None
    assert apply_policy(result, False, 0.95).decision == "manual_review"
    assert apply_policy(result, True, 0.95).decision == "auto_approved"


@pytest.mark.parametrize(
    "text",
    [
        "тебя",
        "маму и папу и ещё кого-то",
        "студентку из 201 группы",
        "Сашу",
        "Иван Петров",
        "когда преподаватель отпускает пораньше",
        "тёплый чай после пар https://example.com",
        "тебя, идиот (шутка)",
        "я просто люблю гулять",
        "www.example.com",
        "Мaму и папу",
        "маму\u200b и папу",
        "маму и папу 👩‍🎓",
        "@someone",
        "mail@example.ru",
        "+7 (900) 000-00-00",
        "убить время за книгой",
    ],
)
def test_uncertain_is_never_auto_approved(text):
    result = RuleBasedClassifier().classify(text, text)
    assert apply_policy(result, True, 0.95).decision == "manual_review"


@pytest.mark.parametrize(
    "text",
    [
        "подпишись https://spam.example.ru",
        "скидки на shop.com",
        "t.me/+invite",
        "я убью тебя",
        "давайте убить их",
        "травите его",
        "её телефон +7 (900) 000-00-00",
        "пишите ей secret@example.com",
        "я у\u200bбью тебя",
    ],
)
def test_clear_violations_are_rejected(text):
    result = RuleBasedClassifier().classify(text, text)
    assert result.decision == "rejected"
    assert result.reasons and result.flags


def test_preview_policy_defaults(settings):
    from love_bot.service import SubmissionService

    assert (
        SubmissionService(None, settings).preview("люблю маму и папу").moderation.decision
        == "manual_review"
    )
