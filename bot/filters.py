"""Чистая логика без обращений к Slack и к модели.

Вынесена отдельно намеренно: всё, что здесь лежит, проверяется тестом за
доли секунды и без единого сетевого вызова. Дешёвый фильтр — самая
нагруженная часть бота (через него проходит каждое сообщение всех каналов),
и ошибка в нём либо заваливает владельца мусором, либо молча съедает
баг-репорты. Такое стоит держать проверяемым.
"""

from __future__ import annotations

import re

# Односложный фон, который встречается в каналах постоянно.
NOISE_RE = re.compile(
    r"^\s*(\+\d*|-|ок|ok|да|нет|ага|угу|ахах+|хах+|ору|спс|спасибо|привет|"
    r"всем привет|доброе утро|пока|до завтра|\W{0,4})\s*$",
    re.IGNORECASE,
)

# Короче этого сообщение почти наверняка реплика в разговоре, а не запрос.
MIN_LENGTH = 15

# Подтипы, которые всё-таки надо разбирать.
#
# Отбрасывать сообщения с подтипом целиком — соблазнительная и почти верная
# эвристика: подтипами помечены вход в канал, редактирование, удаление,
# хадлы, сообщения ботов. Почти — потому что среди них же `file_share`, а
# это сообщение со скриншотом, то есть типичный баг-репорт. Именно на такие
# бот и заводится, и именно их он молча пропускал: «не валидируется загрузка
# ассетов» со скриншотом прилетело в канал, а в ответ не было ничего.
#
# `thread_broadcast` — ответ в треде, продублированный в канал; такой же
# обычный текст. `me_message` — команда /me, редкость, но тоже слова человека.
ALLOWED_SUBTYPES = {"file_share", "thread_broadcast", "me_message"}

# Разметка Slack: :эмодзи:, <@упоминания>, <ссылки|подписи>.
MARKUP_RE = re.compile(r":[a-z0-9_+\-]+:|<[^>]*>")


def meaningful_length(text: str) -> int:
    """Длина сообщения без разметки.

    Считать длину по сырому тексту нельзя: «как часы :slightly_smiling_face:»
    формально длиннее порога, а содержательно это восемь букв. Эмодзи-коды
    в Slack длинные, и без их вычистки половина фонового трёпа уходит
    в модель за деньги.
    """
    return len(MARKUP_RE.sub("", text).strip())


def worth_classifying(event: dict, *, owner_id: str, bot_id: str,
                      include_owner: bool = False) -> bool:
    """Стоит ли тратить на это сообщение вызов модели.

    Событие message.channels приходит на каждое сообщение канала, включая
    хадлы и мемы. Отсев регулярками здесь — не оптимизация, а условие
    того, чтобы бот не стоил как небольшой сотрудник.

    `include_owner` включается только для тестовых каналов. В рабочих
    сообщения владельца не разбираются никогда: бот следит за тем, что
    пишут ему, а не за тем, что пишет он. Но из-за этого бота нельзя
    проверить в одиночку — своё же сообщение он молча пропустит, и это
    выглядит как поломка.
    """
    if event.get("bot_id"):
        return False
    if event.get("subtype") and event["subtype"] not in ALLOWED_SUBTYPES:
        return False
    if event.get("user") == bot_id:
        return False
    if event.get("user") == owner_id and not include_owner:
        return False

    text = (event.get("text") or "").strip()
    if not text:
        return False

    # Прямое обращение к владельцу разбирается всегда, даже одним словом.
    if owner_id in text:
        return True

    if meaningful_length(text) < MIN_LENGTH or NOISE_RE.match(text):
        return False
    return True


def card_text(
    draft_id: str,
    verdict: dict,
    *,
    channel: str,
    author: str,
    excerpt: str,
    link: str,
    prefix_emoji: str = ":robot_face:",
    jira_project: str = "",
    jira_issue_type: str = "",
    revision: int = 1,
) -> str:
    """Собрать карточку черновика для личку владельца.

    Карточка — не просто сообщение, а запись состояния: часовой агент
    восстанавливает из неё координаты исходного сообщения, когда базы
    под рукой нет. Поэтому поля не сокращаются.
    """
    head = f"{prefix_emoji} *Черновик* `{draft_id}`"
    if revision > 1:
        head += f" · редакция {revision}"
    head += f" · {verdict['cls']} · уверенность: {verdict['confidence']}"

    quote = "\n".join(f"> {line}" for line in (excerpt[:300].splitlines() or [""]))
    link_part = f" · <{link}|открыть тред>" if link else ""

    lines = [
        head,
        f"*Канал:* #{channel} · *Автор:* {author}{link_part}",
        "",
        quote,
        "",
        "*Предлагаю ответить:*",
        verdict["reply"],
    ]
    if verdict.get("jira_summary") and jira_project:
        lines += [
            "",
            f"*Заготовка тикета:* {jira_project} / {jira_issue_type} / "
            f"«{verdict['jira_summary']}»",
        ]
    lines += [
        "",
        # Инструкция называет оба способа и место действия. Первая версия
        # обходилась значками без объяснений — и владелец справедливо не понял,
        # куда их ставить. Карточка, которой не умеют пользоваться,
        # бесполезна независимо от качества разбора.
        "_Поставь реакцию на это сообщение — ✅ запостить, 🎫 запостить "
        "и завести тикет, ❌ отменить._",
        f"_Или просто напиши здесь в ответ: «ок», «нет» или текст правки. "
        f"Если карточек несколько — начни с номера: «{draft_id} ок»._",
    ]
    return "\n".join(lines)


