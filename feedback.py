"""Дообучение на отметках «Это не спам».

Что происходит, когда владелец нажимает кнопку:
  1. Нормализованный текст сообщения (или текст, из-за которого заподозрили группу/канал) сохраняется в таблицу
     `feedback`: только то, что человек сам отметил, и только после нажатия кнопки. Остальные тексты по-прежнему не хранятся.
  2. Сразу для этого владельца такое сообщение больше не помечается (точное совпадение), даже если сработали правила.
  3. Если пример подтвердил администратор или ≥ MIN_USERS разных пользователей, он попадает в общее обучение:
     модель переобучается в фоне (базовые данные + подтверждённые примеры) и подменяется без остановки бота.
     Порог нужен, чтобы любой желающий не мог «научить» модель считать казино нормальным.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time

from classifier import SpamGuard, normalize

log = logging.getLogger("feedback")

MAX_LEN = 500
MIN_LEN = 4


class Feedback:
    def __init__(self, db: sqlite3.Connection, guard: SpamGuard, admin_ids: set[int] | None = None, *,
                 min_users: int = 2, repeat: int = 5, label: str = "ok"):
        self.db, self.guard, self.admin_ids = db, guard, set(admin_ids or ())
        self.min_users, self.repeat, self.label = min_users, repeat, label
        self._lock = asyncio.Lock()
        self._dirty = False
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS feedback(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, owner_id INT, norm TEXT, label TEXT, source TEXT,
                UNIQUE(owner_id, norm));
            """
        )

    # ---- сохранение
    def add(self, owner_id: int, texts: list[str], source: str = "chat") -> int:
        """Запоминает отметки владельца. Возвращает, сколько новых примеров сохранено."""
        n = 0
        for t in texts:
            norm = normalize(t or "")[:MAX_LEN].strip()
            if len(norm) < MIN_LEN:
                continue
            cur = self.db.execute("INSERT OR IGNORE INTO feedback(ts, owner_id, norm, label, source) VALUES (?,?,?,?,?)",
                                  (time.time(), owner_id, norm, self.label, source))
            n += cur.rowcount
            self.guard.allow_owner.setdefault(owner_id, set()).add(norm)
        self.db.commit()
        if n:
            self._refresh_global()
            self._dirty = True
        return n

    def forget(self, owner_id: int) -> None:
        """Команда /stop: тексты пользователя удаляются (уже обученная модель сохраняет общий эффект подтверждённых примеров)."""
        self.db.execute("DELETE FROM feedback WHERE owner_id=?", (owner_id,))
        self.db.commit()
        self.guard.allow_owner.pop(owner_id, None)
        self._refresh_global()

    # ---- общая база
    def promoted(self) -> list[str]:
        """Тексты, подтверждённые администратором или не менее чем min_users разными пользователями."""
        rows = self.db.execute("SELECT norm, owner_id FROM feedback WHERE label=?", (self.label,)).fetchall()
        owners: dict[str, set[int]] = {}
        for norm, owner in rows:
            owners.setdefault(norm, set()).add(owner)
        return [n for n, o in owners.items() if len(o) >= self.min_users or (o & self.admin_ids)]

    def _refresh_global(self) -> None:
        self.guard.allow_global = set(self.promoted())

    def load(self) -> None:
        """При старте бота: восстанавливает «память» из базы."""
        for norm, owner in self.db.execute("SELECT norm, owner_id FROM feedback WHERE label=?", (self.label,)):
            self.guard.allow_owner.setdefault(owner, set()).add(norm)
        self._refresh_global()

    async def retrain_if_needed(self, force: bool = False) -> bool:
        """Переобучает модель в потоке, если появились новые подтверждённые примеры. Параллельные вызовы склеиваются."""
        if not (self._dirty or force):
            return False
        if self._lock.locked():
            return False  # уже идёт обучение; _dirty останется, следующий вызов доучит
        async with self._lock:
            self._dirty = False
            extra = [(n, self.label) for n in self.promoted()]
            if not extra and not force:
                return False
            try:
                n = await asyncio.to_thread(self.guard.retrain, extra, self.repeat)
            except Exception:
                log.exception("не удалось дообучить модель")
                self._dirty = True
                return False
            log.info("модель дообучена: %s примеров (из них подтверждённых отметок: %s)", n, len(extra))
            return True

    def stats(self) -> dict:
        total = self.db.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
        owners = self.db.execute("SELECT COUNT(DISTINCT owner_id) FROM feedback").fetchone()[0]
        return {"marks": total, "owners": owners, "promoted": len(self.promoted()), "model_version": self.guard.model_version}
