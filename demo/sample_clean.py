"""示例：一个写得比较好的模块，用于对比「低噪声」的审查结果。

Agent 应该能够识别出这是优质代码，并明确指出亮点，
而不是为了凑数硬报一堆无关痛痒的问题（抑制误报能力同样重要）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional


@dataclass
class Task:
    """一条待办任务。"""

    title: str
    priority: int = 0
    done: bool = False
    tags: list[str] = field(default_factory=list)


class TaskStore:
    """基于 JSON 文件的简单任务仓库。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._tasks: list[Task] = []

    # ------------------------------------------------------------------ #
    def load(self) -> list[Task]:
        """从磁盘加载任务；文件不存在时返回空列表。"""
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"任务文件格式损坏：{self.path}") from exc

        self._tasks = [
            Task(
                title=str(item.get("title", "")),
                priority=int(item.get("priority", 0)),
                done=bool(item.get("done", False)),
                tags=list(item.get("tags") or []),
            )
            for item in raw
        ]
        return self._tasks

    def save(self) -> None:
        """把任务写回磁盘（先写临时文件再替换，避免写入中断损坏数据）。"""
        payload = [
            {"title": t.title, "priority": t.priority, "done": t.done, "tags": t.tags}
            for t in self._tasks
        ]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    # ------------------------------------------------------------------ #
    def add(self, title: str, priority: int = 0, tags: Optional[Iterable[str]] = None) -> Task:
        """新增任务；标题为空时抛出异常。"""
        if not title.strip():
            raise ValueError("任务标题不能为空")
        task = Task(title=title.strip(), priority=priority, tags=list(tags or []))
        self._tasks.append(task)
        return task

    def pending(self) -> list[Task]:
        """按优先级倒序返回未完成任务。"""
        return sorted(
            (task for task in self._tasks if not task.done),
            key=lambda task: task.priority,
            reverse=True,
        )

    def complete(self, title: str) -> bool:
        """把指定标题的任务标记为完成；找不到时返回 False。"""
        for task in self._tasks:
            if task.title == title:
                task.done = True
                return True
        return False

    def summary(self) -> dict[str, int]:
        """返回统计信息。"""
        total = len(self._tasks)
        finished = sum(1 for task in self._tasks if task.done)
        return {"total": total, "done": finished, "pending": total - finished}