def notice_text(
    draft_id: str,
    verdict: dict,
    *,
    channel: str,
    author: str,
    excerpt: str,
    link: str,
    prefix_emoji: str = ":robot_face:",
    jira_project: str = "",
    jira_issue_type: str = "",
) -> str:
    """Сводка о том, что бот уже ответил сам.

    Отличается от карточки не только словами, но и назначением. Карточка
    просит решения и потому обязана быть полной. Здесь решение уже принято
    ботом, и владельцу нужно за секунду понять, всё ли в порядке: что
    произошло, что сказано от его имени и как это отменить. Поэтому цитата
    короче, а последняя строка — про отмену, а не про одобрение.
    """
    head = f"{prefix_emoji} *Ответил сам* `{draft_id}` · {verdict['cls']}"
    if verdict.get("confidence"):
        head += f" · уверенность: {verdict['confidence']}"

    quote = (excerpt or "").replace("\n", " ")[:180]
    link_part = f" · <{link}|открыть тред>" if link else ""

    lines = [
        head,
        f"*Канал:* #{channel} · *Автор:* {author}{link_part}",
        f"> {quote}",
        "",
        f"*Ответил:* {verdict['reply']}",
    ]
    if verdict.get("jira_summary") and jira_project:
        lines += [
            "",
            f"*Черновик тикета:* {jira_project} / {jira_issue_type} / "
            f"«{verdict['jira_summary']}» — заведу по 🎫",
        ]
    lines += [
        "",
        "_❌ — удалить мой ответ из треда · 🎫 — завести тикет · "
        "любой текст — перепишу отправленное._",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# разбор решения владельца
# --------------------------------------------------------------------------

APPROVE_WORDS = {"ок", "окей", "ok", "да", "+", "го", "давай", "запость", "постим"}
TICKET_WORDS = {"ок+жира", "ок+jira", "+тикет", "ok+jira", "+жира", "ок+тикет"}
REJECT_WORDS = {"нет", "не надо", "не нужно", "skip", "no", "отмена", "отменить", "-"}

# Идентификатор черновика: четыре hex-символа, возможно с двоеточием после.
DRAFT_ID_RE = re.compile(r"^\s*`?([0-9a-f]{2,4})`?\s*[:.,]?(?:\s+|$)", re.IGNORECASE)


def parse_decision(text: str) -> dict:
    """Понять, что владелец хочет сделать с черновиком.

    Возвращает ``{"action": ..., "draft_id": ..., "text": ...}``, где action —
    approve | ticket | reject | revise.

    Владелец пишет с телефона и в свободной форме: «ок», «7c1a нет»,
    «убери первое предложение». Требовать от него строгого синтаксиса —
    верный способ сделать так, чтобы карточками перестали пользоваться.
    Поэтому идентификатор необязателен, регистр не важен, а всё, что не
    опознано как команда, считается правкой текста.
    """
    original = (text or "").strip()
    raw = original

    draft_id = None
    match = DRAFT_ID_RE.match(raw)
    if match:
        draft_id = match.group(1).lower()
        raw = raw[match.end():].strip()

    normalized = raw.lower().strip(" .!»«\"'")

    if normalized in TICKET_WORDS:
        action = "ticket"
    elif normalized in APPROVE_WORDS:
        action = "approve"
    elif normalized in REJECT_WORDS:
        action = "reject"
    elif not normalized and draft_id:
        # Прислали один идентификатор без команды — это не решение.
        action = "unclear"
    else:
        action = "revise"

    # original нужен на случай, когда «идентификатор» окажется обычным словом
    # из hex-букв: тогда вызывающий код откатится к разбору всего текста
    # как правки, а не потеряет первое слово.
    return {"action": action, "draft_id": draft_id, "text": raw, "original": original}
