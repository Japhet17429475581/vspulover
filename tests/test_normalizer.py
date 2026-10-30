import pytest

from love_bot.text_normalizer import TextValidationError, fingerprint, normalize_text, prepare_text


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        ("люблю тебя", "тебя"),
        ("Люблю маму", "маму"),
        ("ЛЮБЛЮ свою группу", "свою группу"),
        ("  люблю   когда отменяют пары  ", "когда отменяют пары"),
        ("люблю, когда идёт дождь", "когда идёт дождь"),
        ("люблю: когда все вместе", "когда все вместе"),
        ("«люблю» тебя", "тебя"),
        ('"люблю" тебя', "тебя"),
        ("„люблю“ тебя", "тебя"),
        ("люблю ❤️ тебя", "❤️ тебя"),
        ("люблю — Маму и Папу", "Маму и Папу"),
        ("я люблю тебя", "я люблю тебя"),
        ("люблюсь", "люблюсь"),
        ("полюблю тебя", "полюблю тебя"),
        ("обожаю тебя", "обожаю тебя"),
        ("уважаю людей", "уважаю людей"),
        ("восхищаюсь тобой", "восхищаюсь тобой"),
        ("люблю\u00a0\u00a0тебя", "тебя"),
        ("лю\u200bблю тебя", "тебя"),
        ("свою  семью\n\n\nи\tдрузей", "свою семью\n\nи друзей"),
        ("люблю 👩‍🎓 маму", "👩‍🎓 маму"),
        ("люблю «наш университет»", "«наш университет»"),
        ("  Тебя\r\n и утро  ", "Тебя\nи утро"),
    ],
)
def test_normalization(original, expected):
    assert prepare_text(original) == expected


@pytest.mark.parametrize(
    "text",
    ["", "  ", "люблю", "ЛЮБЛЮ!!!", "«люблю»", "....", "❤️♡", "123456", "аааааааааа", "тебя\x00"],
)
def test_invalid_text(text):
    with pytest.raises(TextValidationError):
        prepare_text(text)


def test_length_after_normalization():
    content = "а б " * 125
    assert len(prepare_text("люблю " + content)) == 499
    with pytest.raises(TextValidationError):
        prepare_text(content + "ещё")
    with pytest.raises(TextValidationError):
        prepare_text("тебя", max_length=3)


def test_fingerprints_and_no_lowercasing():
    assert fingerprint("Тёплый  чай") == fingerprint("тёплый чай")
    assert normalize_text("люблю МАМу ❤️") == "МАМу ❤️"
