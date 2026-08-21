"""Дымовой прогон бота: всё, что проверяется без Slack и без вызовов модели.

Запуск из корня репозитория:  python bot/test_smoke.py

Тест намеренно не трогает сеть. Проверяется то, что ломается тихо:
дедупликация карточек, дешёвый фильтр и отрисовка карточки, которая
служит записью состояния для часового агента.
"""

import os
import pathlib
import sys
import tempfile

os.environ["GLEBOT_DB"] = os.path.join(tempfile.mkdtemp(), "test.sqlite3")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-placeholder")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import classifier  # noqa: E402
import filters  # noqa: E402
import jira  # noqa: E402
import store  # noqa: E402

OWNER, BOT = "U0BK99FCH7F", "U0BOTBOT"


def ok(message: str) -> None:
    print("  ok  ", message)


def test_store():
    store.init()
    draft_id = store.create(
        cls="BUG", confidence="высокая", src_channel="C1", src_ts="1.1",
        src_thread_ts=None, src_author="Тимур", src_excerpt="не листается",
        reply_text="Похоже на баг.", jira_summary="Профиль: не листается",
    )
    assert len(draft_id) == 4
    assert store.already_seen("C1", "1.1")
    assert not store.already_seen("C1", "9.9")

    store.attach_card(draft_id, "D1", "2.2")
    assert store.by_card("D1", "2.2")["status"] == "awaiting"

    store.resolve(draft_id, "posted", "TEAMDEV-999")
    assert store.by_id(draft_id)["jira_key"] == "TEAMDEV-999"
    ok("создание, поиск по карточке, разрешение")

    # Slack умеет доставить одно событие дважды. Без уникального индекса
    # владелец получит две одинаковые карточки на одно сообщение.
    try:
        store.create(
            cls="BUG", confidence="высокая", src_channel="C1", src_ts="1.1",
            src_thread_ts=None, src_author="x", src_excerpt="x",
            reply_text="x", jira_summary="",
        )
        raise AssertionError("дубль прошёл — уникальный индекс не работает")
    except Exception as exc:
        assert "UNIQUE" in str(exc), exc
    ok("повторная карточка на то же сообщение отклонена базой")


def test_jira_optional():
    assert jira.configured() is False
    assert jira.find_similar("что угодно") == []
    ok("не настроена — молчит, но не падает")


def test_rubric():
    rubric = classifier._rubric()
    assert len(rubric) > 3000, "рубрика подозрительно короткая"
    assert "DISCUSSION" in rubric and "Похоже на баг" in rubric
    ok(f"загружена из references/, {len(rubric)} символов")


def test_prefilter():
    # Примеры — настоящие сообщения из #разработка-обсуждение и #qa-баги.
    cases = [
        ({"user": "U1", "text": "БРР БРР"}, False, "мем"),
        ({"user": "U1", "text": "как часы :slightly_smiling_face:"}, False,
         "короткий текст с длинным эмодзи-кодом"),
        ({"user": "USLACKBOT", "text": "A huddle started",
          "subtype": "huddle_thread"}, False, "хадл"),
        ({"user": OWNER, "text": "Всем привет! У нас появился новый проект"},
         False, "сообщение самого владельца"),
        ({"user": BOT, "text": "Похоже на баг, передаю Глебу"}, False,
         "собственные сообщения бота"),
        ({"user": "U1", "text": f"<@{OWNER}> глянь"}, True,
         "тег владельца разбирается даже одним словом"),
        ({"user": "U1", "text": "Профиль: не листается список эмблем и рамок."},
         True, "баг-репорт"),
        ({"user": "U1", "text": "Крайне тупо, что это делается на личные акки. "
                                "Мб корп акк стима есть?"}, True, "вопрос про доступы"),
        ({"user": "U1", "text": "+"}, False, "плюсик"),
        ({"user": "U1", "bot_id": "B1", "text": "деплой прошёл"}, False, "бот"),
        ({"user": "U1", "text": ""}, False, "пустое"),
    ]
    for event, expected, label in cases:
        got = filters.worth_classifying(event, owner_id=OWNER, bot_id=BOT)
        assert got is expected, f"{label}: ждали {expected}, получили {got}"
    ok(f"{len(cases)} случаев разобраны верно")


def test_card():
    card = filters.card_text(
        "a1b2",
        {"cls": "BUG", "confidence": "высокая",
         "reply": "Похоже на баг: не листается список эмблем.",
         "jira_summary": "Профиль: не листается список эмблем"},
        channel="qa-баги", author="Иван Чмутов",
        excerpt="Профиль: не листается список эмблем и рамок",
        link="https://cobaltlabworkspace.slack.com/archives/C1/p11",
        jira_project="TEAMDEV", jira_issue_type="Баг",
    )
    for needed in ("Черновик", "a1b2", "BUG", "qa-баги", "Иван Чмутов",
                   "открыть тред", "Заготовка тикета", "TEAMDEV", "запостить"):
        assert needed in card, needed
    ok("все поля на месте")

    revision = filters.card_text(
        "a1b2", {"cls": "BUG", "confidence": "высокая", "reply": "короче",
                 "jira_summary": ""},
        channel="qa-баги", author="Иван", excerpt="x", link="", revision=2,
    )
    assert "редакция 2" in revision
    assert "Заготовка тикета" not in revision
    ok("правка помечена, лишний блок тикета не рисуется")


if __name__ == "__main__":
    for name, fn in [
        ("хранилище черновиков", test_store),
        ("jira без настройки", test_jira_optional),
        ("рубрика классификатора", test_rubric),
        ("дешёвый фильтр", test_prefilter),
        ("карточка черновика", test_card),
    ]:
        print(name)
        fn()
    print("\nвсё зелёное")
