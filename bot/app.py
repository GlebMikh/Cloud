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

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import classifier  # noqa: E402
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

APPROVE_WORDS = {"ок", "ok", "да", "+", "го", "давай", "запость"}
TICKET_WORDS = {"ок+жира", "ок+jira", "+тикет", "ok+jira", "+жира"}
REJECT_WORDS = {"нет", "не надо", "skip", "no", "отмена", "-"}

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
    channel = event.get("channel")

    # Личка владельца — это инбокс: там решения по карточкам, не разбор.
    if event.get("channel_type") == "im":
        handle_inbox(event)
        return

    if channel not in WATCH:
        # DIGEST-каналы бот не обслуживает: сводки по ним остаются
        # за часовым обходом, которому не нужен постоянный процесс.
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


def handle_inbox(event: dict) -> None:
    """Ответ владельца в личке: решение по карточке или правка."""
    if event.get("user") != OWNER:
        return

    thread_ts = event.get("thread_ts")
    if not thread_ts:
        return  # реплика не в треде карточки — не наше дело

    draft = store.by_card(event["channel"], thread_ts)
    if draft is None or draft["status"] != "awaiting":
        return

    answer = (event.get("text") or "").strip().lower()

    if answer in REJECT_WORDS:
        store.resolve(draft["id"], "dropped")
        reply_in_card(event["channel"], thread_ts, "Отменил, в канал ничего не ушло.")
        return

    if answer in TICKET_WORDS:
        reply_in_card(event["channel"], thread_ts, publish(draft, with_ticket=True))
        return

    if answer in APPROVE_WORDS:
        reply_in_card(event["channel"], thread_ts, publish(draft, with_ticket=False))
        return

    # Всё остальное — правка. Переписываем ответ по сказанному и
    # присылаем новую карточку, не споря: владелец видит канал целиком.
    try:
        verdict = classifier.classify(
            text=draft["src_excerpt"],
            author=draft["src_author"] or "?",
            channel_name=channel_name(draft["src_channel"]),
            owner_mentioned=True,
            thread_replies=0,
            thread_excerpt=f"Владелец просит переписать ответ так: {event['text']}",
        )
    except Exception:
        log.exception("revise")
        reply_in_card(event["channel"], thread_ts, "Не смог переписать — попробуй ещё раз.")
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


if __name__ == "__main__":
    store.init()
    log.info(
        "Глебот стартует · модель %s · каналы %s · авто-ответ на теги: %s",
        classifier.MODEL,
        ", ".join(sorted(WATCH)) or "не заданы",
        AUTO_REPLY_ON_MENTION,
    )
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
