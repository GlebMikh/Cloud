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
# Адаптер на websocket-client, а не встроенный в slack_sdk: встроенный на
# каждом разрыве сети — сон ноутбука, VPN, икота провайдера — сыплет в лог
# «Failed to check the state of sock» каждые десять секунд и переподключается
# неохотно. Этот делает то же самое молча и надёжно.
from slack_bolt.adapter.socket_mode.websocket_client import SocketModeHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# .env читается ДО импорта своих модулей: они разбирают окружение прямо при
# импорте (ключ Anthropic, путь к базе, настройки Jira). Загрузка после
# импортов выглядела бы рабочей и молча брала бы значения по умолчанию.
# Библиотека необязательная: на хостинге переменные приходят из окружения,
# а .env нужен только на своей машине.
try:  # noqa: SIM105
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

import classifier  # noqa: E402
import daily  # noqa: E402
import filters  # noqa: E402
import jira  # noqa: E402
import store  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("glebot")


def required_env(name: str) -> str:
    """Обязательная переменная окружения — или внятный отказ стартовать.

    Первое, обо что спотыкается любой запуск, — незаполненный .env, и
    голый KeyError в трейсбеке не подсказывает, что делать дальше.
    """
    value = os.environ.get(name)
    if not value:
        sys.exit(
            f"Не задана переменная {name}.\n"
            "Скопируй bot/.env.example в .env в корне репозитория и впиши "
            "значения — где их взять, написано в bot/README.md."
        )
    return value


OWNER = required_env("OWNER_SLACK_ID")
WATCH = {c.strip() for c in os.environ.get("WATCH_CHANNELS", "").split(",") if c.strip()}
DIGEST = {c.strip() for c in os.environ.get("DIGEST_CHANNELS", "").split(",") if c.strip()}
# Реагировать во всех каналах, где бот состоит, а не по фиксированному списку.
# В Socket Mode Slack и так доставляет события только из каналов-участников,
# поэтому «везде, где добавлен» — это просто «не фильтровать по списку»:
# добавил бота в канал — он там работает, без правки конфига.
# WATCH_CHANNELS при этом становится необязательным; пустой список означает
# не «нигде», а «везде».
WATCH_ALL_JOINED = os.environ.get("WATCH_ALL_JOINED", "true").lower() == "true"
# Каналы, куда бот не лезет, даже будучи участником: болталки, флуд, личное.
EXCLUDE = {c.strip() for c in os.environ.get("EXCLUDE_CHANNELS", "").split(",") if c.strip()}
# Каналы для проверки бота в одиночку: здесь разбираются и сообщения самого
# владельца. В рабочих каналах это было бы вредно — бот отвечал бы на слова
# того, кому он помогает, — а в песочнице иначе просто нечем проверить.
TEST_CHANNELS = {c.strip() for c in os.environ.get("TEST_CHANNELS", "").split(",") if c.strip()}
WATCH |= TEST_CHANNELS
MARKER = os.environ.get("MARKER_EMOJI", "robot_face")
AUTO_REPLY_ON_MENTION = os.environ.get("AUTO_REPLY_ON_MENTION", "false").lower() == "true"
# Классы, на которые бот отвечает сам, не спрашивая. Пусто — прежний режим,
# когда в канал не уходит ни слова без «ок». `AUTO_REPLY_ON_MENTION=true`
# из старых настроек означает то же самое для одного класса MENTION.
AUTOPOST_CLASSES = {
    c.strip().upper()
    for c in os.environ.get("AUTOPOST_CLASSES", "MENTION" if AUTO_REPLY_ON_MENTION else "").split(",")
    if c.strip()
}
# Ниже этой уверенности бот всё равно спрашивает: цена ошибки автоответа —
# не потраченное внимание владельца, а чужие глаза в рабочем канале.
AUTOPOST_MIN_CONFIDENCE = os.environ.get("AUTOPOST_MIN_CONFIDENCE", "средняя").strip().lower()
CONFIDENCE_RANK = {"низкая": 1, "средняя": 2, "высокая": 3}
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

