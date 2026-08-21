"""Глебот — Slack-бот на Socket Mode.

Слушает каналы разработки, классифицирует сообщения через Claude и приносит
владельцу карточку с готовым ответом. В канал ничего не уходит, пока
владелец не подтвердил — реакцией на карточку или словом в её треде.

Главный выигрыш перед часовым обходом не в скорости реакции на теги, а в
том, что петля одобрения замыкается за секунды: владелец жмёт ✅ — ответ
уже в треде, а не через сорок минут.

Запуск: python bot/app.py (из корня репозитория — там лежит рубрика).
"""

from __future__ import annotations

import functools
import logging
import os
import sys
import threading
import time

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import classifier  # noqa: E402
import daily  # noqa: E402
import filters  # noqa: E402
import jira  # noqa: E402
import store  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("glebot")

OWNER = os.environ["OWNER_SLACK_ID"]
WATCH = {c.strip() for c in os.environ.get("WATCH_CHANNELS", "").split(",") if c.strip()}
DIGEST = {c.strip() for c in os.environ.get("DIGEST_CHANNELS", "").split(",") if c.strip()}
MARKER = os.environ.get("MARKER_EMOJI", "robot_face")
AUTO_REPLY_ON_MENTION = os.environ.get("AUTO_REPLY_ON_MENTION", "false").lower() == "true"
# Сколько часов истории добрать при старте. Slack не переигрывает пропущенные
# события: всё, что произошло, пока процесс лежал, для него не существует.
# Добор при старте — единственное, что закрывает эту дыру.
BACKFILL_HOURS = int(os.environ.get("BACKFILL_HOURS", "24"))
# Время ежедневной сводки, ЧЧ:ММ по локальному времени хоста.
DIGEST_AT = os.environ.get("DIGEST_AT", "09:30")
DRAFT_EXPIRY_HOURS = float(os.environ.get("DRAFT_EXPIRY_HOURS", "24"))
PREFIX = ":robot_face: *Глебот* (AI-помощник Глеба):"

# Не влезать в тред, где разговор уже идёт своим ходом.
SKIP_THREAD_IF_REPLIES_GTE = 3

app = App(
    token=os.environ["SLACK_BOT_TOKEN"],
    signing_secret=os.environ["SLACK_SIGNING_SECRET"],
)


@functools.lru_cache(maxsize=1)
def bot_user_id() -> str:
    """Собственный id бота, чтобы не реагировать на свои же сообщения.

    Запрашивается лениво, а не при импорте: иначе модуль невозможно
    импортировать без живого токена, и любая проверка кода требует Slack.
    """
    return app.client.auth_test()["user_id"]


# --------------------------------------------------------------------------
# вспомогательное
# --------------------------------------------------------------------------

def channel_name(channel_id: str) -> str:
    try:
        return app.client.conversations_info(channel=channel_id)["channel"]["name"]
    except Exception:
        return channel_id


def user_name(user_id: str) -> str:
    try:
        profile = app.client.users_info(user=user_id)["user"]
        return profile.get("real_name") or profile.get("name") or user_id
    except Exception:
        return user_id


def permalink(channel: str, ts: str) -> str:
    try:
        return app.client.chat_getPermalink(channel=channel, message_ts=ts)["permalink"]
    except Exception:
        return ""


def thread_state(channel: str, thread_ts: str | None) -> tuple[int, bool, str]:
    """Сколько в треде ответов, отвечал ли владелец, и краткая выжимка."""
    if not thread_ts:
        return 0, False, ""
    try:
        replies = app.client.conversations_replies(
            channel=channel, ts=thread_ts, limit=30
        )["messages"][1:]
    except Exception:
        return 0, False, ""

    owner_replied = any(m.get("user") == OWNER for m in replies)
    excerpt = "\n".join(
        f"{user_name(m.get('user', '?'))}: {m.get('text', '')[:200]}" for m in replies[-8:]
    )
    return len(replies), owner_replied, excerpt


# --------------------------------------------------------------------------
# карточка черновика
# --------------------------------------------------------------------------

def send_card(draft_id: str, verdict: dict, *, src_channel: str, src_ts: str,
              author: str, excerpt: str, revision: int = 1) -> None:
    text = filters.card_text(
        draft_id,
        verdict,
        channel=channel_name(src_channel),
        author=author,
        excerpt=excerpt,
        link=permalink(src_channel, src_ts),
        jira_project=jira.PROJECT_KEY,
        jira_issue_type=jira.ISSUE_TYPE_BUG,
        revision=revision,
    )
    posted = app.client.chat_postMessage(channel=OWNER, text=text)
    store.attach_card(draft_id, posted["channel"], posted["ts"])


