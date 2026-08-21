"""Создание тикетов в Jira.

Модуль намеренно необязательный: без переменных окружения бот работает
целиком, просто реакция 🎫 сводится к обычной публикации ответа. Падать
из-за ненастроенной интеграции он не должен — баг важнее тикета.
"""

from __future__ import annotations

import os

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


def find_similar(summary: str, days: int = 30) -> list[dict]:
    """Поискать похожие тикеты, чтобы не плодить дубли.

    Ищем по значимым словам заголовка. Пустой результат — не гарантия
    отсутствия дубля, поэтому решение всё равно остаётся за владельцем.
    """
    if not configured():
        return []

    words = [w for w in summary.replace(":", " ").split() if len(w) > 3][:5]
    if not words:
        return []

    text = " ".join(words).replace('"', "")
    jql = (
        f'project = {PROJECT_KEY} AND created >= -{days}d '
        f'AND text ~ "{text}" ORDER BY created DESC'
    )
    response = requests.get(
        f"{BASE_URL}/rest/api/3/search/jql",
        auth=(EMAIL, TOKEN),
        params={"jql": jql, "maxResults": 3, "fields": "summary"},
        timeout=20,
    )
    if response.status_code >= 300:
        return []
    return [
        {"key": issue["key"], "summary": issue["fields"]["summary"]}
        for issue in response.json().get("issues", [])
    ]
