"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

除了业务表，这里还承担两件与订阅推送相关的基础能力：
- 一把可重入锁：推送批次判定、记录落库、排期推进必须在同一临界区里完成，
  否则并发请求会对同一个业务批次各写一条记录；
- 共享快照槽：用户洞察页与订阅推送都从这里读同一份洞察报告快照，
  快照失效后由下一次读取统一重建，避免两边各算一份。
"""
from __future__ import annotations

from contextlib import contextmanager
from threading import RLock
from typing import Any, Iterator

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._lock = RLock()
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._snapshot_versions: dict[str, int] = {}

    @contextmanager
    def lock(self) -> Iterator[None]:
        """业务临界区：订阅推送的“查重 + 建记录 + 推进排期”整体持锁。"""
        with self._lock:
            yield

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def find_by(self, module: str, field: str, value: Any) -> dict[str, Any] | None:
        for row in self.rows(module):
            if row.get(field) == value:
                return row
        return None

    # ---- 共享快照 ----
    def cached_snapshot(self, name: str) -> dict[str, Any] | None:
        return self._snapshots.get(name)

    def next_snapshot_version(self, name: str) -> int:
        version = self._snapshot_versions.get(name, 0) + 1
        self._snapshot_versions[name] = version
        return version

    def save_snapshot(self, name: str, snapshot: dict[str, Any]) -> dict[str, Any]:
        self._snapshots[name] = snapshot
        return snapshot

    def invalidate_snapshot(self, name: str) -> None:
        self._snapshots.pop(name, None)

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
