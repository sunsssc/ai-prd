from __future__ import annotations

import re


CLICKUP_TASK_LINK_RE = re.compile(r"https?://(?:app|acme)\.clickup\.com/t/(?:[A-Za-z0-9_-]+/)?([A-Za-z0-9_-]+)", re.IGNORECASE)
# 兼容 PR 描述里只写 "Clickup: 86eybp7yz"、"需求：86eybp7yz"、"需求：clickup 86eybp7yz" 这类不带完整链接的写法。
CLICKUP_TASK_LABEL_RE = re.compile(
    r"(?:clickup|需求)\s*[:：]?\s*(?:clickup\s*[:：]?\s*)?(?!https?://)([A-Za-z0-9_-]{5,})",
    re.IGNORECASE,
)


def extract_clickup_task_ids(text: str) -> tuple[str, ...]:
    task_ids: list[str] = []
    seen: set[str] = set()
    body = text or ""
    for pattern in (CLICKUP_TASK_LINK_RE, CLICKUP_TASK_LABEL_RE):
        for match in pattern.finditer(body):
            task_id = match.group(1).strip()
            if task_id and task_id not in seen:
                seen.add(task_id)
                task_ids.append(task_id)
    return tuple(task_ids)
