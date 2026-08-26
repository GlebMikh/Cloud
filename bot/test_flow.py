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
    # Пауза перед ответом здесь выключена: проверки, которым она нужна,
    # включают её сами. Иначе каждая из остальных ждала бы две минуты
    # или, что хуже, молча проверяла бы не то.
    REPLY_DELAY_SECONDS="0",
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


def test_test_channel():
    """В песочнице разбираются и собственные сообщения владельца.

    Без этого бота нельзя проверить в одиночку: в рабочих каналах он
    сознательно пропускает всё, что пишет владелец, и молчание выглядит
    как поломка, хотя это ровно задуманное поведение.
    """
    sandbox = "C0BSMS2HK1R"
    saved_test, saved_watch = app.TEST_CHANNELS, app.WATCH
    app.TEST_CHANNELS = {sandbox}
    app.WATCH = set(app.WATCH) | {sandbox}
    try:
        clear_pending()
        fake.reset()
        own = {"channel": CHANNEL, "ts": "1600.000100", "user": OWNER,
               "text": "Профиль: не листается список эмблем и рамок"}
        app.process_channel_message(own)
        assert not fake.posted, "в рабочем канале сообщение владельца разбирать не должны"
        ok("в рабочем канале собственные сообщения владельца по-прежнему игнорируются")

        app.process_channel_message({**own, "channel": sandbox, "ts": "1601.000100"})
        assert fake.posted, "в тестовом канале сообщение владельца осталось без разбора"
        ok("в тестовом канале бот разбирает сообщение владельца и отвечает")
    finally:
        app.TEST_CHANNELS, app.WATCH = saved_test, saved_watch
        clear_pending()


def test_held_when_colleague_replied():
    """Коллега уже откликнулся — бот не лезет в тред, а докладывает владельцу.

    Третий голос в треде, где работа началась, не помогает никому: автор
    получил реакцию, разработчик занят. А владельцу знать полезно — и
    решать, нужен ли он там, будет он сам.
    """
    saved = autopost_mode()
    try:
        clear_pending()
        fake.reset()
        # Баг-репорт, на который уже ответил разработчик.
        fake.thread_replies[(CHANNEL, "1700.000100")] = [
            {"user": "U0DIMA", "text": "Проверю"},
        ]
        app.process_channel_message(
            {**incoming("1700.000100"), "thread_ts": "1700.000100"}
        )

        assert not fake.to_channel(), "бот влез в тред, где уже отвечают"
        notice = fake.to_inbox()
        assert len(notice) == 1, fake.posted
        assert "Не влез в тред" in notice[0][2]
        assert "В треде уже ответили" in notice[0][2], "не показано, кто откликнулся"
        assert "всё же ответить" in notice[0][2], "нет способа передумать"
        ok("при чужом ответе бот пишет только в личку и показывает, кто взялся")

        # Модель должна была писать текст для владельца, а не для канала.
        assert CALLS[-1]["audience"] == "owner", CALLS[-1]
        ok("модель предупреждена, что ответ увидит только владелец")
    finally:
        fake.thread_replies.clear()
        restore_mode(saved)


def test_held_answer_anyway():
    """✅ на такой сводке всё же отправляет ответ в тред — с новым текстом."""
    saved = autopost_mode()
    try:
        clear_pending()
        fake.reset()
        fake.thread_replies[(CHANNEL, "1800.000100")] = [
            {"user": "U0DIMA", "text": "Проверю"},
        ]
        app.process_channel_message(
            {**incoming("1800.000100"), "thread_ts": "1800.000100"}
        )
        with store._connect() as conn:
            row = conn.execute(
                "SELECT * FROM drafts WHERE status = 'held' ORDER BY created_at DESC"
            ).fetchone()
        assert row is not None, "черновик не сохранён как придержанный"
        fake.reset()

        app.on_reaction({
            "user": OWNER, "reaction": "white_check_mark",
            "item": {"channel": row["card_channel"], "ts": row["card_ts"]},
        })
        published = fake.to_channel()
        assert len(published) == 1, fake.posted
        assert published[0][1] == "1800.000100", "ответ ушёл не в тот тред"
        assert CALLS[-1]["audience"] == "channel", "текст для канала не перегенерирован"
        assert store.by_id(row["id"])["status"] == "posted"
        ok("✅ на придержанной сводке отправляет ответ в тред заново собранным текстом")
    finally:
        fake.thread_replies.clear()
        restore_mode(saved)


