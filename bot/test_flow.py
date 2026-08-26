"""Сквозной прогон цикла бота — без Slack, без сети и без вызовов модели.

Дымовой тест рядом проверяет кирпичи: фильтр, хранилище, отрисовку карточки.
Здесь проверяется то, ради чего они собраны вместе, — путь сообщения от
канала до треда: карточка ушла владельцу, метка встала, «ок» опубликовал
ответ, «нет» не опубликовал ничего.

Ошибка на этом пути стоит дороже всех остальных: она видна не в логе, а
в рабочем канале, чужими глазами и от имени владельца. Поэтому Slack и
Claude заменены заглушками, и весь путь гоняется целиком за доли секунды.

Запуск:  python bot/test_flow.py   (или заодно с дымовым: python bot/test_smoke.py)
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

OWNER, BOT, DEV = "U0BK99FCH7F", "U0BOTBOT", "U0DEVDEV"
CHANNEL, INBOX = "C099240QR3N", "D0INBOX"

# Окружение должно стоять до импорта app: модуль разбирает его при импорте.
# Задаётся ВСЁ, на что тесты опираются, включая пустые значения: app.py
# читает .env из корня репозитория, и без явных значений проверки начинают
# зависеть от того, что владелец включил у себя на машине.
os.environ.update(
    SLACK_BOT_TOKEN="xoxb-test",
    SLACK_APP_TOKEN="xapp-test",
    SLACK_SIGNING_SECRET="secret",
    OWNER_SLACK_ID=OWNER,
    WATCH_CHANNELS=CHANNEL,
    DIGEST_CHANNELS="",
    MARKER_EMOJI="robot_face",
    AUTO_REPLY_ON_MENTION="false",
    AUTOPOST_CLASSES="",
    AUTOPOST_MIN_CONFIDENCE="средняя",
)
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-placeholder")

import store  # noqa: E402

# Своя база, а не общая с дымовым тестом: проверки ниже считают ждущие
# карточки, и чужой недорешённый черновик ломал бы их через раз.
store.DB_PATH = os.path.join(tempfile.mkdtemp(), "flow.sqlite3")


class FakeClient:
    """Slack, какой он нужен боту: два десятка методов из сотен.

    Возвращает ровно те формы ответов, которые читает код, — включая ту,
    из-за которой легко ошибиться: карточка отправляется по user_id
    владельца, а Slack кладёт её в канал с собственным id (`D…`), и решения
    приходят потом именно оттуда.
    """

    def __init__(self):
        self.posted = []       # (channel, thread_ts, text)
        self.reactions = []    # (channel, ts, name)
        self.thread_replies = {}
        self.deleted = []
        self.updated = []
        self._ts = 1000

    def _next_ts(self) -> str:
        self._ts += 1
        return f"{self._ts}.000100"

    def auth_test(self):
        return {"user_id": BOT}

    def conversations_info(self, channel):
        return {"channel": {"name": "разработка-обсуждение"}}

    def users_info(self, user):
        return {"user": {"real_name": {OWNER: "Глеб", DEV: "Андрей Сергиенко"}.get(user, user)}}

    def chat_getPermalink(self, channel, message_ts):
        return {"permalink": f"https://slack.test/{channel}/p{message_ts}"}

    def conversations_replies(self, channel, ts, limit=30):
        # Первым элементом Slack всегда отдаёт само родительское сообщение —
        # код срезает его, и заглушка обязана вести себя так же.
        return {"messages": [{"ts": ts, "user": DEV}] + self.thread_replies.get((channel, ts), [])}

    def chat_postMessage(self, channel, text, thread_ts=None):
        landed = INBOX if channel == OWNER else channel
        self.posted.append((landed, thread_ts, text))
        return {"channel": landed, "ts": self._next_ts()}

    def chat_delete(self, channel, timestamp):
        self.deleted.append((channel, timestamp))
        return {"ok": True}

    def chat_update(self, channel, ts, text):
        self.updated.append((channel, ts, text))
        return {"ok": True}

    def reactions_add(self, channel, timestamp, name):
        self.reactions.append((channel, timestamp, name))
        return {"ok": True}

    # -- удобства для проверок ---------------------------------------------

    def to_inbox(self):
        return [p for p in self.posted if p[0] == INBOX]

    def to_channel(self):
        return [p for p in self.posted if p[0] == CHANNEL]

    def reset(self):
        self.posted.clear()
        self.reactions.clear()
        self.deleted.clear()
        self.updated.clear()


class FakeApp:
    """Заглушка slack_bolt.App: декораторы событий и клиент."""

    def __init__(self, **kwargs):
        self.client = FakeClient()

    def event(self, _name):
        return lambda fn: fn


import slack_bolt  # noqa: E402

slack_bolt.App = FakeApp

import app  # noqa: E402

fake = app.client = app.app.client
store.init()


# --------------------------------------------------------------------------
# заглушка классификатора
# --------------------------------------------------------------------------

CALLS = []
VERDICT = {
    "cls": "BUG",
    "confidence": "высокая",
    "reason": "описан симптом",
    "reply": "Похоже на баг, передаю Глебу.",
    "jira_summary": "Профиль: не листается список эмблем",
}


def fake_classify(**kwargs):
    CALLS.append(kwargs)
    return dict(VERDICT)


app.classifier.classify = fake_classify


def ok(message: str) -> None:
    print("  ok  ", message)


def incoming(ts: str, text: str = "Профиль: не листается список эмблем и рамок") -> dict:
    return {"channel": CHANNEL, "ts": ts, "user": DEV, "text": text}


def last_draft():
    pending = store.awaiting()
    assert pending, "ни одной ждущей карточки"
    return pending[0]


# --------------------------------------------------------------------------
# проверки
# --------------------------------------------------------------------------

def test_message_to_card():
    fake.reset()
    app.process_channel_message(incoming("100.000100"))

    cards = fake.to_inbox()
    assert len(cards) == 1, fake.posted
    assert not fake.to_channel(), "в канал ничего уходить не должно до одобрения"

    draft = last_draft()
    assert draft["id"] in cards[0][2], "в карточке нет идентификатора черновика"
    assert "Похоже на баг" in cards[0][2]
    ok("сообщение канала превратилось в карточку в личке, в канал не ушло")

    assert (CHANNEL, "100.000100", "robot_face") in fake.reactions
    ok("на исходном сообщении стоит метка «разобрано»")

    assert draft["card_channel"] == INBOX, (
        "карточка запомнена по user_id, а решения придут из D-канала"
    )
    ok("карточка запомнена по каналу из ответа Slack, а не по user_id")


def test_duplicate_delivery():
    fake.reset()
    before = len(CALLS)
    app.process_channel_message(incoming("100.000100"))
    assert not fake.posted, "повторная доставка события удвоила карточку"
    assert len(CALLS) == before, "повторная доставка сходила в модель второй раз"
    ok("повторная доставка того же события не делает ни карточки, ни вызова модели")


def test_chatter_is_remembered():
    """Болтовня стоит денег ровно один раз.

    Метка-реакция ставится только на то, из чего вышел черновик. Если
    разобранную и отвергнутую болтовню не помнить отдельно, добор истории
    при каждом старте будет гонять через модель одни и те же сутки заново —
    молча и за деньги.
    """
    fake.reset()
    app.classifier.classify = lambda **kw: (
        CALLS.append(kw) or {"cls": "DISCUSSION", "confidence": "высокая",
                             "reason": "трёп", "reply": "", "jira_summary": ""}
    )
    app.process_channel_message(incoming("200.000100", "да ладно, у нас так везде сделано"))
    assert not fake.posted, "болтовня дошла до владельца"

    before = len(CALLS)
    app.process_channel_message(incoming("200.000100", "да ладно, у нас так везде сделано"))
    assert len(CALLS) == before, "разобранная болтовня пошла в модель по второму кругу"
    app.classifier.classify = fake_classify
    ok("разобранная болтовня не носится владельцу и не переоплачивается после рестарта")


def test_approve_by_word():
    fake.reset()
    draft = last_draft()
    app.handle_inbox({"user": OWNER, "channel": INBOX, "ts": "900.1", "text": "ок"})

    published = fake.to_channel()
    assert len(published) == 1, fake.posted
    channel, thread_ts, text = published[0]
    assert thread_ts == "100.000100", "ответ ушёл не в тред исходного сообщения"
    assert text.startswith(app.PREFIX), "ответ ушёл без пометки, что писал бот"
    assert "Похоже на баг" in text
    assert store.by_id(draft["id"])["status"] == "posted"
    ok("«ок» в личке публикует ответ в исходный тред с префиксом бота")


def test_reject():
    fake.reset()
    app.process_channel_message(incoming("300.000100"))
    draft = last_draft()
    fake.reset()

    app.handle_inbox({"user": OWNER, "channel": INBOX, "ts": "901.1", "text": "нет"})
    assert not fake.to_channel(), "отменённый черновик всё равно ушёл в канал"
    assert store.by_id(draft["id"])["status"] == "dropped"
    ok("«нет» закрывает черновик и ничего не постит")


def test_approve_by_reaction():
    fake.reset()
    app.process_channel_message(incoming("400.000100"))
    draft = last_draft()
    card_channel, card_ts = draft["card_channel"], draft["card_ts"]
    fake.reset()

    app.on_reaction({
        "user": OWNER,
        "reaction": "white_check_mark",
        "item": {"channel": card_channel, "ts": card_ts},
    })
    assert len(fake.to_channel()) == 1, fake.posted
    assert store.by_id(draft["id"])["status"] == "posted"
    ok("галочка на карточке публикует ответ")


def test_ticket_without_jira():
    """Ненастроенная Jira не должна съедать ответ.

    Реакция 🎫 — это два действия сразу, и молчание вместо ответа в треде
    было бы худшим исходом: тикета нет, ответа тоже, а владелец уверен,
    что сделано и то и другое.
    """
    fake.reset()
    app.process_channel_message(incoming("500.000100"))
    draft = last_draft()
    fake.reset()

    app.on_reaction({
        "user": OWNER,
        "reaction": "ticket",
        "item": {"channel": draft["card_channel"], "ts": draft["card_ts"]},
    })
    published = fake.to_channel()
    assert len(published) == 1, "ответ не ушёл из-за ненастроенной Jira"
    assert "Jira не настроена" in published[0][2]
    ok("🎫 без настроенной Jira всё равно публикует ответ и честно говорит про тикет")


def test_owner_already_replied():
    fake.reset()
    app.process_channel_message(incoming("600.000100"))
    draft = last_draft()
    fake.reset()

    # Пока карточка ждала решения, владелец ответил в треде сам.
    fake.thread_replies[(CHANNEL, "600.000100")] = [{"user": OWNER, "text": "уже разобрались"}]
    app.handle_inbox({"user": OWNER, "channel": INBOX, "ts": "902.1", "text": "ок"})

    assert not fake.to_channel(), "бот ответил следом за владельцем в тот же тред"
    assert store.by_id(draft["id"])["status"] == "dropped"
    assert "уже ответил" in fake.to_inbox()[0][2]
    ok("если владелец ответил сам, бот не лезет следом и говорит об этом")


def test_revision():
    fake.reset()
    app.process_channel_message(incoming("700.000100"))
    draft = last_draft()
    old_card_ts = draft["card_ts"]
    fake.reset()

    app.classifier.classify = lambda **kw: dict(VERDICT, reply="Баг, передаю Глебу.")
    app.handle_inbox({"user": OWNER, "channel": INBOX, "ts": "903.1", "text": "сделай короче"})
    app.classifier.classify = fake_classify

    assert not fake.to_channel(), "правка не должна ничего публиковать"
    inbox = fake.to_inbox()
    assert any("редакция 2" in text for _, _, text in inbox), "новая карточка не помечена как правка"
    assert any(thread_ts == old_card_ts for _, thread_ts, _ in inbox), (
        "старая карточка осталась без приписки — реакции на ней молча не сработают"
    )
    assert store.by_id(draft["id"])["reply_text"] == "Баг, передаю Глебу."
    ok("правка присылает новую карточку и помечает старую")


def test_ambiguous_decision():
    """Две ждущие карточки и голое «ок» — угадывать нельзя.

    Ценой ошибки будет чужой ответ в рабочем канале от имени владельца,
    поэтому единственный правильный исход — переспросить.
    """
    for row in store.awaiting():
        store.resolve(row["id"], "dropped")

    fake.reset()
    app.process_channel_message(incoming("800.000100"))
    app.process_channel_message(incoming("801.000100", "Второй баг: не грузится аватар в профиле"))
    assert len(store.awaiting()) == 2
    fake.reset()

    app.handle_inbox({"user": OWNER, "channel": INBOX, "ts": "904.1", "text": "ок"})
    assert not fake.to_channel(), "бот угадал адресата и запостил наугад"
    assert "не понял, про какую речь" in fake.to_inbox()[0][2]
    ok("при нескольких ждущих карточках бот переспрашивает, а не угадывает")

    # А с номером — исполняет без вопросов.
    target = store.awaiting()[0]
    fake.reset()
    app.handle_inbox({"user": OWNER, "channel": INBOX, "ts": "905.1", "text": f"{target['id']} ок"})
    assert len(fake.to_channel()) == 1, fake.posted
    assert store.by_id(target["id"])["status"] == "posted"
    ok("номер перед словом снимает двусмысленность")


def test_ignores_others_in_inbox():
    fake.reset()
    app.handle_inbox({"user": DEV, "channel": INBOX, "ts": "906.1", "text": "ок"})
    app.handle_inbox({"user": OWNER, "channel": INBOX, "ts": "907.1",
                      "subtype": "message_changed", "text": "ок"})
    assert not fake.posted, "чужое или отредактированное сообщение принято за решение"
    ok("решения принимаются только от владельца и только от живых сообщений")


def clear_pending():
    """Закрыть всё, что осталось ждать от предыдущих проверок.

    Проверки автоответа считают ждущие карточки, а предыдущие сценарии
    оставляют свои — без уборки тест ловил бы чужой хвост.
    """
    for row in store.awaiting():
        store.resolve(row["id"], "dropped")
    # И отправленные автоответы тоже: «нет» без номера относится к
    # последнему, и чужой недавний ответ сделал бы адресата неоднозначным.
    for row in store.recently_posted():
        store.resolve(row["id"], "dropped")


def autopost_mode(classes="MENTION,BUG,TASK,QUESTION", minimum="средняя"):
    """Включить автоответ на время одной проверки."""
    saved = (app.AUTOPOST_CLASSES, app.AUTOPOST_MIN_CONFIDENCE)
    app.AUTOPOST_CLASSES = {c.strip() for c in classes.split(",") if c.strip()}
    app.AUTOPOST_MIN_CONFIDENCE = minimum
    return saved


def restore_mode(saved):
    app.AUTOPOST_CLASSES, app.AUTOPOST_MIN_CONFIDENCE = saved


def test_autopost():
    """Режим автоответа: сначала в тред, потом сводка владельцу."""
    saved = autopost_mode()
    try:
        clear_pending()
        fake.reset()
        app.process_channel_message(incoming("1000.000100"))

        published = fake.to_channel()
        assert len(published) == 1, fake.posted
        assert published[0][1] == "1000.000100", "ответ ушёл не в тред исходного сообщения"
        assert published[0][2].startswith(app.PREFIX)
        ok("ответ ушёл в канал сразу, без одобрения")

        notice = fake.to_inbox()
        assert len(notice) == 1, fake.posted
        assert "Ответил сам" in notice[0][2]
        assert "Черновик тикета" in notice[0][2], "в сводке нет заготовки задачи"
        assert "удалить мой ответ" in notice[0][2], "в сводке не сказано, как отменить"
        ok("в личку пришла сводка с черновиком тикета и способом отменить")

        assert not store.awaiting(), "автоответ не должен оставлять карточку в ожидании"
        posted = store.recently_posted()
        assert len(posted) == 1 and posted[0]["posted_ts"], "координаты ответа не сохранены"
        ok("черновик закрыт как отправленный, координаты ответа сохранены")
    finally:
        restore_mode(saved)


def test_autopost_undo_by_reaction():
    """❌ на сводке убирает сказанное из треда — это главная страховка режима."""
    saved = autopost_mode()
    try:
        clear_pending()
        fake.reset()
        app.process_channel_message(incoming("1100.000100"))
        draft = store.recently_posted()[0]
        fake.reset()

        app.on_reaction({
            "user": OWNER, "reaction": "x",
            "item": {"channel": draft["card_channel"], "ts": draft["card_ts"]},
        })
        assert fake.deleted == [(CHANNEL, draft["posted_ts"])], fake.deleted
        assert store.by_id(draft["id"])["status"] == "undone"
        assert "Удалил свой ответ" in fake.to_inbox()[0][2]
        ok("реакция ❌ удаляет ответ из треда и подтверждает это")
    finally:
        restore_mode(saved)


def test_autopost_undo_by_word():
    """«Нет» словом в личке относится к последнему автоответу."""
    saved = autopost_mode()
    try:
        clear_pending()
        fake.reset()
        app.process_channel_message(incoming("1200.000100"))
        draft = store.recently_posted()[0]
        fake.reset()

        app.handle_inbox({"user": OWNER, "channel": INBOX, "ts": "990.1", "text": "нет"})
        assert fake.deleted == [(CHANNEL, draft["posted_ts"])], fake.deleted
        assert store.by_id(draft["id"])["status"] == "undone"
        ok("«нет» без номера отменяет последний автоответ")

        # А если недавних автоответов несколько — угадывать нельзя: удалить
        # не тот ответ хуже, чем переспросить.
        fake.reset()
        app.process_channel_message(incoming("1210.000100"))
        app.process_channel_message(incoming("1220.000100", "Второй баг: не грузится аватар"))
        fake.reset()
        app.handle_inbox({"user": OWNER, "channel": INBOX, "ts": "990.2", "text": "нет"})
        assert not fake.deleted, "при двух свежих автоответах бот удалил наугад"
        assert "не понял, про какую речь" in fake.to_inbox()[0][2]
        ok("при нескольких свежих автоответах бот переспрашивает, а не удаляет наугад")
    finally:
        restore_mode(saved)


def test_autopost_rewrite():
    """Правка меняет уже отправленное сообщение, а не досылает второе."""
    saved = autopost_mode()
    try:
        clear_pending()
        fake.reset()
        app.process_channel_message(incoming("1300.000100"))
        draft = store.recently_posted()[0]
        fake.reset()

        app.classifier.classify = lambda **kw: dict(VERDICT, reply="Баг, передаю Глебу.")
        app.handle_inbox({"user": OWNER, "channel": INBOX, "ts": "991.1",
                          "text": "покороче и без «похоже»"})
        app.classifier.classify = fake_classify

        assert not fake.to_channel(), "правка досылает второе сообщение вместо замены"
        assert fake.updated and fake.updated[0][1] == draft["posted_ts"], fake.updated
        assert "Баг, передаю Глебу." in fake.updated[0][2]
        assert store.by_id(draft["id"])["reply_text"] == "Баг, передаю Глебу."
        ok("правка редактирует сообщение прямо в треде")
    finally:
        restore_mode(saved)


def test_autopost_ticket():
    """🎫 на сводке заводит тикет по уже отправленному ответу."""
    saved = autopost_mode()
    try:
        clear_pending()
        fake.reset()
        app.process_channel_message(incoming("1400.000100"))
        draft = store.recently_posted()[0]
        fake.reset()

        app.on_reaction({
            "user": OWNER, "reaction": "ticket",
            "item": {"channel": draft["card_channel"], "ts": draft["card_ts"]},
        })
        answer = fake.to_inbox()[0][2]
        assert "Jira не настроена" in answer, answer
        assert store.by_id(draft["id"])["status"] == "posted", "ответ не должен отзываться"
        ok("🎫 без настроенной Jira честно сообщает и не трогает отправленное")
    finally:
        restore_mode(saved)


def test_autopost_low_confidence_asks():
    """Сомнительный разбор идёт прежним путём — через одобрение.

    Ошибка автоответа видна не владельцу, а всему каналу, поэтому порог
    уверенности здесь не украшение: он единственный, что отделяет
    «бот иногда отвечает лишнее» от «бот иногда позорит владельца».
    """
    saved = autopost_mode(minimum="высокая")
    try:
        clear_pending()
        fake.reset()
        app.classifier.classify = lambda **kw: dict(VERDICT, confidence="средняя")
        app.process_channel_message(incoming("1500.000100"))
        app.classifier.classify = fake_classify

        assert not fake.to_channel(), "разбор ниже порога ушёл в канал сам"
        assert "Черновик" in fake.to_inbox()[0][2], "не пришла карточка на одобрение"
        assert len(store.awaiting()) == 1
        ok("уверенность ниже порога возвращает обычный режим одобрения")

        for row in store.awaiting():
            store.resolve(row["id"], "dropped")
    finally:
        restore_mode(saved)


TESTS = [
    ("сообщение канала становится карточкой", test_message_to_card),
    ("повторная доставка события", test_duplicate_delivery),
    ("болтовня разбирается один раз", test_chatter_is_remembered),
    ("одобрение словом", test_approve_by_word),
    ("отказ", test_reject),
    ("одобрение реакцией", test_approve_by_reaction),
    ("тикет без настроенной Jira", test_ticket_without_jira),
    ("владелец ответил сам", test_owner_already_replied),
    ("правка черновика", test_revision),
    ("несколько ждущих карточек", test_ambiguous_decision),
    ("чужие сообщения в инбоксе", test_ignores_others_in_inbox),
    ("автоответ без одобрения", test_autopost),
    ("отмена автоответа реакцией", test_autopost_undo_by_reaction),
    ("отмена автоответа словом", test_autopost_undo_by_word),
    ("правка отправленного", test_autopost_rewrite),
    ("тикет по автоответу", test_autopost_ticket),
    ("низкая уверенность спрашивает", test_autopost_low_confidence_asks),
]


def run() -> None:
    for name, fn in TESTS:
        print(name)
        fn()


if __name__ == "__main__":
    run()
    print("\nвсё зелёное")
