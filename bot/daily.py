"""Ежедневная сводка и уборка просроченных черновиков.

Единственная часть бота, которая работает не от событий, а по часам.
Ради неё же удаётся отказаться от отдельного часового обхода: постоянному
процессу нужен свой будильник, иначе сводки не будет вовсе, а карточки
без ответа провисят вечно.
"""

from __future__ import annotations

import logging
import time

import filters
import store

log = logging.getLogger("glebot.daily")


def collect_digest_channels(client, channels: dict[str, str], *, hours: int,
                            owner_id: str, bot_id: str) -> list[str]:
    """По строке на канал, за которым бот только наблюдает.

    Содержательное отделяется тем же дешёвым фильтром, что и в основном
    потоке: если после него ничего не осталось, канал в сводку не попадает
    и не создаёт строку «ничего не произошло».
    """
    oldest = str(int(time.time()) - hours * 3600)
    lines = []

    for channel_id, name in channels.items():
        try:
            history = client.conversations_history(
                channel=channel_id, oldest=oldest, limit=50
            )["messages"]
        except Exception:
            log.exception("история %s", name)
            continue

        meaningful = [
            m for m in history
            if filters.worth_classifying(m, owner_id=owner_id, bot_id=bot_id)
        ]
        if not meaningful:
            continue

        latest = meaningful[0].get("text", "").replace("\n", " ")[:120]
        lines.append(f"• #{name} — {len(meaningful)} сообщений, последнее: «{latest}…»")

    return lines


def expire_stale(client, *, inbox: str, hours: float) -> list:
    """Отменить карточки, провисевшие без ответа дольше срока.

    Молча отменять нельзя: владелец должен узнать, что вопрос протух, —
    иначе он будет считать, что бот всё ещё чего-то ждёт.
    """
    expired = store.expire_older_than(hours)
    for draft in expired:
        if not draft["card_channel"] or not draft["card_ts"]:
            continue
        try:
            client.chat_postMessage(
                channel=draft["card_channel"],
                thread_ts=draft["card_ts"],
                text=(
                    f"Отменил: карточка провисела без ответа больше "
                    f"{int(hours)} часов. Если тема ещё жива, скажи — соберу заново."
                ),
            )
        except Exception:
            log.exception("уведомление об истечении %s", draft["id"])
    return expired


def build_summary(digest_lines: list[str], expired_count: int) -> str | None:
    """Собрать текст сводки. None — если говорить не о чем."""
    posted = dropped = 0
    with store._connect() as conn:
        day_ago = time.time() - 86400
        posted = conn.execute(
            "SELECT COUNT(*) FROM drafts WHERE status = 'posted' AND resolved_at > ?",
            (day_ago,),
        ).fetchone()[0]
        dropped = conn.execute(
            "SELECT COUNT(*) FROM drafts WHERE status = 'dropped' AND resolved_at > ?",
            (day_ago,),
        ).fetchone()[0]
        # Отозванные автоответы считаем отдельно: это не «отменил черновик»,
        # а «сказал и забрал слова назад», и владельцу важно видеть, как
        # часто такое случается — по этому числу и решается, оставлять ли
        # автоответ включённым.
        undone = conn.execute(
            "SELECT COUNT(*) FROM drafts WHERE status = 'undone' AND resolved_at > ?",
            (day_ago,),
        ).fetchone()[0]

    pending = store.awaiting()

    if not (digest_lines or pending or posted or dropped or expired_count or undone):
        return None

    parts = [":robot_face: *Сводка за сутки*"]

    if pending:
        parts.append(f"\n*Ждёт решения ({len(pending)}):*")
        for draft in pending:
            excerpt = (draft["src_excerpt"] or "").replace("\n", " ")[:80]
            parts.append(f"• `{draft['id']}` · {draft['cls']} · «{excerpt}…»")

    if digest_lines:
        parts.append(f"\n*Обсуждают без тебя:*")
        parts.extend(digest_lines)

    tail = f"\n*Сделано:* запощено {posted}, отменено {dropped}"
    if expired_count:
        tail += f", протухло {expired_count}"
    parts.append(tail)

    return "\n".join(parts)


def run(client, *, inbox: str, digest_channels: dict[str, str], owner_id: str,
        bot_id: str, expiry_hours: float = 24, window_hours: int = 24) -> None:
    """Один прогон будильника."""
    # Журнал разобранного нужен ровно на глубину добора истории; недельного
    # запаса хватает с большим краем, а база остаётся маленькой.
    purged = store.purge_seen_older_than(7)
    if purged:
        log.info("журнал разобранного: вычищено %s записей", purged)

    expired = expire_stale(client, inbox=inbox, hours=expiry_hours)
    lines = collect_digest_channels(
        client, digest_channels, hours=window_hours,
        owner_id=owner_id, bot_id=bot_id,
    )
    summary = build_summary(lines, len(expired))
    if summary:
        client.chat_postMessage(channel=inbox, text=summary)
        log.info("сводка отправлена")
    else:
        log.info("сводка пропущена — говорить не о чем")