# --------------------------------------------------------------------------
# публикация одобренного
# --------------------------------------------------------------------------

def publish(draft, *, with_ticket: bool) -> str:
    """Запостить ответ в исходный тред. Вернуть строку отчёта для владельца."""
    channel = draft["src_channel"]
    thread_ts = draft["src_thread_ts"] or draft["src_ts"]

    # Тред мог измениться, пока карточка ждала решения: владелец ответил
    # сам, баг починили, вопрос снялся. Публиковать вслепую — худший
    # способ выглядеть автоматом.
    replies, owner_replied, _ = thread_state(channel, thread_ts)
    if owner_replied:
        store.resolve(draft["id"], "dropped")
        return "Не запостил: ты уже ответил в этом треде сам."

    reply = f"{PREFIX} {draft['reply_text']}"
    jira_key = None

    if with_ticket and draft["jira_summary"]:
        if not jira.configured():
            reply += "\n\n_(Jira не настроена — тикет не завёл)_"
        else:
            try:
                link = permalink(channel, draft["src_ts"])
                jira_key = jira.create_bug(
                    draft["jira_summary"],
                    f"Откуда: Slack, #{channel_name(channel)}, {draft['src_author']}\n"
                    f"{link}\n\n"
                    f"Что происходит\n{draft['src_excerpt']}\n\n"
                    f"Заведено Глеботом из обсуждения в Slack, детали — по ссылке выше.",
                )
                reply += f"\n\nЗавёл {jira_key}."
            except Exception as exc:  # тикет не должен ронять публикацию
                log.exception("Jira")
                reply += "\n\n_(тикет завести не вышло, Глеб заведёт руками)_"
                jira_key = f"ошибка: {exc}"

    app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=reply)
    store.resolve(draft["id"], "posted", jira_key)

    report = "Запощено."
    if jira_key and not str(jira_key).startswith("ошибка"):
        report += f" Тикет {jira_key}."
    return report


# --------------------------------------------------------------------------
# события
# --------------------------------------------------------------------------

@app.event("message")
def on_message(event):
    # Личка владельца — это инбокс: там решения по карточкам, не разбор.
    if event.get("channel_type") == "im":
        handle_inbox(event)
        return
    process_channel_message(event)


def process_channel_message(event: dict) -> None:
    """Разобрать одно сообщение канала.

    Отдельно от обработчика события, потому что тот же путь проходят
    сообщения, добранные из истории при старте: событие и запись в истории
    отличаются только источником, а решения по ним должны быть одинаковыми.
    """
    channel = event.get("channel")

    if channel not in WATCH:
        # DIGEST-каналы в тред не обслуживаются — они попадают в сводку.
        return
    if not filters.worth_classifying(event, owner_id=OWNER, bot_id=bot_user_id()):
        return
    if store.already_seen(channel, event["ts"]):
        return

    text = event["text"]
    author = user_name(event.get("user", "?"))
    owner_mentioned = OWNER in text
    thread_ts = event.get("thread_ts")
    replies, owner_replied, thread_excerpt = thread_state(channel, thread_ts)

    try:
        verdict = classifier.classify(
            text=text,
            author=author,
            channel_name=channel_name(channel),
            owner_mentioned=owner_mentioned,
            thread_replies=replies,
            thread_excerpt=thread_excerpt,
            owner_replied_in_thread=owner_replied,
        )
    except Exception:
        # Молчание при недоступной модели безопаснее ответа наугад.
        log.exception("classify")
        return

    cls = verdict["cls"]
    log.info("%s / %s — %s", cls, verdict["confidence"], verdict["reason"])

    if cls not in classifier.ACTIONABLE or not verdict["reply"].strip():
        return
    if cls != "MENTION":
        if owner_replied or replies >= SKIP_THREAD_IF_REPLIES_GTE:
            return
        if verdict["confidence"] == "низкая" and cls != "BUG":
            return

    # Метка ставится до отправки карточки: оборвавшийся после метки процесс
    # промолчит, оборвавшийся до неё — продублирует карточку, и второе хуже.
    try:
        app.client.reactions_add(channel=channel, timestamp=event["ts"], name=MARKER)
    except Exception:
        pass

    draft_id = store.create(
        cls=cls,
        confidence=verdict["confidence"],
        src_channel=channel,
        src_ts=event["ts"],
        src_thread_ts=thread_ts,
        src_author=author,
        src_excerpt=text,
        reply_text=verdict["reply"],
        jira_summary=verdict.get("jira_summary", ""),
    )

    if cls == "MENTION" and AUTO_REPLY_ON_MENTION:
        app.client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts or event["ts"],
            text=f"{PREFIX} {verdict['reply']}",
        )
        store.resolve(draft_id, "posted")
        app.client.chat_postMessage(
            channel=OWNER,
            text=(
                f":robot_face: Тебя тегнули в #{channel_name(channel)} — ответил сам: "
                f"«{verdict['reply'][:150]}»\n{permalink(channel, event['ts'])}"
            ),
        )
        return

    send_card(
        draft_id,
        verdict,
        src_channel=channel,
        src_ts=event["ts"],
        author=author,
        excerpt=text,
    )


