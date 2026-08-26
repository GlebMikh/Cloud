"""Дымовой прогон бота: всё, что проверяется без Slack и без вызовов модели.

Запуск из корня репозитория:  python bot/test_smoke.py

Тест намеренно не трогает сеть. Проверяется то, что ломается тихо:
дедупликация карточек, дешёвый фильтр и отрисовка карточки, которая
служит записью состояния для часового агента.
"""

import json
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
        # Баг-репорт со скриншотом. Slack помечает такое сообщение подтипом
        # file_share, и отсев «любой подтип — мимо» съедал именно те
        # сообщения, ради которых бот и заведён.
        ({"user": "U1", "subtype": "file_share",
          "text": "Не валидируется корректность загрузки ассетов на картинки "
                  "предметов в кейсах, периодически вот такая ерунда загружается"},
         True, "баг-репорт со скриншотом"),
        ({"user": "U1", "subtype": "thread_broadcast",
          "text": "Дублирую в канал: на проде платежи не проходят"}, True,
         "ответ из треда, продублированный в канал"),
        ({"user": "U1", "subtype": "file_share", "text": ""}, False,
         "скриншот без единого слова — разбирать нечего"),
        ({"user": "U1", "subtype": "message_changed",
          "text": "Профиль: не листается список эмблем"}, False,
         "редактирование чужого сообщения"),
        ({"user": "U1", "subtype": "channel_join", "text": "has joined the channel"},
         False, "вход в канал"),
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


def test_decisions():
    """Разбор ответа владельца — свободная форма, а не строгий синтаксис."""
    cases = [
        ("ок", "approve", None),
        ("ОК", "approve", None),
        ("да", "approve", None),
        ("+", "approve", None),
        ("нет", "reject", None),
        ("отмена", "reject", None),
        ("ок+жира", "ticket", None),
        ("7c1a ок", "approve", "7c1a"),
        ("`7c1a` нет", "reject", "7c1a"),
        ("7c1a", "unclear", "7c1a"),
        ("убери первое предложение", "revise", None),
    ]
    for text, action, draft_id in cases:
        got = filters.parse_decision(text)
        assert got["action"] == action, f"{text!r}: ждали {action}, получили {got['action']}"
        assert got["draft_id"] == draft_id, f"{text!r}: id {got['draft_id']}"
    ok(f"{len(cases)} формулировок разобраны верно")

    # Слово из hex-букв не должно съедаться как номер карточки: полный текст
    # сохраняется, и вызывающий код откатится к нему, не найдя такой черновик.
    parsed = filters.parse_decision("abc сделай короче")
    assert parsed["draft_id"] == "abc"
    assert parsed["original"] == "abc сделай короче"
    ok("текст правки не теряет первое слово, даже если оно похоже на номер")


def test_draft_lookup():
    """Поиск карточки по началу номера и защита от неоднозначности."""
    first = store.create(
        cls="BUG", confidence="высокая", src_channel="C9", src_ts="5.1",
        src_thread_ts=None, src_author="a", src_excerpt="первый",
        reply_text="r", jira_summary="",
    )
    assert store.by_id_prefix(first)["id"] == first
    assert store.by_id_prefix(first[:2])["id"] == first
    assert store.by_id_prefix(first.upper())["id"] == first
    assert store.by_id_prefix("zzzz") is None
    assert len(store.awaiting()) == 1
    ok("поиск по префиксу и регистру, список ждущих")

    store.resolve(first, "dropped")
    assert store.awaiting() == []
    assert store.by_id_prefix(first) is None, "закрытая карточка не должна находиться"
    ok("закрытая карточка выпадает из поиска")


def test_expiry():
    """Просроченные карточки отменяются, свежие — нет."""
    import time as _time
    fresh = store.create(
        cls="BUG", confidence="высокая", src_channel="C9", src_ts="6.1",
        src_thread_ts=None, src_author="a", src_excerpt="свежий",
        reply_text="r", jira_summary="",
    )
    stale = store.create(
        cls="BUG", confidence="высокая", src_channel="C9", src_ts="6.2",
        src_thread_ts=None, src_author="a", src_excerpt="протухший",
        reply_text="r", jira_summary="",
    )
    with store._connect() as conn:
        conn.execute("UPDATE drafts SET created_at = ? WHERE id = ?",
                     (_time.time() - 40 * 3600, stale))

    expired = store.expire_older_than(24)
    assert [row["id"] for row in expired] == [stale], expired
    assert store.by_id(stale)["status"] == "expired"
    assert store.by_id(fresh)["status"] == "awaiting"
    ok("протухшая отменена, свежая не тронута")