def test_direct_tag_still_answers_in_thread():
    """Прямой тег владельца — исключение: там реакция в треде уместна."""
    saved = autopost_mode()
    try:
        clear_pending()
        fake.reset()
        fake.thread_replies[(CHANNEL, "1900.000100")] = [
            {"user": "U0DIMA", "text": "щас гляну"},
        ]
        app.process_channel_message({
            "channel": CHANNEL, "ts": "1900.000100", "thread_ts": "1900.000100",
            "user": DEV, "text": f"<@{OWNER}> глянь плиз, тут платежи отваливаются",
        })
        assert fake.to_channel(), "на прямой тег бот промолчал в треде"
        ok("на прямой тег владельца бот отвечает в треде даже при чужих ответах")
    finally:
        fake.thread_replies.clear()
        restore_mode(saved)


def delayed_mode(seconds=300):
    """Включить паузу перед ответом, но так, чтобы таймер не выстрелил сам."""
    saved = app.REPLY_DELAY
    app.REPLY_DELAY = seconds
    return saved


def stop_timers():
    for timer in list(app._timers.values()):
        timer.cancel()
    app._timers.clear()


def only_delayed():
    with store._connect() as conn:
        return conn.execute(
            "SELECT * FROM drafts WHERE status = 'delayed' ORDER BY created_at DESC"
        ).fetchone()


def test_delay_holds_reply():
    """Ответ не уходит сразу — он ждёт своей минуты."""
    saved_mode, saved_delay = autopost_mode(), delayed_mode()
    try:
        clear_pending()
        fake.reset()
        app.process_channel_message(incoming("2000.000100"))

        assert not fake.posted, "ответ ушёл, не дождавшись паузы"
        row = only_delayed()
        assert row is not None and row["reply_text"], "черновик не сохранён как отложенный"
        assert row["id"] in app._timers, "таймер на отправку не заведён"
        ok("ответ отложен: в канал ничего, черновик ждёт в базе")
    finally:
        stop_timers()
        restore_mode(saved_mode)
        app.REPLY_DELAY = saved_delay


def test_delay_delivers_when_quiet():
    """Никто не откликнулся за паузу — ответ уходит как задумано."""
    saved_mode, saved_delay = autopost_mode(), delayed_mode()
    try:
        clear_pending()
        fake.reset()
        app.process_channel_message(incoming("2100.000100"))
        row = only_delayed()
        stop_timers()
        fake.reset()

        app.deliver(row["id"])
        assert len(fake.to_channel()) == 1, fake.posted
        assert fake.to_inbox(), "сводка владельцу не пришла"
        assert store.by_id(row["id"])["status"] == "posted"
        ok("после паузы в тишине ответ уходит в тред и приходит сводка")
    finally:
        stop_timers()
        restore_mode(saved_mode)
        app.REPLY_DELAY = saved_delay


def test_delay_yields_to_human():
    """Пока бот ждал, откликнулся живой — в канал уже не лезем.

    Ровно та ситуация, ради которой пауза и заведена: разработчик пишет
    «проверю» через минуту после баг-репорта, и реплика бота следом
    выглядит как разговор с самим собой.
    """
    saved_mode, saved_delay = autopost_mode(), delayed_mode()
    try:
        clear_pending()
        fake.reset()
        app.process_channel_message(incoming("2200.000100"))
        row = only_delayed()
        stop_timers()

        # За время паузы в треде появился человек.
        fake.thread_replies[(CHANNEL, "2200.000100")] = [
            {"user": "U0DIMA", "text": "Проверю"},
        ]
        fake.reset()
        app.deliver(row["id"])

        assert not fake.to_channel(), "бот всё же влез в тред после чужого ответа"
        notice = fake.to_inbox()
        assert notice and "Не влез в тред" in notice[0][2], notice
        assert CALLS[-1]["audience"] == "owner", "текст не переписан для владельца"
        assert store.by_id(row["id"])["status"] == "held"
        ok("живой ответ за время паузы отменяет реплику в канал")
    finally:
        fake.thread_replies.clear()
        stop_timers()
        restore_mode(saved_mode)
        app.REPLY_DELAY = saved_delay