def resolve_target(decision: dict, channel: str, thread_ts: str | None):
    """К какой карточке относится ответ владельца.

    Возвращает ``(draft, ambiguous, matched_by_id)``. ``ambiguous`` — список
    ждущих карточек, когда понять однозначно нельзя.

    Порядок проб идёт от самого надёжного к самому удобному. В личке с самим
    собой естественнее всего просто напечатать «ок», а не искать «ответить
    в треде», — и это должно работать. Но угадывать адресата, когда карточек
    несколько, нельзя: ценой ошибки будет чужой ответ в рабочем канале.
    """
    # 1. Ответ в треде карточки — адресат назван самим Slack.
    if thread_ts:
        draft = store.by_card(channel, thread_ts)
        if draft is not None:
            return draft, None, False

    # 2. Владелец назвал идентификатор сам.
    if decision["draft_id"]:
        draft = store.by_id_prefix(decision["draft_id"])
        if draft is not None:
            return draft, None, True

    # 3. Ждёт ровно одна карточка — двусмысленности нет.
    pending = store.awaiting()
    if len(pending) == 1:
        return pending[0], None, False
    if not pending:
        return None, None, False

    return None, pending, False


def describe(draft) -> str:
    excerpt = (draft["src_excerpt"] or "").replace("\n", " ")[:80]
    return f"`{draft['id']}` · {draft['cls']} · «{excerpt}…»"


def handle_inbox(event: dict) -> None:
    """Ответ владельца в личке: решение по карточке или правка."""
    if event.get("user") != OWNER:
        return

    text = (event.get("text") or "").strip()
    if not text:
        return

    thread_ts = event.get("thread_ts")
    decision = filters.parse_decision(text)
    draft, ambiguous, matched_by_id = resolve_target(
        decision, event["channel"], thread_ts
    )

    # Куда отвечать: если владелец писал в тред — туда же, иначе обычным
    # сообщением, чтобы ответ не спрятался в свёрнутом треде.
    def respond(message: str) -> None:
        if thread_ts:
            app.client.chat_postMessage(
                channel=event["channel"], thread_ts=thread_ts, text=message
            )
        else:
            app.client.chat_postMessage(channel=event["channel"], text=message)

    if ambiguous:
        listing = "\n".join(f"• {describe(d)}" for d in ambiguous)
        respond(
            f"Решения ждут {len(ambiguous)} карточки — не понял, про какую речь.\n"
            f"{listing}\n\nНапиши идентификатор перед ответом, например "
            f"`{ambiguous[0]['id']} ок`, или ответь реакцией прямо на карточку."
        )
        return

    if draft is None:
        return  # ничего не ждёт решения — это просто заметка себе

    if draft["status"] != "awaiting":
        respond(f"Карточка `{draft['id']}` уже закрыта: {draft['status']}.")
        return

    if decision["action"] == "unclear":
        respond(
            f"{describe(draft)}\nЧто с ней делать — «ок», «нет» или текст правки?"
        )
        return

    if decision["action"] == "reject":
        store.resolve(draft["id"], "dropped")
        respond(f"Отменил `{draft['id']}`, в канал ничего не ушло.")
        return

    if decision["action"] in ("approve", "ticket"):
        respond(publish(draft, with_ticket=decision["action"] == "ticket"))
        return

    # Всё остальное — правка. Переписываем по сказанному и присылаем новую
    # карточку, не споря: владелец видит канал целиком, а бот — нет.
    instruction = decision["text"] if matched_by_id else decision["original"]
    try:
        verdict = classifier.classify(
            text=draft["src_excerpt"] or "",
            author=draft["src_author"] or "?",
            channel_name=channel_name(draft["src_channel"]),
            owner_mentioned=True,
            thread_replies=0,
            thread_excerpt=f"Владелец просит переписать ответ так: {instruction}",
        )
    except Exception:
        log.exception("revise")
        respond("Не смог переписать — попробуй ещё раз.")
        return

    store.update_reply(draft["id"], verdict["reply"])
    send_card(
        draft["id"],
        {**verdict, "cls": draft["cls"], "confidence": draft["confidence"]},
        src_channel=draft["src_channel"],
        src_ts=draft["src_ts"],
        author=draft["src_author"] or "?",
        excerpt=draft["src_excerpt"] or "",
        revision=2,
    )


