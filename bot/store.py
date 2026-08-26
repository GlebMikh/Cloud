"""Хранилище черновиков.

SQLite, а не память процесса: бот перезапускается — деплой, падение,
перезагрузка хоста, — и висящие на одобрении карточки не должны при этом
исчезать. Владелец не обязан помнить, что он не ответил на карточку,
которую съел рестарт.
"""

from __future__ import annotations

import os
import sqlite3
import time
import uuid

DB_PATH = os.environ.get("GLEBOT_DB", "glebot.sqlite3")

SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts (
    id            TEXT PRIMARY KEY,
    created_at    REAL NOT NULL,
    status        TEXT NOT NULL,          -- awaiting | posted | dropped | expired
    cls           TEXT NOT NULL,
    confidence    TEXT NOT NULL,
    src_channel   TEXT NOT NULL,
    src_ts        TEXT NOT NULL,
    src_thread_ts TEXT,
    src_author    TEXT,
    src_excerpt   TEXT,
    reply_text    TEXT,
    jira_summary  TEXT,
    jira_key      TEXT,
    card_channel  TEXT,
    card_ts       TEXT,
    resolved_at   REAL
);
-- Поиск идёт по карточке (пришла реакция — чей это черновик?)
-- и по исходному сообщению (не разбирали ли мы его уже?).
CREATE INDEX IF NOT EXISTS drafts_card ON drafts (card_channel, card_ts);
CREATE UNIQUE INDEX IF NOT EXISTS drafts_src ON drafts (src_channel, src_ts);

-- Каждое сообщение, дошедшее до модели, независимо от вердикта.
-- Черновик остаётся только от разговорного меньшинства, а платим мы за все:
-- без этой таблицы добор истории при каждом старте гонит через Claude одни
-- и те же сутки болтовни заново.
CREATE TABLE IF NOT EXISTS seen (
    channel TEXT NOT NULL,
    ts      TEXT NOT NULL,
    at      REAL NOT NULL,
    PRIMARY KEY (channel, ts)
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _connect() as conn:
        conn.executescript(SCHEMA)


def already_seen(channel: str, ts: str) -> bool:
    """Разбирали ли мы это сообщение раньше.

    Вторая линия защиты после реакции-метки: Slack умеет доставлять
    событие повторно, и без этой проверки владелец получит две одинаковые
    карточки на одно сообщение.

    Смотрим в обе таблицы. Метка-реакция остаётся только на сообщениях,
    из которых вышел черновик, а деньги модель берёт за каждое: болтовня,
    разобранная и признанная болтовнёй, обязана остаться разобранной и
    после перезапуска.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM drafts WHERE src_channel = ? AND src_ts = ? "
            "UNION ALL SELECT 1 FROM seen WHERE channel = ? AND ts = ? LIMIT 1",
            (channel, ts, channel, ts),
        ).fetchone()
    return row is not None


def mark_seen(channel: str, ts: str) -> None:
    """Запомнить, что сообщение уже проходило через классификатор."""
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO seen (channel, ts, at) VALUES (?, ?, ?)",
            (channel, ts, time.time()),
        )


def purge_seen_older_than(days: float) -> int:
    """Подчистить журнал разобранного.

    Держать его вечно незачем: добор истории смотрит на последние
    BACKFILL_HOURS часов, всё, что старше, второй раз не всплывёт.
    """
    cutoff = time.time() - days * 86400
    with _connect() as conn:
        cursor = conn.execute("DELETE FROM seen WHERE at < ?", (cutoff,))
        return cursor.rowcount


def create(**fields) -> str:
    draft_id = uuid.uuid4().hex[:4]
    with _connect() as conn:
        conn.execute(
            """INSERT INTO drafts
               (id, created_at, status, cls, confidence, src_channel, src_ts,
                src_thread_ts, src_author, src_excerpt, reply_text, jira_summary)
               VALUES (?, ?, 'awaiting', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                draft_id,
                time.time(),
                fields["cls"],
                fields["confidence"],
                fields["src_channel"],
                fields["src_ts"],
                fields.get("src_thread_ts"),
                fields.get("src_author"),
                fields.get("src_excerpt"),
                fields.get("reply_text"),
                fields.get("jira_summary"),
            ),
        )
    return draft_id


def attach_card(draft_id: str, channel: str, ts: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE drafts SET card_channel = ?, card_ts = ? WHERE id = ?",
            (channel, ts, draft_id),
        )


def by_card(channel: str, ts: str) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM drafts WHERE card_channel = ? AND card_ts = ?",
            (channel, ts),
        ).fetchone()


def by_id(draft_id: str) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM drafts WHERE id = ?", (draft_id,)
        ).fetchone()


def awaiting() -> list[sqlite3.Row]:
    """Черновики, ждущие решения, — новые первыми."""
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM drafts WHERE status = 'awaiting' ORDER BY created_at DESC"
        ).fetchall()


def by_id_prefix(prefix: str) -> sqlite3.Row | None:
    """Найти черновик по началу идентификатора.

    Владелец печатает id с телефона и вполне может ошибиться регистром или
    оборвать его на середине, поэтому ищем по префиксу, а не по точному
    совпадению. Неоднозначный префикс считаем ненайденным: лучше переспросить,
    чем запостить не тот ответ в рабочий канал.
    """
    prefix = prefix.strip().lower()
    if not prefix:
        return None
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM drafts WHERE id LIKE ? AND status = 'awaiting'",
            (prefix + "%",),
        ).fetchall()
    return rows[0] if len(rows) == 1 else None


def resolve(draft_id: str, status: str, jira_key: str | None = None) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE drafts SET status = ?, jira_key = ?, resolved_at = ? WHERE id = ?",
            (status, jira_key, time.time(), draft_id),
        )


def update_reply(draft_id: str, reply_text: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE drafts SET reply_text = ? WHERE id = ?", (reply_text, draft_id)
        )


def expire_older_than(hours: float) -> list[sqlite3.Row]:
    """Отменить карточки, провисевшие без ответа дольше срока."""
    cutoff = time.time() - hours * 3600
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM drafts WHERE status = 'awaiting' AND created_at < ?",
            (cutoff,),
        ).fetchall()
        conn.execute(
            "UPDATE drafts SET status = 'expired', resolved_at = ? "
            "WHERE status = 'awaiting' AND created_at < ?",
            (time.time(), cutoff),
        )
    return rows