def test_verdict_parsing():
    """Разбор ответа модели, когда схему гарантировать нечем.

    На бэкенде `cli` формат — это просьба в промпте, а не контракт API:
    модель может обернуть JSON в ```json, приписать «Вот результат» или
    вернуть класс строчными. Каждый такой случай, не разобранный здесь,
    означает молча потерянное сообщение канала.
    """
    canonical = classifier.parse_verdict(
        '{"cls":"BUG","confidence":"высокая","reason":"симптом",'
        '"reply":"Похоже на баг.","jira_summary":"Профиль: не листается"}'
    )
    assert canonical["cls"] == "BUG"
    assert canonical["reply"] == "Похоже на баг."

    fenced = classifier.parse_verdict(
        'Вот результат:\n```json\n{"cls":"noise","confidence":"ВЫСОКАЯ",'
        '"reason":"трёп","reply":"","jira_summary":""}\n```\nГотово.'
    )
    assert fenced["cls"] == "NOISE", "класс строчными не опознан"
    assert fenced["confidence"] == "высокая"
    ok("обёртки, болтовня вокруг JSON и регистр класса разбираются")

    loose = classifier.parse_verdict(
        'Разобрал так: {"cls":"QUESTION","confidence":"не знаю","reason":"?",'
        '"reply":"Уточню у Глеба.","jira_summary":""} — как-то так.'
    )
    assert loose["confidence"] == "средняя", "неизвестная уверенность должна падать в среднюю"
    ok("неизвестная уверенность не теряет весь разбор")

    for broken in ("совсем не json", '{"cls":"ЧТО-ТО","confidence":"высокая"}'):
        try:
            classifier.parse_verdict(broken)
            raise AssertionError(f"мусор {broken!r} прошёл как вердикт")
        except (ValueError, json.JSONDecodeError):
            pass
    ok("мусор и неизвестный класс отвергаются, а не превращаются в карточку")


def test_backend_choice():
    """Чем платим — выбирается само, но приказ важнее догадки."""
    saved = (classifier.BACKEND, os.environ.get("ANTHROPIC_API_KEY"))
    try:
        classifier.BACKEND = ""
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-something"
        assert classifier.backend() == "api"
        del os.environ["ANTHROPIC_API_KEY"]
        assert classifier.backend() == "cli", "без ключа разбор должен идти по подписке"
        classifier.BACKEND = "api"
        assert classifier.backend() == "api", "явно заданный бэкенд должен побеждать"
    finally:
        classifier.BACKEND = saved[0]
        if saved[1] is not None:
            os.environ["ANTHROPIC_API_KEY"] = saved[1]
    ok("бэкенд выбирается по наличию ключа и переопределяется вручную")


def test_rate_cap():
    """Потолок в час защищает квоту, а не данные — но защищает жёстко."""
    saved, calls = classifier.MAX_PER_HOUR, list(classifier._calls)
    try:
        classifier.MAX_PER_HOUR = 2
        classifier._calls.clear()
        classifier._check_rate()
        classifier._check_rate()
        try:
            classifier._check_rate()
            raise AssertionError("третий разбор прошёл мимо потолка")
        except classifier.RateLimited:
            pass
    finally:
        classifier.MAX_PER_HOUR = saved
        classifier._calls[:] = calls
    ok("потолок разборов в час срабатывает")


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
        ("разбор вердикта модели", test_verdict_parsing),
        ("выбор бэкенда", test_backend_choice),
        ("потолок разборов в час", test_rate_cap),
        ("разбор решений владельца", test_decisions),
        ("поиск карточки", test_draft_lookup),
        ("отмена просроченных", test_expiry),
        ("дешёвый фильтр", test_prefilter),
        ("карточка черновика", test_card),
    ]:
        print(name)
        fn()

    # Сквозной прогон идёт последним и в отдельном файле: он поднимает
    # заглушки Slack и модели и работает на своей базе, а здесь проверяются
    # кирпичи по отдельности. Одна команда на оба набора — чтобы «зелёно»
    # означало «зелёно везде», а не «в той половине, которую я вспомнил».
    import test_flow  # noqa: E402

    test_flow.run()
    print("\nвсё зелёное")