def reply_in_card(channel: str, thread_ts: str, text: str) -> None:
    app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)


@app.event("reaction_added")
def on_reaction(event):
    if event.get("user") != OWNER:
        return

    item = event.get("item", {})
    draft = store.by_card(item.get("channel", ""), item.get("ts", ""))
    if draft is None or draft["status"] != "awaiting":
        return

    reaction = event.get("reaction", "")
    card_channel, card_ts = item["channel"], item["ts"]

    if reaction in ("white_check_mark", "heavy_check_mark", "+1"):
        reply_in_card(card_channel, card_ts, publish(draft, with_ticket=False))
    elif reaction in ("ticket", "tickets"):
        reply_in_card(card_channel, card_ts, publish(draft, with_ticket=True))
    elif reaction in ("x", "no_entry_sign", "-1"):
        store.resolve(draft["id"], "dropped")
        reply_in_card(card_channel, card_ts, "Отменил, в канал ничего не ушло.")


@app.event("app_mention")
def on_app_mention(event, say):
    """Тегнули самого бота — отвечаем сразу, это его собственный разговор."""
    say(
        text=(
            f"{PREFIX} на связи. Я слежу за багами и вопросами к Глебу в этих "
            "каналах и приношу ему то, что требует решения. Если что-то нужно "
            "от него — тегни его, я подхвачу."
        ),
        thread_ts=event.get("thread_ts") or event["ts"],
    )


def has_marker(message: dict) -> bool:
    """Стоит ли на сообщении наша метка «разобрано»."""
    return any(
        reaction.get("name") == MARKER
        for reaction in message.get("reactions", [])
    )


def backfill() -> None:
    """Разобрать историю, накопившуюся, пока процесс не работал.

    Событий Slack за время простоя не будет никогда — их не переигрывают.
    Поэтому при каждом старте бот прочитывает недавнюю историю сам и
    пропускает всё, что уже помечено. Без этого любой деплой означал бы
    навсегда потерянный кусок канала.
    """
    oldest = str(int(time.time()) - BACKFILL_HOURS * 3600)
    for channel in sorted(WATCH):
        try:
            history = app.client.conversations_history(
                channel=channel, oldest=oldest, limit=50
            )["messages"]
        except Exception:
            log.exception("добор истории %s", channel)
            continue

        # Slack отдаёт новые первыми, а разбирать надо в порядке разговора.
        for message in reversed(history):
            if has_marker(message):
                continue
            process_channel_message({**message, "channel": channel})

    log.info("добор истории за %s ч завершён", BACKFILL_HOURS)


def scheduler() -> None:
    """Будильник для ежедневной сводки и отмены протухших карточек."""
    last_run_date = None
    while True:
        try:
            now = time.localtime()
            stamp = f"{now.tm_hour:02d}:{now.tm_min:02d}"
            today = time.strftime("%Y-%m-%d", now)
            if stamp == DIGEST_AT and last_run_date != today:
                last_run_date = today
                daily.run(
                    app.client,
                    inbox=OWNER,
                    digest_channels={c: channel_name(c) for c in sorted(DIGEST)},
                    owner_id=OWNER,
                    bot_id=bot_user_id(),
                    expiry_hours=DRAFT_EXPIRY_HOURS,
                )
        except Exception:
            log.exception("будильник")
        time.sleep(30)


if __name__ == "__main__":
    store.init()
    log.info(
        "Глебот стартует · модель %s · каналы %s · авто-ответ на теги: %s · "
        "сводка в %s · добор %s ч",
        classifier.MODEL,
        ", ".join(sorted(WATCH)) or "не заданы",
        AUTO_REPLY_ON_MENTION,
        DIGEST_AT,
        BACKFILL_HOURS,
    )

    backfill()
    threading.Thread(target=scheduler, daemon=True, name="glebot-scheduler").start()

    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
