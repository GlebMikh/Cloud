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


def worth_classifying(event: dict, *, owner_id: str, bot_id: str) -> bool:
    """Стоит ли тратить на это сообщение вызов модели.

    Событие message.channels приходит на каждое сообщение канала, включая
    хадлы и мемы. Отсев регулярками здесь — не оптимизация, а условие
    того, чтобы бот не стоил как небольшой сотрудник.
    """
    if event.get("bot_id") or event.get("subtype"):
        return False
    if event.get("user") in (owner_id, bot_id):
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
        "_✅ запостить · 🎫 запостить и завести тикет · ❌ отменить · текстом — правка_",
    ]
    return "\n".join(lines)
