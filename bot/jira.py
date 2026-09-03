"""Создание тикетов в Jira.

Модуль намеренно необязательный: без переменных окружения бот работает
целиком, просто реакция 🎫 сводится к обычной публикации ответа. Падать
из-за ненастроенной интеграции он не должен — баг важнее тикета.
"""

from __future__ import annotations

import os
import re

import requests

BASE_URL = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
EMAIL = os.environ.get("JIRA_EMAIL", "")
TOKEN = os.environ.get("JIRA_API_TOKEN", "")
PROJECT_KEY = os.environ.get("JIRA_PROJECT_KEY", "TEAMDEV")
ISSUE_TYPE_BUG = os.environ.get("JIRA_ISSUE_TYPE_BUG", "Баг")


def configured() -> bool:
    return bool(BASE_URL and EMAIL and TOKEN)


def _adf(text: str) -> dict:
    """Обернуть простой текст в Atlassian Document Format.

    Jira Cloud v3 не принимает описание строкой, только структурой.
    Пустые строки разбивают текст на абзацы.
    """
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": p.strip()}],
            }
            for p in paragraphs
        ],
    }


def create_bug(summary: str, description: str) -> str:
    """Завести баг и вернуть его ключ. Бросает исключение при ошибке."""
    if not configured():
        raise RuntimeError("Jira не настроена: нет JIRA_BASE_URL / EMAIL / API_TOKEN")

    response = requests.post(
        f"{BASE_URL}/rest/api/3/issue",
        auth=(EMAIL, TOKEN),
        json={
            "fields": {
                "project": {"key": PROJECT_KEY},
                "issuetype": {"name": ISSUE_TYPE_BUG},
                "summary": summary[:250],
                "description": _adf(description),
            }
        },
        timeout=20,
    )
    if response.status_code >= 300:
        raise RuntimeError(f"Jira {response.status_code}: {response.text[:400]}")
    return response.json()["key"]


LOOKBACK_DAYS = int(os.environ.get("JIRA_LOOKBACK_DAYS", "180"))
_WORD_RE = re.compile(r"[^\W\d_]{4,}", re.UNICODE)


def find_similar(summary: str, days: int = LOOKBACK_DAYS) -> list[dict]:
    """Поискать похожие тикеты, чтобы связать повтор с прошлым случаем.

    Два урока, оплаченных реальным промахом на TEAMDEV-695.

    Окно было 30 дней — а баг, про который в чате говорят «снова», чинили
    как раз недели и месяцы назад. Поэтому по умолчанию 180 дней.

    Слова искались все разом (`text ~ "a b c"` — это И), и репорт «замена
    приза в карточках» не находил тикет «Баг иконки в Daily Cards»: общих
    слов у них нет. Теперь ИЛИ по словам с усечением (`карточ*` ловит и
    «карточки», и «карточках»), и находится по любому пересечению. Обратная
    сторона — в выдачу попадает и лишнее, поэтому решает уже модель: список
    для неё кандидатский, а не готовый ответ.
    """
    if not configured():
        return []

    words = {w.lower() for w in _WORD_RE.findall(summary)}
    words -= {"баг", "ошибка", "проблема", "сайт", "прод", "снова", "опять"}
    words = list(words)[:6]
    if not words:
        return []

    clause = " OR ".join(f'text ~ "{w}*"' for w in words)
    jql = (
        f'project = {PROJECT_KEY} AND created >= -{days}d '
        f'AND ({clause}) ORDER BY created DESC'
    )
    response = requests.get(
        f"{BASE_URL}/rest/api/3/search/jql",
        auth=(EMAIL, TOKEN),
        params={
            "jql": jql,
            "maxResults": 8,
            # Не только заголовок: исполнитель и версия-фикс — то, ради чего
            # бот вообще лезет в Jira. Ответ «баг чинили в TEAMDEV-695,
            # заехало в 1.3.2, глянь, кто закрывал» без этих полей не собрать.
            "fields": "summary,status,assignee,fixVersions,resolutiondate",
        },
        timeout=20,
    )
    if response.status_code >= 300:
        return []
    out = []
    for issue in response.json().get("issues", []):
        f = issue.get("fields", {})
        assignee = f.get("assignee") or {}
        out.append({
            "key": issue["key"],
            "summary": f.get("summary", ""),
            "status": (f.get("status") or {}).get("name", ""),
            "assignee_name": assignee.get("displayName", ""),
            "assignee_email": assignee.get("emailAddress", ""),
            "fix_versions": [v.get("name", "") for v in f.get("fixVersions") or []],
            "resolved": (f.get("resolutiondate") or "")[:10],
        })
    return out


def describe_matches(matches: list[dict]) -> str:
    """Свернуть похожие тикеты в короткую справку для модели.

    Пустая строка, если ничего похожего нет: тогда рубрика работает как
    раньше, без ссылок на прошлое.
    """
    lines = []
    for m in matches:
        part = f"{m['key']} «{m['summary'][:80]}» — {m['status'] or 'статус неизвестен'}"
        if m["fix_versions"]:
            part += f", релиз {', '.join(m['fix_versions'])}"
        if m["resolved"]:
            part += f", закрыт {m['resolved']}"
        if m["assignee_name"]:
            part += f", чинил {m['assignee_name']}"
        lines.append(part)
    return "\n".join(lines)