def test_delay_owner_answered():
    """Владелец ответил сам — бот исчезает молча, без сводки."""
    saved_mode, saved_delay = autopost_mode(), delayed_mode()
    try:
        clear_pending()
        fake.reset()
        app.process_channel_message(incoming("2300.000100"))
        row = only_delayed()
        stop_timers()

        fake.thread_replies[(CHANNEL, "2300.000100")] = [
            {"user": OWNER, "text": "уже смотрим"},
        ]
        fake.reset()
        app.deliver(row["id"])

        assert not fake.posted, "бот что-то сказал, хотя владелец ответил сам"
        assert store.by_id(row["id"])["status"] == "dropped"
        ok("если владелец ответил сам, отложенный ответ тихо снимается")
    finally:
        fake.thread_replies.clear()
        stop_timers()
        restore_mode(saved_mode)
        app.REPLY_DELAY = saved_delay


def test_delay_survives_restart():
    """Отложенный ответ не должен пропасть вместе с процессом.

    Таймер жил в памяти; после перезапуска его нет. Без разбора при старте
    ответ не ушёл бы ни в канал, ни в личку — просто исчез бы.
    """
    saved_mode, saved_delay = autopost_mode(), delayed_mode()
    try:
        clear_pending()
        fake.reset()
        app.process_channel_message(incoming("2400.000100"))
        row = only_delayed()
        stop_timers()          # как будто процесс погасили
        fake.reset()

        app.recover_delayed()
        assert store.by_id(row["id"])["status"] in ("posted", "held"), "ответ завис навсегда"
        assert fake.posted, "после перезапуска ничего не произошло"
        ok("отложенный ответ разбирается при следующем старте, а не теряется")
    finally:
        stop_timers()
        restore_mode(saved_mode)
        app.REPLY_DELAY = saved_delay


def last_decision():
    return store.decisions(1)[0]


def test_decision_journal():
    """Молчание должно быть объяснимым.

    Бот молчит по доброму десятку причин, и снаружи все они выглядят
    одинаково. Пока причина не записана, единственный доступный ответ на
    «почему он не ответил?» — пожать плечами.
    """
    clear_pending()
    fake.reset()
    app.classifier.classify = lambda **kw: {
        "cls": "DISCUSSION", "confidence": "высокая",
        "reason": "трёп про пятницу", "reply": "", "jira_summary": "",
    }
    app.process_channel_message(incoming("2500.000100", "ну и пятница выдалась, конечно"))
    app.classifier.classify = fake_classify

    row = last_decision()
    assert row["cls"] == "DISCUSSION", dict(row)
    assert row["outcome"] == "класс не требует ответа", dict(row)
    assert "пятницу" in (row["reason"] or "")
    ok("отказ отвечать записан вместе с классом и причиной")


def test_empty_reply_retried():
    """Класс требует ответа, а ответа нет — это промах модели, не решение.

    Первая версия молча пропускала такое: сообщение помечалось разобранным,
    и баг-репорт исчезал без следа. Одна повторная попытка дешевле.
    """
    clear_pending()
    fake.reset()
    attempts = []

    def flaky(**kw):
        attempts.append(kw)
        if len(attempts) == 1:
            return dict(VERDICT, reply="")      # промах
        return dict(VERDICT)                    # со второй попытки текст есть

    app.classifier.classify = flaky
    app.process_channel_message(incoming("2600.000100"))
    app.classifier.classify = fake_classify

    assert len(attempts) == 2, f"повторной попытки не было: {len(attempts)}"
    assert fake.to_inbox(), "после удачной второй попытки владелец ничего не получил"
    ok("пустой ответ переспрашивается, а не теряется молча")

    # А если и вторая пустая — это записывается, а не исчезает.
    fake.reset()
    app.classifier.classify = lambda **kw: dict(VERDICT, reply="")
    app.process_channel_message(incoming("2700.000100"))
    app.classifier.classify = fake_classify
    assert not fake.posted
    assert last_decision()["outcome"].startswith("модель не дала текста"), last_decision()["outcome"]
    ok("две пустые попытки подряд оставляют запись в журнале")


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
    ("тестовый канал", test_test_channel),
    ("в треде уже ответили", test_held_when_colleague_replied),
    ("всё же ответить в тред", test_held_answer_anyway),
    ("прямой тег — исключение", test_direct_tag_still_answers_in_thread),
    ("пауза перед ответом", test_delay_holds_reply),
    ("после паузы в тишине", test_delay_delivers_when_quiet),
    ("живой успел раньше", test_delay_yields_to_human),
    ("владелец ответил сам", test_delay_owner_answered),
    ("пауза переживает перезапуск", test_delay_survives_restart),
    ("журнал решений", test_decision_journal),
    ("пустой ответ модели", test_empty_reply_retried),
]


def run() -> None:
    for name, fn in TESTS:
        print(name)
        fn()


if __name__ == "__main__":
    run()
    print("\nвсё зелёное")