# Пауза перед автоответом, секунды. Бот выбирает маршрут по состоянию треда
# на момент разбора — но живой коллега может откликнуться секундой позже, и
# тогда бот уже высказался. Пауза даёт людям фору: перед отправкой тред
# перечитывается заново, и если там кто-то появился, ответ не уходит в канал
# вовсе. 0 — отвечать сразу.
REPLY_DELAY = int(os.environ.get("REPLY_DELAY_SECONDS", "120"))
# Отложенный ответ, переживший перезапуск, старше этого возраста в канал не
# уходит: в разговоре двухчасовой давности реплика бота выглядит нелепо.
STALE_DELAYED_MINUTES = float(os.environ.get("STALE_DELAYED_MINUTES", "30"))
# Живые таймеры — чтобы их можно было отменить при остановке и в тестах.
_timers: dict[str, threading.Timer] = {}

try:
    app = App(
        token=required_env("SLACK_BOT_TOKEN"),
        signing_secret=required_env("SLACK_SIGNING_SECRET"),
    )
except Exception as exc:  # токены есть, но Slack их не принял
    # Bolt проверяет токен при создании приложения и падает трейсбеком на
    # тридцать строк. Второй по частоте промах после пустого .env —
    # перепутанные местами токены, и об этом стоит сказать словами.
    sys.exit(
        f"Slack не принял токены: {exc}\n"
        "Проверь SLACK_BOT_TOKEN (начинается с xoxb-) и SLACK_SIGNING_SECRET "
        "в .env — оба берутся на api.slack.com/apps, порядок в bot/README.md."
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

@functools.lru_cache(maxsize=256)
def slack_mention_for(email: str | None) -> str:
    """Найти в Slack упоминание по почте из Jira, если получится.

    Тег живого человека полезнее, чем имя строкой: тот, кто чинил баг,
    увидит, что регресс вернулся к нему. Но почта в Jira может быть скрыта,
    а пользователя в Slack — не найтись; тогда возвращаем пусто, и ответ
    просто назовёт исполнителя по имени.
    """
    if not email:
        return ""
    try:
        user = app.client.users_lookupByEmail(email=email)["user"]
        return f"<@{user['id']}>"
    except Exception:
        return ""


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
# автоответ: сначала отвечаем, потом отчитываемся
# --------------------------------------------------------------------------

def should_autopost(cls: str, confidence: str) -> bool:
    """Отвечать ли самому, не спрашивая.

    Уверенность — единственный порог, который здесь имеет смысл. Ошибка
    автоответа видна не владельцу, а всему каналу, поэтому сомнительный
    разбор идёт прежним путём: карточка, одобрение, только потом ответ.
    """
    if cls not in AUTOPOST_CLASSES:
        return False
    return CONFIDENCE_RANK.get(confidence, 0) >= CONFIDENCE_RANK.get(AUTOPOST_MIN_CONFIDENCE, 2)


def autopost(draft_id: str, verdict: dict, *, src_channel: str, src_ts: str,
             thread_ts: str | None, author: str, excerpt: str) -> None:
    """Ответить в тред самому и отчитаться владельцу постфактум.

    Порядок именно такой: сначала ответ, потом запись координат, потом
    сводка. Если процесс оборвётся между шагами, худшее, что случится, —
    владелец не увидит сводку о честно отправленном ответе; обратный
    порядок дал бы сводку о том, чего в канале нет.
    """
    posted = app.client.chat_postMessage(
        channel=src_channel,
        thread_ts=thread_ts or src_ts,
        text=f"{PREFIX} {verdict['reply']}",
    )
    store.mark_posted(draft_id, src_channel, posted["ts"])
    store.resolve(draft_id, "posted")

    notice = app.client.chat_postMessage(
        channel=OWNER,
        text=filters.notice_text(
            draft_id,
            verdict,
            channel=channel_name(src_channel),
            author=author,
            excerpt=excerpt,
            link=permalink(src_channel, src_ts),
            jira_project=jira.PROJECT_KEY,
            jira_issue_type=jira.ISSUE_TYPE_BUG,
        ),
    )
    store.attach_card(draft_id, notice["channel"], notice["ts"])
    log.info("ответил сам в #%s (%s)", channel_name(src_channel), draft_id)


def hold(draft_id: str, verdict: dict, *, src_channel: str, src_ts: str,
         author: str, excerpt: str, thread_excerpt: str) -> None:
    """Не писать в тред, а рассказать владельцу, что там происходит."""
    store.resolve(draft_id, "held")
    notice = app.client.chat_postMessage(
        channel=OWNER,
        text=filters.notice_text(
            draft_id,
            verdict,
            channel=channel_name(src_channel),
            author=author,
            excerpt=excerpt,
            link=permalink(src_channel, src_ts),
            jira_project=jira.PROJECT_KEY,
            jira_issue_type=jira.ISSUE_TYPE_BUG,
            mode="held",
            thread_excerpt=thread_excerpt,
        ),
    )
    store.attach_card(draft_id, notice["channel"], notice["ts"])
    log.info("не влез в тред в #%s (%s) — доложил в личку", channel_name(src_channel), draft_id)


def answer_anyway(draft, instruction: str = "") -> str:
    """Всё-таки ответить в тред по просьбе владельца.

    Текст генерируется заново: тот, что лежит в черновике, написан для
    владельца — служебная справка о чужом треде. Отправить её в канал
    означало бы заговорить с коллегами языком отчёта.
    """
    thread_ts = draft["src_thread_ts"] or draft["src_ts"]
    hint = f"Владелец просит ответить в тред так: {instruction}" if instruction else ""
    try:
        verdict = classifier.classify(
            text=draft["src_excerpt"] or "",
            author=draft["src_author"] or "?",
            channel_name=channel_name(draft["src_channel"]),
            owner_mentioned=False,
            thread_replies=0,
            thread_excerpt=hint,
            audience="channel",
        )
    except Exception:
        log.exception("ответ по просьбе %s", draft["id"])
        return "Не смог собрать ответ — попробуй ещё раз."

    posted = app.client.chat_postMessage(
        channel=draft["src_channel"],
        thread_ts=thread_ts,
        text=f"{PREFIX} {verdict['reply']}",
    )
    store.mark_posted(draft["id"], draft["src_channel"], posted["ts"])
    store.update_reply(draft["id"], verdict["reply"])
    store.resolve(draft["id"], "posted")
    return f"Ответил в треде: «{verdict['reply'][:200]}»"


def verdict_from(draft) -> dict:
    """Собрать вердикт обратно из записи — тем, кто отправляет с задержкой."""
    return {
        "cls": draft["cls"],
        "confidence": draft["confidence"],
        "reason": "",
        "reply": draft["reply_text"] or "",
        "jira_summary": draft["jira_summary"] or "",
    }


def schedule_reply(draft_id: str) -> None:
    """Отложить отправку и вернуться к ней через паузу."""
    store.mark_delayed(draft_id)
    timer = threading.Timer(REPLY_DELAY, deliver, args=(draft_id,))
    timer.daemon = True
    _timers[draft_id] = timer
    timer.start()
    log.info("ответ %s отложен на %s с — жду, не откликнется ли живой", draft_id, REPLY_DELAY)


def deliver(draft_id: str) -> None:
    """Отправить отложенный ответ — если он всё ещё нужен.

    Здесь и происходит главное: тред перечитывается заново. За минуту-две
    мог откликнуться разработчик, мог ответить сам владелец, тема могла
    закрыться. Отправлять вслепую то, что решено две минуты назад, — ровно
    та ошибка, ради которой пауза и заводилась.
    """
    _timers.pop(draft_id, None)
    draft = store.by_id(draft_id)
    if draft is None or draft["status"] != "delayed":
        return  # владелец уже что-то сделал с этим черновиком

    channel = draft["src_channel"]
    thread_ts = draft["src_thread_ts"] or draft["src_ts"]
    replies, owner_replied, thread_excerpt = thread_state(channel, thread_ts)
    verdict = verdict_from(draft)
    stale = (time.time() - draft["created_at"]) > STALE_DELAYED_MINUTES * 60

    if owner_replied:
        # Владелец ответил сам, пока бот выжидал. Лучшее, что можно сделать, —
        # исчезнуть: он уже в курсе темы, сводка ему ничего не добавит.
        store.resolve(draft_id, "dropped")
        log.info("ответ %s не понадобился: владелец ответил сам", draft_id)
        return

    if replies or stale:
        # Кто-то откликнулся за время паузы — или пауза затянулась из-за
        # перезапуска. В обоих случаях в канал уже поздно, а владельцу
        # рассказать стоит. Текст переписывается для него: тот, что лежит
        # в черновике, написан для канала.
        try:
            fresh = classifier.classify(
                text=draft["src_excerpt"] or "",
                author=draft["src_author"] or "?",
                channel_name=channel_name(channel),
                owner_mentioned=False,
                thread_replies=replies,
                thread_excerpt=thread_excerpt,
                audience="owner",
            )
            verdict = {**fresh, "cls": draft["cls"], "confidence": draft["confidence"]}
            store.update_reply(draft_id, verdict["reply"])
        except Exception:
            log.exception("пересборка ответа %s для личку", draft_id)

        hold(
            draft_id,
            verdict,
            src_channel=channel,
            src_ts=draft["src_ts"],
            author=draft["src_author"] or "?",
            excerpt=draft["src_excerpt"] or "",
            thread_excerpt=thread_excerpt or ("ответ устарел, пока бот не работал" if stale else ""),
        )
        return

    autopost(
        draft_id,
        verdict,
        src_channel=channel,
        src_ts=draft["src_ts"],
        thread_ts=draft["src_thread_ts"],
        author=draft["src_author"] or "?",
        excerpt=draft["src_excerpt"] or "",
    )


def recover_delayed() -> None:
    """Разобраться с отложенными ответами, пережившими перезапуск.

    Без этого они остались бы в базе навсегда: таймер жил в памяти
    процесса, которого больше нет, и ответ не ушёл бы ни в канал, ни в
    личку — молча пропал бы.
    """
    stuck = store.delayed()
    for draft in stuck:
        deliver(draft["id"])
    if stuck:
        log.info("разобрал %s отложенных ответов после перезапуска", len(stuck))


def undo(draft) -> str:
    """Убрать из треда то, что бот сказал от имени владельца."""
    if not draft["posted_ts"]:
        return "Нечего отменять: в канал ничего не уходило."
    try:
        app.client.chat_delete(channel=draft["posted_channel"], timestamp=draft["posted_ts"])
    except Exception as exc:
        log.exception("удаление ответа %s", draft["id"])
        return f"Не смог удалить ответ: {exc}. Удали руками в треде."
    store.resolve(draft["id"], "undone")
    return "Удалил свой ответ из треда."


def make_ticket(draft) -> str:
    """Завести тикет по уже отправленному ответу."""
    if not draft["jira_summary"]:
        return "Заготовки тикета для этого сообщения нет."
    if draft["jira_key"]:
        return f"Тикет уже заведён: {draft['jira_key']}."
    if not jira.configured():
        return "Jira не настроена — тикет не завёл. Нужны JIRA_EMAIL и JIRA_API_TOKEN в .env."
    try:
        link = permalink(draft["src_channel"], draft["src_ts"])
        key = jira.create_bug(
            draft["jira_summary"],
            f"Откуда: Slack, #{channel_name(draft['src_channel'])}, {draft['src_author']}\n"
            f"{link}\n\n"
            f"Что происходит\n{draft['src_excerpt']}\n\n"
            f"Заведено Глеботом из обсуждения в Slack, детали — по ссылке выше.",
        )
    except Exception as exc:
        log.exception("Jira")
        return f"Тикет завести не вышло: {exc}"
    store.attach_ticket(draft["id"], key)
    return f"Завёл {key}."


def rewrite_posted(draft, instruction: str) -> str:
    """Переписать уже отправленный ответ прямо в треде.

    Правка редактирует существующее сообщение, а не досылает второе:
    в рабочем канале две реплики подряд от бота выглядят хуже одной
    неточной.
    """
    try:
        verdict = classifier.classify(
            text=draft["src_excerpt"] or "",
            author=draft["src_author"] or "?",
            channel_name=channel_name(draft["src_channel"]),
            owner_mentioned=True,
            thread_replies=0,
            thread_excerpt=f"Владелец просит переписать уже отправленный ответ так: {instruction}",
        )
    except Exception:
        log.exception("правка отправленного %s", draft["id"])
        return "Не смог переписать — попробуй ещё раз."

    try:
        app.client.chat_update(
            channel=draft["posted_channel"],
            ts=draft["posted_ts"],
            text=f"{PREFIX} {verdict['reply']}",
        )
    except Exception as exc:
        log.exception("обновление ответа %s", draft["id"])
        return f"Не смог поправить сообщение в треде: {exc}"

    store.update_reply(draft["id"], verdict["reply"])
    return f"Поправил в треде: «{verdict['reply'][:200]}»"


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

    if channel in DIGEST or channel in EXCLUDE:
        # DIGEST — только в сводку, в тред бот там не пишет. EXCLUDE —
        # каналы, куда его звать не стоило: болталки и личное.
        return
    if not (WATCH_ALL_JOINED or channel in WATCH):
        return
    if not filters.worth_classifying(
        event,
        owner_id=OWNER,
        bot_id=bot_user_id(),
        include_owner=channel in TEST_CHANNELS,
    ):
        return
    if store.already_seen(channel, event["ts"]):
        return

    text = event["text"]
    author = user_name(event.get("user", "?"))
    owner_mentioned = OWNER in text
    thread_ts = event.get("thread_ts")
    replies, owner_replied, thread_excerpt = thread_state(channel, thread_ts)
    media = filters.media_kinds(event)

    # Кто-то из команды уже отреагировал — значит работа началась без нас.
    # Третий голос в треде тут не помогает никому, а владельцу знать полезно:
    # ответ пойдёт ему в личку и будет написан для него, а не для канала.
    # Прямой тег владельца — исключение: там короткое «увидел, передаю» в
    # треде уместно даже при чужих ответах, человек ждёт реакции на своё
    # обращение. Признак известен до классификации, поэтому маршрут можно
    # выбрать заранее — а от него зависит, для кого модель пишет текст.
    held = bool(replies) and not owner_replied and not owner_mentioned

    try:
        verdict = classifier.classify(
            text=text,
            author=author,
            channel_name=channel_name(channel),
            owner_mentioned=owner_mentioned,
            thread_replies=replies,
            thread_excerpt=thread_excerpt,
            owner_replied_in_thread=owner_replied,
            audience="owner" if held else "channel",
            media=media,
        )
    except classifier.RateLimited as limit:
        # Не ошибка, а решение: квота дороже одной карточки. Сообщение
        # остаётся неразобранным и без метки — добор при следующем старте
        # к нему вернётся.
        log.warning("пропускаю разбор: %s", limit)
        return
    except Exception:
        # Молчание при недоступной модели безопаснее ответа наугад.
        log.exception("classify")
        return

    cls = verdict["cls"]

    # Класс требует ответа, а ответа нет — это промах модели, а не решение
    # промолчать. Одна повторная попытка дешевле, чем потерянный баг-репорт;
    # если и она пустая, об этом будет видно в журнале решений.
    if cls in classifier.ACTIONABLE and not verdict["reply"].strip():
        log.warning("%s без текста ответа — пробую ещё раз", cls)
        try:
            verdict = classifier.classify(
                text=text,
                author=author,
                channel_name=channel_name(channel),
                owner_mentioned=owner_mentioned,
                thread_replies=replies,
                thread_excerpt=thread_excerpt,
                owner_replied_in_thread=owner_replied,
                audience="owner" if held else "channel",
                media=media,
            )
            cls = verdict["cls"]
        except Exception:
            log.exception("повторный разбор")

    # Баг — единственный класс, ради которого стоит идти в Jira: там ищется
    # прошлый такой же тикет, и если он есть, разбор переигрывается уже с
    # ним. Тогда ответ ссылается на TEAMDEV-…, называет релиз и зовёт того,
    # кто чинил, — вместо того чтобы описывать проблему заново. Лишний вызов
    # модели тут оправдан: баги редки, а цена вопроса — не «ещё карточка», а
    # «повторно пропущенный регресс».
    if cls == "BUG" and jira.configured():
        try:
            matches = jira.find_similar(verdict.get("jira_summary") or text)
        except Exception:
            log.exception("поиск похожих тикетов")
            matches = []
        if matches:
            jira_context = jira.describe_matches(matches)  # noqa: F841 — уходит в classify
            mention = slack_mention_for(matches[0].get("assignee_email"))
            if mention:
                jira_context += f"\nчинившего можно тегнуть так: {mention}"
            try:
                verdict = classifier.classify(
                    text=text, author=author, channel_name=channel_name(channel),
                    owner_mentioned=owner_mentioned, thread_replies=replies,
                    thread_excerpt=thread_excerpt, owner_replied_in_thread=owner_replied,
                    audience="owner" if held else "channel", media=media,
                    jira_context=jira_context,
                )
                cls = verdict["cls"]
                log.info("баг обогащён похожими тикетами: %s", matches[0]["key"])
            except Exception:
                log.exception("повторный разбор с Jira-контекстом")

    def remember(outcome: str) -> None:
        """Записать разбор вместе с тем, чем он кончился."""
        store.mark_seen(
            channel, event["ts"], cls=cls, confidence=verdict["confidence"],
            reason=verdict.get("reason", ""), outcome=outcome,
        )

    log.info(
        "%s / %s — %s (кеш %s токенов)",
        cls,
        verdict["confidence"],
        verdict["reason"],
        verdict.get("_usage", {}).get("cached", "?"),
    )

    if cls not in classifier.ACTIONABLE:
        remember("класс не требует ответа")
        return
    if not verdict["reply"].strip():
        remember("модель не дала текста ответа даже со второй попытки")
        return
    if cls != "MENTION":
        if owner_replied or replies >= SKIP_THREAD_IF_REPLIES_GTE:
            remember("в треде уже идёт разговор")
            return
        if verdict["confidence"] == "низкая" and cls != "BUG":
            remember("низкая уверенность — не рискую")
            return

    remember("held" if held else ("автоответ" if should_autopost(cls, verdict["confidence"]) else "карточка"))

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

    if held:
        hold(
            draft_id,
            verdict,
            src_channel=channel,
            src_ts=event["ts"],
            author=author,
            excerpt=text,
            thread_excerpt=thread_excerpt,
        )
        return

    if should_autopost(cls, verdict["confidence"]):
        if REPLY_DELAY > 0:
            schedule_reply(draft_id)
            return
        autopost(
            draft_id,
            verdict,
            src_channel=channel,
            src_ts=event["ts"],
            thread_ts=thread_ts,
            author=author,
            excerpt=text,
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
    if pending:
        return None, pending, False

    # 4. Ничего не ждёт решения, но бот недавно отвечал сам. «Нет», сказанное
    # сразу после сводки, относится к ней — иначе отменить автоответ можно
    # было бы только реакцией, а с телефона это лишний жест.
    recent = store.recently_posted()
    if len(recent) == 1:
        return recent[0], None, False
    if recent:
        return None, recent, False

    return None, None, False


def describe(draft) -> str:
    excerpt = (draft["src_excerpt"] or "").replace("\n", " ")[:80]
    return f"`{draft['id']}` · {draft['cls']} · «{excerpt}…»"


def handle_inbox(event: dict) -> None:
    """Ответ владельца в личке: решение по карточке или правка."""
    if event.get("user") != OWNER:
        return
    # Правка сообщения, удаление, ответ бота — не решения. Slack шлёт их
    # тем же событием, и без этой проверки исправленная опечатка в «ок»
    # выглядит как второе одобрение.
    if event.get("subtype") or event.get("bot_id"):
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

    # Бот придержал ответ: словом можно попросить всё же ответить, завести
    # тикет или закрыть тему.
    if draft["status"] == "held":
        action = decision["action"]
        instruction = decision["text"] if matched_by_id else decision["original"]
        if action == "approve":
            respond(answer_anyway(draft))
        elif action == "ticket":
            respond(make_ticket(draft))
        elif action == "reject":
            store.resolve(draft["id"], "dropped")
            respond("Закрыл, в канал ничего не уходило.")
        elif action == "unclear":
            respond(
                f"{describe(draft)}\nОтветить в треде, завести тикет или закрыть?"
            )
        else:
            respond(answer_anyway(draft, instruction))
        return

    # Ответ уже в канале — значит словом можно отменить его, довести до
    # тикета или переписать прямо в треде.
    if draft["status"] == "posted" and draft["posted_ts"]:
        action = decision["action"]
        instruction = decision["text"] if matched_by_id else decision["original"]
        if action == "reject":
            respond(undo(draft))
        elif action == "ticket":
            respond(make_ticket(draft))
        elif action == "approve":
            respond("Этот ответ уже в треде — отменить можно словом «нет» или ❌.")
        elif action == "unclear":
            respond(f"{describe(draft)}\nОтвет уже отправлен. Отменить, завести тикет или переписать?")
        else:
            respond(rewrite_posted(draft, instruction))
        return

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
    old_card_channel, old_card_ts = draft["card_channel"], draft["card_ts"]
    send_card(
        draft["id"],
        {**verdict, "cls": draft["cls"], "confidence": draft["confidence"]},
        src_channel=draft["src_channel"],
        src_ts=draft["src_ts"],
        author=draft["src_author"] or "?",
        excerpt=draft["src_excerpt"] or "",
        revision=2,
    )
    # Решение теперь ищется по новой карточке, и галочка на старой не сделает
    # ничего — молча. Сказать об этом дешевле, чем разбираться, почему ответ
    # не ушёл.
    if old_card_channel and old_card_ts:
        try:
            reply_in_card(
                old_card_channel,
                old_card_ts,
                "Переписал — решение принимаю по новой карточке ниже. "
                "Реакции на этой больше не действуют.",
            )
        except Exception:
            log.exception("пометка старой карточки %s", draft["id"])


def reply_in_card(channel: str, thread_ts: str, text: str) -> None:
    app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)


@app.event("reaction_added")
def on_reaction(event):
    if event.get("user") != OWNER:
        return

    item = event.get("item", {})
    draft = store.by_card(item.get("channel", ""), item.get("ts", ""))
    if draft is None:
        return

    reaction = event.get("reaction", "")
    card_channel, card_ts = item["channel"], item["ts"]

    # Сводка об уже отправленном ответе: значки означают не «сделать», а
    # «переделать» — отменить сказанное или довести до тикета.
    if draft["status"] == "posted":
        if reaction in ("x", "no_entry_sign", "-1"):
            reply_in_card(card_channel, card_ts, undo(draft))
        elif reaction in ("ticket", "tickets"):
            reply_in_card(card_channel, card_ts, make_ticket(draft))
        return

    # Сводка о треде, куда бот не полез: значки означают «всё же ответить»
    # или «закрыть тему».
    if draft["status"] == "held":
        if reaction in ("white_check_mark", "heavy_check_mark", "+1"):
            reply_in_card(card_channel, card_ts, answer_anyway(draft))
        elif reaction in ("ticket", "tickets"):
            reply_in_card(card_channel, card_ts, make_ticket(draft))
        elif reaction in ("x", "no_entry_sign", "-1"):
            store.resolve(draft["id"], "dropped")
            reply_in_card(card_channel, card_ts, "Закрыл, в канал ничего не уходило.")
        return

    if draft["status"] != "awaiting":
        return

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


def safe_process(event: dict) -> None:
    """Разобрать сообщение из истории, не роняя весь добор.

    Одно сообщение, на котором споткнулись, не должно уносить остальные
    сутки: бот в этот момент ещё даже не начал слушать события, и упавший
    добор означает молчание до следующего рестарта.
    """
    try:
        process_channel_message(event)
    except Exception:
        log.exception("разбор %s / %s", event.get("channel"), event.get("ts"))


def joined_channels() -> set[str]:
    """Каналы, где бот состоит участником — публичные и приватные.

    Живому потоку событий этот список не нужен: Slack сам шлёт события
    только из каналов-участников. Нужен он добору истории при старте —
    там некому подсказать, куда смотреть, кроме самого Slack.
    """
    found: set[str] = set()
    cursor = None
    try:
        while True:
            resp = app.client.users_conversations(
                types="public_channel,private_channel",
                exclude_archived=True,
                limit=200,
                cursor=cursor,
            )
            found.update(c["id"] for c in resp["channels"])
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
    except Exception:
        log.exception("не смог перечислить каналы бота")
    return found


def backfill_channels() -> set[str]:
    """Где добирать историю: все каналы-участники или явный список."""
    channels = (joined_channels() if WATCH_ALL_JOINED else set(WATCH)) | TEST_CHANNELS
    return channels - DIGEST - EXCLUDE


def backfill() -> None:
    """Разобрать историю, накопившуюся, пока процесс не работал.

    Событий Slack за время простоя не будет никогда — их не переигрывают.
    Поэтому при каждом старте бот прочитывает недавнюю историю сам и
    пропускает всё, что уже помечено. Без этого любой деплой означал бы
    навсегда потерянный кусок канала.

    История отдаёт только корневые сообщения, и корень датируется своим
    временем, а не временем последнего ответа. Тред, заведённый неделю
    назад, в окно не попадёт, даже если ответы в нём писали десять минут
    назад — а именно в таких тредах и живёт обсуждение разработки. Поэтому
    окно применяется не к запросу, а к разбору: историю берём целиком, а
    дальше смотрим на `latest_reply`.
    """
    oldest = time.time() - BACKFILL_HOURS * 3600
    for channel in sorted(backfill_channels()):
        try:
            history = app.client.conversations_history(channel=channel, limit=50)[
                "messages"
            ]
        except Exception:
            log.exception("добор истории %s", channel)
            continue

        # Slack отдаёт новые первыми, а разбирать надо в порядке разговора.
        for message in reversed(history):
            if float(message.get("ts", 0)) >= oldest and not has_marker(message):
                safe_process({**message, "channel": channel})
            if float(message.get("latest_reply", 0)) >= oldest:
                backfill_thread(channel, message["ts"], oldest)

    log.info("добор истории за %s ч завершён", BACKFILL_HOURS)


def backfill_thread(channel: str, thread_ts: str, oldest: float) -> None:
    """Разобрать свежие ответы в треде, чей корень уже вне окна."""
    try:
        replies = app.client.conversations_replies(
            channel=channel, ts=thread_ts, limit=100
        )["messages"]
    except Exception:
        log.exception("добор треда %s/%s", channel, thread_ts)
        return

    for reply in replies:
        if reply.get("ts") == thread_ts:
            continue  # корень уже разобран выше или слишком стар
        if float(reply.get("ts", 0)) < oldest or has_marker(reply):
            continue
        safe_process({**reply, "channel": channel, "thread_ts": thread_ts})


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
        "Глебот стартует · разбор через %s · модель %s · каналы %s · "
        "сам отвечает на: %s · сводка в %s · добор %s ч · тестовые каналы: %s",
        "Anthropic API" if classifier.backend() == "api"
        else f"подписку Claude Code ({classifier.cli_path()})",
        classifier.MODEL,
        "везде, где бот участник" + (f" (кроме {', '.join(sorted(EXCLUDE))})" if EXCLUDE else "")
        if WATCH_ALL_JOINED else (", ".join(sorted(WATCH)) or "не заданы"),
        ", ".join(sorted(AUTOPOST_CLASSES)) or "ничего, всё через одобрение",
        DIGEST_AT,
        BACKFILL_HOURS,
        ", ".join(sorted(TEST_CHANNELS)) or "нет",
    )

    recover_delayed()
    backfill()
    threading.Thread(target=scheduler, daemon=True, name="glebot-scheduler").start()

    # Соединение переподключается само, но если рухнет весь клиент —
    # DNS пропал вместе с сетью, истёк токен, что угодно, — бот обязан
    # пробовать снова, а не умирать: он работает без присмотра, и его
    # смерть заметят только по пропавшим карточкам.
    while True:
        try:
            SocketModeHandler(app, required_env("SLACK_APP_TOKEN")).start()
        except KeyboardInterrupt:
            log.info("остановлен вручную")
            break
        except Exception:
            log.exception("Socket Mode упал целиком — переподключаюсь через 30 секунд")
            time.sleep(30)
