from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.utils.clickup.export_task import build_doc_page_index, parse_doc_url

_CLICKUP_URL_PATTERN = re.compile(r"https?://[a-zA-Z0-9.-]*clickup\.com/\S+")


@dataclass(frozen=True, slots=True)
class ResolvedClickUpLink:
    url: str
    local_path: str


def resolve_clickup_doc_links(
    message: str,
    docs_dir: Path,
    base_dir: Path,
) -> list[ResolvedClickUpLink]:
    """从消息文本中提取 ClickUp 文档链接，解析为本地文件的相对路径。

    Args:
        message: 用户消息原文。
        docs_dir: 本地 docs 目录的绝对路径（如 runtime shadow root 下的 coinex/knowledge/requirements/docs）。
        base_dir: agent shadow root，用于计算沙箱相对路径。

    Returns:
        成功解析的链接列表；未命中的链接会被跳过。
    """
    urls = _CLICKUP_URL_PATTERN.findall(message)
    if not urls or not docs_dir.exists():
        return []

    index = build_doc_page_index(docs_dir)
    if not index:
        return []

    results: list[ResolvedClickUpLink] = []
    seen: set[str] = set()

    for url in urls:
        cleaned = url.rstrip(",.;:!?\"')>]}")
        if cleaned in seen:
            continue
        seen.add(cleaned)

        parsed = parse_doc_url(cleaned)
        if parsed is None:
            continue

        page_id = parsed["page_id"]
        local_path = index.get(page_id)
        if local_path is None:
            continue

        try:
            rel = "/" + local_path.absolute().relative_to(base_dir.absolute()).as_posix()
        except ValueError:
            rel = str(local_path)

        results.append(ResolvedClickUpLink(url=cleaned, local_path=rel))

    return results
