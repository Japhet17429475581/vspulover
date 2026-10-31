import pytest

from love_bot.blocklist import WordBlocklist
from love_bot.moderation import RuleBasedClassifier, apply_policy


@pytest.mark.parametrize(
    "text",
    [
        "тебя",
        "маму и папу",
        "когда отменяют пары",
        "свою группу",
        "осенний Волгоград",
        "тёплый чай после пар",
        "маму и папу и ещё кого-то",
        "студентку из 201 группы",
        "Сашу",
        "Иван Петров",
        "девушку, которая улыбнулась мне сегодня",
        "Машу из нашей группы",
        "когда преподаватель отпускает пораньше",
        "я просто люблю гулять",
        "Мaму и папу",
        "маму\u200b и папу",
        "маму и папу 👩‍🎓",
        "@someone",
        "убить время за книгой",
        "я люблю убить время за книгой",
        "тебя, несмотря на всё ❤️",
        "страхуй меня на скалодроме",
        "наш педагогический университет",
        "хулиганскую улыбку",
        "свой педикюр",
        "суккуленты",
        "ненавижу расставания, но люблю тебя",
        "Тебя, my sunshine ❤️",
    ],
)
def test_clean_confessions_are_auto_approved_without_whitelist(text):
    result = RuleBasedClassifier().classify(text, text)
    assert result.confidence is None
    assert apply_policy(result, False, 0.95).decision == "manual_review"
    assert apply_policy(result, True, 0.95).decision == "auto_approved"


@pytest.mark.parametrize(
    "text",
    [
        "тёплый чай после пар https://example.com",
        "www.example.com",
        "mail@example.ru",
        "+7 (900) 000-00-00",
        "убить одиночество",
    ],
)
def test_links_contacts_and_ambiguous_threats_need_review(text):
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
        "купить у @someone",
        "тебя, идиот (шутка)",
        "всех этих мудаков",
        "этот пиздец",
        "БЛЯТЬ",
        "х.у.й",
        "п и з д е ц",
        "хyй",
        "х*й",
        "бл@ть",
        "х\u200bуй",
        "х\u200dуй",
        "пизде́ц",
        "fucking ads",
        "ты мразь",
    ],
)
def test_blocked_words_and_clear_violations_are_rejected(text):
    result = RuleBasedClassifier().classify(text, text)
    assert result.decision == "rejected"
    assert result.reasons and result.flags


def test_editable_blocklist_uses_whole_words_and_optional_suffix():
    blocklist = WordBlocklist(["# comment", "", "мат", "запрещ*", "two words"])
    assert blocklist.matches("Мат")
    assert not blocklist.matches("математика и формат")
    assert blocklist.matches("запрещённое")
    assert blocklist.matches("TWO WORDS")
    assert not WordBlocklist([]).matches("любой текст")
    classifier = RuleBasedClassifier(blocklist)
    assert classifier.classify("мат", "мат").decision == "rejected"
    assert classifier.classify("математика", "математика").decision == "auto_approved"


def test_manual_mode_can_still_be_selected(settings):
    from love_bot.service import SubmissionService

    settings.auto_approve_enabled = False
    assert (
        SubmissionService(None, settings).preview("люблю Машу ❤️").moderation.decision
        == "manual_review"
    )
