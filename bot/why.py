"""Почему бот промолчал: журнал последних решений.

Запуск из корня репозитория:  python bot/why.py [сколько]

Молчание бота выглядит одинаково, что бы за ним ни стояло: сообщение
сочли болтовнёй, в треде уже шёл разговор, модель не дала текста, кончился
потолок вызовов. Отличить одно от другого по каналу невозможно, а вопрос
«почему он не ответил?» возникает первым. Этот журнал на него отвечает.
"""

from __future__ import annotations

import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

import store  # noqa: E402


def when(value) -> str:
    return datetime.datetime.fromtimestamp(float(value)).strftime("%d.%m %H:%M:%S")


def main(limit: int) -> None:
    store.init()
    rows = store.decisions(limit)
    if not rows:
        print("Журнал пуст: бот ещё ничего не разбирал.")
        return

    drafts = {(d["src_channel"], d["src_ts"]): d for d in _drafts()}

    print(f"{'когда':<15} {'класс':<11} {'увер.':<9} что сделал")
    print("-" * 78)
    for row in rows:
        draft = drafts.get((row["channel"], row["ts"]))
        outcome = row["outcome"] or "—"
        if draft is not None:
            outcome = f"{outcome} · черновик {draft['id']} ({draft['status']})"
        print(f"{when(row['at']):<15} {(row['cls'] or '—'):<11} "
              f"{(row['confidence'] or '—'):<9} {outcome}")
        if row["reason"]:
            print(f"{'':<15} причина: {row['reason']}")
        if draft is not None and draft["src_excerpt"]:
            print(f"{'':<15} «{draft['src_excerpt'][:70]}»")


def _drafts():
    with store._connect() as conn:
        return conn.execute("SELECT * FROM drafts").fetchall()


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 20)
