"""
导出 ClickUp 任务（单个、List 或整个 Folder）为 AI 友好的本地 Markdown 文件。

用法:
    # 导出单个任务
    python export_task.py --task <task_id_or_url>

    # 导出某个 List 下的所有任务
    python export_task.py --list <list_id_or_url> [--output-dir <dir>]

    # 导出整个 Folder 下的所有任务
    python export_task.py --folder <folder_id_or_url> [--output-dir <dir>]

常用选项:
    --output-file   指定输出文件路径（仅 --task 模式有效）
    --output-dir    指定输出目录
    --show-raw      同时输出原始 ClickUp JSON，便于排查字段结构

环境变量 (可写入同目录 .env 或 .env.local):
    CLICKUP_API_TOKEN        必填
    CLICKUP_API_BASE_URL     默认 https://api.clickup.com/api/v2
    CLICKUP_API_V3_BASE_URL  默认 https://api.clickup.com/api/v3

默认输出:
    Task: workspace/knowledge/requirements/tasks
    Doc:  workspace/knowledge/requirements/docs
"""

import hashlib
import json
import mimetypes
import os
import re
from pathlib import Path
from typing import Any, List, Optional
from urllib.parse import quote, urlparse

import click
import requests
from dotenv import load_dotenv
from app.utils.clickup.markdown_image_paths import build_relative_image_reference


# ---------------------------------------------------------------------------
# 环境变量加载
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[5]  # source/backend/app/utils/clickup -> repo root
_SCRIPTS_CLICKUP_DIR = _REPO_ROOT / "source" / "scripts" / "clickup"
DEFAULT_REQUIREMENTS_ROOT = _REPO_ROOT / "workspace" / "knowledge" / "requirements"
DEFAULT_REQUIREMENTS_TASKS_DIR = DEFAULT_REQUIREMENTS_ROOT / "tasks"
DEFAULT_REQUIREMENTS_DOCS_DIR = DEFAULT_REQUIREMENTS_ROOT / "docs"
_DEFAULT_ENV_FILES = (_SCRIPTS_CLICKUP_DIR / ".env", _SCRIPTS_CLICKUP_DIR / ".env.local")
_ENV_LOADED = False


def _load_env() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    for env_file in _DEFAULT_ENV_FILES:
        if env_file.exists():
            load_dotenv(env_file, override=False)
    _ENV_LOADED = True


def _require_env_vars(*names: str) -> None:
    missing = [n for n in names if not os.getenv(n)]
    if missing:
        raise click.ClickException(f"缺少环境变量: {', '.join(missing)}")


# ---------------------------------------------------------------------------
# URL / ID 解析
# ---------------------------------------------------------------------------

# 单任务链接: https://app.clickup.com/t/<task_id>
CLICKUP_TASK_URL_PATTERN = re.compile(
    r"https://\w+\.clickup\.com/t/(?P<task_id>[A-Za-z0-9]+)"
)

# Folder 链接: https://acme.clickup.com/{workspace}/v/o/f/{folder_id}
# 也兼容 /v/dc/f/ 等变体，只要路径里有 /f/<digits>
CLICKUP_FOLDER_URL_PATTERN = re.compile(
    r"https://\w+\.clickup\.com/(?P<workspace_id>\d+)/[^?#]*/f/(?P<folder_id>\d+)"
)

# List 链接: https://acme.clickup.com/{workspace}/v/l/{list_id}
CLICKUP_LIST_URL_PATTERN = re.compile(
    r"https://\w+\.clickup\.com/(?P<workspace_id>\d+)/[^?#]*/l/(?P<list_id>[A-Za-z0-9-]+)"
)

# 任务内容里的 ClickUp 链接（用于关联文档抓取）
CLICKUP_URL_PATTERN = re.compile(r"""https://[^\s\])>"']*clickup\.com/[^\s\])>"']+""")


def extract_task_id(value: str) -> str:
    value = value.strip()
    m = CLICKUP_TASK_URL_PATTERN.search(value)
    if m:
        return m.group("task_id")
    if re.fullmatch(r"[A-Za-z0-9]+", value):
        return value
    raise click.ClickException(
        "请传入 ClickUp task_id 或形如 https://app.clickup.com/t/<task_id> 的任务链接"
    )


def extract_folder_id(value: str) -> str:
    value = value.strip()
    m = CLICKUP_FOLDER_URL_PATTERN.search(value)
    if m:
        return m.group("folder_id")
    if re.fullmatch(r"\d+", value):
        return value
    raise click.ClickException(
        "请传入 ClickUp folder_id（纯数字）或 Folder 页面链接，"
        "如 https://acme.clickup.com/9000000001/v/o/f/90100000001"
    )


def extract_list_id(value: str) -> str:
    value = value.strip()
    m = CLICKUP_LIST_URL_PATTERN.search(value)
    if m:
        return m.group("list_id")
    if re.fullmatch(r"[A-Za-z0-9-]+", value):
        return value
    raise click.ClickException(
        "请传入 ClickUp list_id 或形如 "
        "https://acme.clickup.com/9000000001/v/l/8cabcde-6638 的 List 链接"
    )


def extract_list_workspace_id(value: str) -> Optional[str]:
    m = CLICKUP_LIST_URL_PATTERN.search(value.strip())
    if not m:
        return None
    return m.group("workspace_id")


def parse_doc_url(url: str) -> Optional[dict]:
    parsed = urlparse(url)
    if "clickup.com" not in parsed.netloc:
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 3 or not parts[0].isdigit():
        return None
    if len(parts) >= 5 and parts[1:3] == ["v", "dc"]:
        doc_id, page_id = parts[3], parts[4]
    elif len(parts) >= 4 and parts[1] == "docs":
        doc_id, page_id = parts[2], parts[3]
    else:
        return None
    return {"url": url, "workspace_id": parts[0], "doc_id": doc_id, "page_id": page_id}


# ---------------------------------------------------------------------------
# ClickUp API 基础
# ---------------------------------------------------------------------------

def _headers() -> dict:
    token = os.getenv("CLICKUP_API_TOKEN")
    if not token:
        raise click.ClickException("缺少 CLICKUP_API_TOKEN 环境变量")
    return {"Authorization": token, "Content-Type": "application/json"}


def _base_v2() -> str:
    return os.getenv("CLICKUP_API_BASE_URL", "https://api.clickup.com/api/v2").rstrip("/")


def _base_v3() -> str:
    return os.getenv("CLICKUP_API_V3_BASE_URL", "https://api.clickup.com/api/v3").rstrip("/")


def _get(url: str, params: Optional[dict] = None, label: str = "") -> dict:
    r = requests.get(url, headers=_headers(), params=params, timeout=30)
    if r.status_code >= 400:
        raise click.ClickException(
            f"ClickUp API 请求失败{(' (' + label + ')') if label else ''}: "
            f"HTTP {r.status_code} {r.text[:500]}"
        )
    return r.json()


# ---------------------------------------------------------------------------
# ClickUp API — 任务
# ---------------------------------------------------------------------------

def fetch_task(task_id: str) -> dict:
    data = _get(
        f"{_base_v2()}/task/{task_id}",
        params={"include_markdown_description": "true"},
        label=f"task/{task_id}",
    )
    cr = requests.get(
        f"{_base_v2()}/task/{task_id}/comment", headers=_headers(), timeout=30
    )
    data["comments"] = cr.json().get("comments") or [] if cr.status_code < 400 else []
    return data


# ---------------------------------------------------------------------------
# ClickUp API — Folder / List（批量）
# ---------------------------------------------------------------------------

def fetch_folder_info(folder_id: str) -> dict:
    return _get(f"{_base_v2()}/folder/{folder_id}", label=f"folder/{folder_id}")


def fetch_folder_lists(folder_id: str) -> List[dict]:
    data = _get(f"{_base_v2()}/folder/{folder_id}/list", label=f"folder/{folder_id}/list")
    return data.get("lists") or []


def fetch_list_info(list_id: str) -> dict:
    return _get(f"{_base_v2()}/list/{list_id}", label=f"list/{list_id}")


def fetch_workspace_lists(workspace_id: str) -> List[dict]:
    """枚举 workspace 下所有 Space/Folder/List，用于把前端 List URL 映射到 API List ID。"""
    spaces_data = _get(
        f"{_base_v2()}/team/{workspace_id}/space",
        params={"archived": "false"},
        label=f"team/{workspace_id}/space",
    )
    lists: List[dict] = []
    for space in spaces_data.get("spaces") or []:
        space_id = space.get("id")
        space_name = space.get("name", "")
        if not space_id:
            continue

        folderless = _get(
            f"{_base_v2()}/space/{space_id}/list",
            params={"archived": "false"},
            label=f"space/{space_id}/list",
        )
        for lst in folderless.get("lists") or []:
            lst["_space_name"] = space_name
            lists.append(lst)

        folders_data = _get(
            f"{_base_v2()}/space/{space_id}/folder",
            params={"archived": "false"},
            label=f"space/{space_id}/folder",
        )
        for folder in folders_data.get("folders") or []:
            folder_id = folder.get("id")
            folder_name = folder.get("name", "")
            if not folder_id:
                continue
            folder_lists = fetch_folder_lists(str(folder_id))
            for lst in folder_lists:
                lst["_space_name"] = space_name
                lst["_folder_name"] = folder_name
                lists.append(lst)
    return lists


def resolve_workspace_list_by_name(workspace_id: str, list_name: str) -> dict:
    expected = list_name.strip().lower()
    workspace_lists = fetch_workspace_lists(workspace_id)
    matches = [
        lst for lst in workspace_lists
        if _normalize(lst.get("name")).lower() == expected
    ]
    if not matches:
        matches = [
            lst for lst in workspace_lists
            if expected in _normalize(lst.get("name")).lower()
        ]
    if not matches:
        candidate_names = sorted({
            _normalize(lst.get("name"))
            for lst in workspace_lists
            if _normalize(lst.get("name"))
        })
        shown = "；".join(candidate_names[:80])
        raise click.ClickException(
            f"workspace {workspace_id} 下没有找到名为「{list_name}」的 List。候选 List: {shown}"
        )
    if len(matches) > 1:
        locations = [
            f"{lst.get('_space_name', '')}/{lst.get('_folder_name', '')}/{lst.get('name', '')} ({lst.get('id', '')})"
            for lst in matches
        ]
        raise click.ClickException(
            f"workspace {workspace_id} 下存在多个名为「{list_name}」的 List，无法唯一确定: "
            + "；".join(locations)
        )
    return matches[0]


def fetch_list_tasks(list_id: str, include_subtasks: bool = True) -> List[dict]:
    """拉取 List 下所有 Task（自动翻页）。"""
    tasks: List[dict] = []
    page = 0
    while True:
        params: dict = {
            "page": page,
            "include_closed": "true",
            "include_markdown_description": "true",
        }
        if include_subtasks:
            params["subtasks"] = "true"
        data = _get(
            f"{_base_v2()}/list/{list_id}/task",
            params=params,
            label=f"list/{list_id}/task page={page}",
        )
        batch = data.get("tasks") or []
        tasks.extend(batch)
        # ClickUp 分页：返回空列表或 last_page=true 时结束
        if not batch or data.get("last_page") is True:
            break
        page += 1
    return tasks


def _fetch_task_comments(task_id: str) -> list:
    cr = requests.get(
        f"{_base_v2()}/task/{task_id}/comment", headers=_headers(), timeout=30
    )
    return cr.json().get("comments") or [] if cr.status_code < 400 else []


def fetch_list_all_tasks(list_id: str, list_name: Optional[str] = None) -> List[dict]:
    """拉取 List 下所有 Task（含评论）。"""
    raw_tasks = fetch_list_tasks(list_id)
    for rt in raw_tasks:
        task_id = rt.get("id", "")
        rt["comments"] = _fetch_task_comments(task_id)
        rt["_list_name"] = list_name or rt.get("_list_name") or list_id
    return raw_tasks


def fetch_folder_all_tasks(folder_id: str) -> List[dict]:
    """拉取 Folder 下所有 List 的所有 Task（含评论）。"""
    lists = fetch_folder_lists(folder_id)
    if not lists:
        return []

    all_tasks: List[dict] = []
    for lst in lists:
        list_id = lst.get("id", "")
        list_name = lst.get("name", list_id)
        click.echo(f"  拉取 List [{list_name}] ({list_id}) ...", err=True)
        all_tasks.extend(fetch_list_all_tasks(list_id, list_name=list_name))
    return all_tasks


# ---------------------------------------------------------------------------
# ClickUp API — Folder Docs
# ---------------------------------------------------------------------------

def fetch_folder_docs(workspace_id: str, folder_id: str) -> List[dict]:
    """拉取 Folder 下所有 ClickUp Docs（parent_type=5 表示 Folder）。"""
    all_docs: List[dict] = []
    cursor = None
    while True:
        params: dict = {"parent_id": folder_id, "parent_type": 5, "limit": 100}
        if cursor:
            params["cursor"] = cursor
        r = requests.get(
            f"{_base_v3()}/workspaces/{workspace_id}/docs",
            headers=_headers(),
            params=params,
            timeout=30,
        )
        if r.status_code >= 400:
            break
        data = r.json()
        batch = data.get("docs") or []
        all_docs.extend(batch)
        cursor = data.get("next_cursor")
        if not cursor or not batch:
            break
    return all_docs


def _safe_filename(name: str) -> str:
    """将页面名转为安全的文件名（去掉非法字符）。"""
    name = re.sub(r'[\\/:*?"<>|]', "_", name).strip()
    return name or "unnamed"


def _extract_page_tree(pages: list, depth: int = 0, rel_dir: Path = Path(".")) -> List[dict]:
    """
    从 v2 API 的 pages 树中递归提取，返回带路径信息的平铺列表。
    每个条目包含：
      id        : page ID（用于后续从 v3 API 获取 markdown 内容）
      name      : 页面名称
      _depth    : 层级深度（0 = 顶层）
      _rel_dir  : 相对于 doc 根目录的目录路径
      _safe_name: 安全文件名（不含扩展名）
    内容不从 v2 取，后续统一用 v3 API 获取 markdown。
    """
    result = []
    for page in pages:
        safe = _safe_filename(page.get("name", "unnamed"))
        sub_pages = page.get("pages") or []
        entry = {
            "id": page.get("id"),
            "name": page.get("name", ""),
            "_depth": depth,
            "_rel_dir": rel_dir / safe if sub_pages else rel_dir,
            "_safe_name": "_index" if sub_pages else safe,
        }
        result.append(entry)
        if sub_pages:
            # 子页面放到以本页面命名的子目录下
            result.extend(_extract_page_tree(sub_pages, depth + 1, rel_dir / safe))
    return result


def fetch_doc_all_pages(workspace_id: str, doc_id: str) -> List[dict]:
    """
    拉取某个 Doc 的所有 pages（含子页面，递归展开层级）。
    使用 v2 API，一次请求即可获取完整页面树。
    返回带 _depth 的平铺列表，content 为纯文本。
    """
    r = requests.get(
        f"{_base_v2()}/doc/{doc_id}/page",
        headers=_headers(),
        timeout=60,
    )
    if r.status_code >= 400:
        return []
    root_pages = r.json().get("pages") or []
    return _extract_page_tree(root_pages)


# ---------------------------------------------------------------------------
# ClickUp API — Doc 页面
# ---------------------------------------------------------------------------

def fetch_doc_page(workspace_id: str, doc_id: str, page_id: str) -> dict:
    return _get(
        f"{_base_v3()}/workspaces/{workspace_id}/docs/{doc_id}/pages/{page_id}",
        params={"content_format": "text/md"},
        label=f"doc/{doc_id}/page/{page_id}",
    )


def fetch_page_subtree_from_v2(doc_id: str, target_page_id: str) -> Optional[dict]:
    """
    从 v2 API 获取完整 doc 树，返回目标 page 节点（含 pages 子页面字段）。
    可直接传给 _extract_page_tree 以获得带层级的平铺列表。
    """
    r = requests.get(f"{_base_v2()}/doc/{doc_id}/page", headers=_headers(), timeout=60)
    if r.status_code >= 400:
        return None

    def _find(pages: list) -> Optional[dict]:
        for page in pages:
            if page.get("id") == target_page_id:
                return page
            found = _find(page.get("pages") or [])
            if found:
                return found
        return None

    return _find(r.json().get("pages") or [])


# ---------------------------------------------------------------------------
# 文本规范化 & 文档清洗
# ---------------------------------------------------------------------------

def _normalize(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "\n".join(filter(None, (_normalize(i) for i in value)))
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value).strip()


_IMAGE_MD_PATTERN = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_DOC_PAGE_ID_PATTERN = re.compile(r"^- Page ID: `([^`]+)`\s*$", re.MULTILINE)
_DOC_PAGE_INDEX_CACHE: dict[str, dict[str, Path]] = {}


def _clean_doc(content: str, max_lines: int = 200) -> str:
    kept, seen = [], set()
    for raw in content.splitlines():
        # 保留图片行，不经过链接剥除处理
        if raw.strip().startswith("!"):
            img_line = raw.strip()
            if img_line not in seen:
                seen.add(img_line)
                kept.append(img_line)
                if len(kept) >= max_lines:
                    break
            continue
        line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", raw)
        line = re.sub(r"`([^`]+)`", r"\1", line)
        line = re.sub(r"\s+", " ", line).strip()
        if not line or line.startswith("```") or line == "---":
            continue
        if re.fullmatch(r"\|?[\s:\-\|]+\|?", line):
            continue
        if line.lower() in {"目录", "toc", "table of contents"}:
            continue
        if line in seen:
            continue
        seen.add(line)
        kept.append(line)
        if len(kept) >= max_lines:
            break
    return "\n".join(kept) or "未提取到有效文档内容"


def _format_clickup_timestamp(value: Any) -> str:
    text = _normalize(value)
    if not text:
        return ""
    try:
        from datetime import datetime, timezone

        seconds = int(text) / 1000
        return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (TypeError, ValueError, OSError):
        return text


def _format_user_list(users: Any) -> str:
    if not isinstance(users, list):
        return ""
    names = []
    for user in users:
        if not isinstance(user, dict):
            continue
        name = user.get("username") or user.get("name") or user.get("email") or user.get("id")
        if name:
            names.append(str(name))
    return "、".join(names)


def _format_tags(tags: Any) -> str:
    if not isinstance(tags, list):
        return ""
    names = []
    for tag in tags:
        if isinstance(tag, dict):
            name = tag.get("name")
        else:
            name = tag
        if name:
            names.append(str(name))
    return "、".join(names)


def _format_status(status: Any) -> str:
    if isinstance(status, dict):
        return _normalize(status.get("status") or status.get("type") or status)
    return _normalize(status)


def _format_priority(priority: Any) -> str:
    if isinstance(priority, dict):
        return _normalize(priority.get("priority") or priority.get("id") or priority)
    return _normalize(priority)


def _has_custom_field_value(value: Any) -> bool:
    return value not in (None, "", [], {})


def _format_custom_field_option_value(field: dict, value: Any) -> str:
    type_config = field.get("type_config")
    if not isinstance(type_config, dict):
        return ""

    option_by_id = {}
    for option in type_config.get("options") or []:
        if not isinstance(option, dict):
            continue
        option_id = option.get("id")
        option_name = option.get("name") or option.get("label")
        if option_id and option_name:
            option_by_id[str(option_id)] = str(option_name)

    if not option_by_id:
        return ""

    if isinstance(value, list):
        values = [option_by_id.get(str(item), _normalize(item)) for item in value]
        return "、".join(filter(None, values))
    return option_by_id.get(str(value), "")


def _format_custom_field_value(field: dict) -> str:
    value = field.get("value")
    if not _has_custom_field_value(value):
        return ""

    option_value = _format_custom_field_option_value(field, value)
    if option_value:
        return option_value

    field_type = _normalize(field.get("type")).lower()
    if field_type == "date":
        return _format_clickup_timestamp(value)

    if isinstance(value, list):
        users = _format_user_list(value)
        if users:
            return users
        return "、".join(filter(None, (_normalize(item) for item in value)))

    if isinstance(value, dict):
        user = _format_user_list([value])
        if user:
            return user
        for key in ("name", "label", "title", "value", "email", "username", "id"):
            if value.get(key):
                return _normalize(value.get(key))

    if isinstance(value, bool):
        return "是" if value else "否"

    return _normalize(value)


def _escape_table_cell(value: Any) -> str:
    text = _normalize(value)
    return text.replace("|", "\\|").replace("\n", "<br>")


def _format_custom_fields(fields: Any) -> str:
    if not isinstance(fields, list):
        return "无"

    rows = []
    for field in fields:
        if not isinstance(field, dict):
            continue
        name = _normalize(field.get("name") or field.get("id"))
        value = _format_custom_field_value(field)
        if not value:
            continue
        rows.append((name, value))

    if not rows:
        return "无"

    lines = ["| 字段 | 值 |", "| --- | --- |"]
    for name, value in rows:
        lines.append(f"| {_escape_table_cell(name)} | {_escape_table_cell(value)} |")
    return "\n".join(lines)


def _format_task_metadata(task: dict) -> str:
    lines = [
        f"- Task ID: `{task['task_id']}`",
        f"- 标题: {task['title']}",
        f"- 状态: {task['status']}",
    ]
    if task.get("list_name"):
        lines.append(f"- List: {task['list_name']}")
    lines.extend([
        f"- 链接: {task['url']}",
        f"- 负责人: {task['assignees']}",
        f"- 创建人: {task['creator']}",
        f"- 优先级: {task['priority']}",
        f"- 标签: {task['tags']}",
        f"- 创建时间: {task['date_created']}",
        f"- 更新时间: {task['date_updated']}",
        f"- 截止时间: {task['due_date']}",
    ])
    return "\n".join(lines)


def _comment_author(comment: dict) -> str:
    user = comment.get("user")
    if isinstance(user, dict):
        return _normalize(user.get("username") or user.get("name") or user.get("email") or user.get("id")) or "unknown"
    return _normalize(comment.get("author") or comment.get("username") or comment.get("email")) or "unknown"


def _comment_created_at(comment: dict) -> str:
    for key in ("date", "date_created", "created_at", "created"):
        formatted = _format_clickup_timestamp(comment.get(key))
        if formatted:
            return formatted
    return ""


def _format_comments(comments: Any) -> str:
    if not isinstance(comments, list):
        return "无"

    blocks = []
    for comment in comments[:10]:
        if not isinstance(comment, dict):
            continue
        body = _normalize(
            comment.get("comment_text")
            or comment.get("comment")
            or comment.get("text")
            or comment.get("body")
        )
        if not body:
            continue

        header = f"> **{_comment_author(comment)}**"
        created_at = _comment_created_at(comment)
        if created_at:
            header = f"{header} · {created_at}"
        body_lines = [f"> {line}" if line else ">" for line in body.splitlines()]
        blocks.append("\n".join([header, ">", *body_lines]))

    return "\n\n".join(blocks) or "无"


def _build_relative_file_reference(markdown_dir: Path, target_path: Path) -> str:
    relative_path = Path(os.path.relpath(target_path, markdown_dir)).as_posix()
    return quote(relative_path, safe="/")


def _infer_requirements_docs_dir(markdown_dir: Optional[Path]) -> Optional[Path]:
    if markdown_dir is None:
        return None
    current = Path(markdown_dir).resolve()
    for path in [current, *current.parents]:
        if (
            path.name == "requirements"
            and path.parent.name == "knowledge"
            and path.parent.parent.name == "workspace"
        ):
            return path / "docs"
    return None


def build_doc_page_index(docs_dir: Path) -> dict[str, Path]:
    key = docs_dir.resolve().as_posix()
    cached = _DOC_PAGE_INDEX_CACHE.get(key)
    if cached is not None:
        return cached

    index: dict[str, Path] = {}
    if docs_dir.exists():
        for path in docs_dir.rglob("*.md"):
            try:
                head = "\n".join(path.read_text(encoding="utf-8").splitlines()[:20])
            except (OSError, UnicodeDecodeError):
                continue
            match = _DOC_PAGE_ID_PATTERN.search(head)
            if match:
                index[match.group(1)] = path

    _DOC_PAGE_INDEX_CACHE[key] = index
    return index


def invalidate_doc_page_index_cache(docs_dir: Path | None = None) -> None:
    """清除文档页面索引的内存缓存。

    如果指定 docs_dir，只清除该目录的缓存；否则清除全部。
    """
    if docs_dir is None:
        _DOC_PAGE_INDEX_CACHE.clear()
    else:
        _DOC_PAGE_INDEX_CACHE.pop(docs_dir.resolve().as_posix(), None)


def _export_page_entries_to_requirements_docs(
    *,
    workspace_id: str,
    doc_id: str,
    root_page_id: str,
    root_name: str,
    page_entries: list,
    docs_dir: Path,
    markdown_dir: Path,
) -> tuple[list[str], Optional[tuple[str, str]]]:
    docs_dir.mkdir(parents=True, exist_ok=True)
    images_dir = docs_dir / "images"
    index = build_doc_page_index(docs_dir)
    root_existing = index.get(root_page_id)
    doc_dir = (
        root_existing.parent
        if root_existing
        else docs_dir / "_linked" / f"{_safe_filename(root_name)}__{doc_id}_{root_page_id}"
    )

    file_list = []
    root_link: Optional[tuple[str, str]] = None
    for entry in page_entries:
        existing_path = index.get(entry["id"])
        if existing_path and existing_path.exists():
            dest = existing_path
        else:
            page_dir = doc_dir / entry["_rel_dir"]
            page_dir.mkdir(parents=True, exist_ok=True)
            try:
                page_data = fetch_doc_page(workspace_id, doc_id, entry["id"])
                content = _normalize(page_data.get("content") or "")
            except click.ClickException:
                content = ""
            content = _download_images(content, images_dir, page_dir)
            dest = page_dir / f"{entry['_safe_name']}.md"
            dest.write_text(
                _render_doc_page(entry["name"], entry["id"], doc_id, workspace_id, content),
                encoding="utf-8",
            )
            index[entry["id"]] = dest

        indent = "  " * entry["_depth"]
        local_ref = _build_relative_file_reference(markdown_dir, dest)
        if entry["id"] == root_page_id:
            root_link = (entry["name"], local_ref)
        file_list.append(f"{indent}- [{entry['name']}]({local_ref})")

    return file_list, root_link


def _download_images(content: str, images_dir: Path, markdown_dir: Path) -> str:
    """将 Markdown 内容中引用的图片下载到本地，并替换为相对路径。"""
    images_dir.mkdir(parents=True, exist_ok=True)

    def _replace(m: re.Match) -> str:
        alt_text = m.group(1)
        url = m.group(2)
        parsed = urlparse(url)
        if not parsed.scheme.startswith("http"):
            return m.group(0)
        try:
            r = requests.get(url, headers=_headers(), timeout=30)
            if r.status_code >= 400:
                return m.group(0)
            content_type = r.headers.get("content-type", "image/png").split(";")[0].strip()
            ext = mimetypes.guess_extension(content_type) or ".png"
            if ext in (".jpe", ".jpeg"):
                ext = ".jpg"
            filename = hashlib.md5(url.encode()).hexdigest()[:16] + ext
            image_path = images_dir / filename
            image_path.write_bytes(r.content)
            image_ref = build_relative_image_reference(markdown_dir, image_path)
            return f"![{alt_text}]({image_ref})"
        except Exception:
            return m.group(0)

    return _IMAGE_MD_PATTERN.sub(_replace, content)


# ---------------------------------------------------------------------------
# 关联文档抓取
# ---------------------------------------------------------------------------

def _collect_doc_links(task_data: dict) -> list:
    unique: dict = {}

    def collect(value: Any) -> None:
        if isinstance(value, str):
            for m in CLICKUP_URL_PATTERN.finditer(value):
                url = m.group(0).rstrip(".,);]")
                parsed = parse_doc_url(url)
                if parsed:
                    unique[url] = parsed
            return
        if isinstance(value, list):
            for item in value:
                collect(item)
            return
        if isinstance(value, dict):
            for item in value.values():
                collect(item)

    for key in ("description", "text_content", "textContent", "markdown_description", "markdownDescription"):
        collect(task_data.get(key))
    for comment in (task_data.get("comments") or [])[:20]:
        collect(comment)

    return list(unique.values())


_MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]]*)\]\((https://[^\s)]+clickup\.com/[^\s)]+)\)")


def _rewrite_clickup_doc_links(text: str, replacements: dict[str, tuple[str, str]]) -> str:
    if not text or not replacements:
        return text

    def replacement_for(url: str) -> Optional[tuple[str, str]]:
        normalized_url = url.rstrip(".,);]")
        if normalized_url in replacements:
            return replacements[normalized_url]
        parsed = parse_doc_url(normalized_url)
        if not parsed:
            return None
        for source_url, replacement in replacements.items():
            source = parse_doc_url(source_url)
            if source and source["doc_id"] == parsed["doc_id"] and source["page_id"] == parsed["page_id"]:
                return replacement
        return None

    def replace_markdown_link(match: re.Match) -> str:
        label = match.group(1).strip()
        url = match.group(2)
        replacement = replacement_for(url)
        if not replacement:
            return match.group(0)
        title, local_ref = replacement
        next_label = title if not label or "clickup.com" in label else label
        return f"[{next_label}]({local_ref})"

    rewritten = _MARKDOWN_LINK_PATTERN.sub(replace_markdown_link, text)

    def replace_bare_url(match: re.Match) -> str:
        url = match.group(0).rstrip(".,);]")
        replacement = replacement_for(url)
        if not replacement:
            return match.group(0)
        title, local_ref = replacement
        suffix = match.group(0)[len(url):]
        return f"[{title}]({local_ref}){suffix}"

    return CLICKUP_URL_PATTERN.sub(replace_bare_url, rewritten)


def _remove_promoted_reference_doc_lines(text: str, replacements: dict[str, tuple[str, str]]) -> str:
    if not text or not replacements:
        return text

    local_refs = [local_ref for _, local_ref in replacements.values() if local_ref]
    if not local_refs:
        return text

    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            kept.append(line)
            continue

        contains_promoted_doc = any(f"]({local_ref})" in stripped for local_ref in local_refs)
        is_reference_line = re.match(r"^(详细)?(文档)?参考(文档|内容)?[:：]", stripped) is not None
        if contains_promoted_doc and is_reference_line:
            continue
        kept.append(line)

    return "\n".join(kept).strip()


def _build_docs_section(
    task_data: dict,
    linked_docs_dir: Optional[Path] = None,
    images_dir: Optional[Path] = None,
    markdown_dir: Optional[Path] = None,
    requirements_docs_dir: Optional[Path] = None,
) -> str:
    """
    处理 Task 中引用的 ClickUp Doc 链接。
    - requirements_docs_dir 不为 None 时：优先复用或补导出到需求知识库 docs 目录
    - linked_docs_dir 不为 None 时：将每个 doc page 导出为独立 .md 文件（与 folder docs 同一策略）
    - 返回的字符串列出各文档的基本信息和导出路径（或读取失败原因）
    """
    links = _collect_doc_links(task_data)
    if not links:
        return "无"

    sections = []
    for i, link in enumerate(links, 1):
        workspace_id = link["workspace_id"]
        doc_id = link["doc_id"]
        page_id = link["page_id"]

        # 用 v2 获取目标 page 节点（含子页面层级）
        page_node = fetch_page_subtree_from_v2(doc_id, page_id)
        if page_node:
            page_entries = _extract_page_tree([page_node])
        else:
            # fallback: 直接从 v3 获取单个页面
            try:
                page_data = fetch_doc_page(workspace_id, doc_id, page_id)
                name = _normalize(page_data.get("name") or f"文档{i}")
                page_entries = [{
                    "id": page_id, "name": name,
                    "_depth": 0, "_rel_dir": Path("."), "_safe_name": _safe_filename(name),
                }]
            except click.ClickException as e:
                sections.append(f"- 关联文档 {i}: {link['url']}（读取失败: {e.format_message()}）")
                continue

        root_name = page_entries[0]["name"] if page_entries else f"文档{i}"

        if requirements_docs_dir and markdown_dir:
            file_list, root_link = _export_page_entries_to_requirements_docs(
                workspace_id=workspace_id,
                doc_id=doc_id,
                root_page_id=page_id,
                root_name=root_name,
                page_entries=page_entries,
                docs_dir=requirements_docs_dir,
                markdown_dir=markdown_dir,
            )
            if root_link:
                replacements = task_data.setdefault("_local_doc_link_replacements", {})
                replacements[link["url"]] = root_link
            sections.extend(file_list)
        elif linked_docs_dir:
            # 导出为独立文件
            doc_dir = linked_docs_dir / f"{_safe_filename(root_name)}__{doc_id}_{page_id}"
            file_list = []
            root_link: Optional[tuple[str, str]] = None
            for entry in page_entries:
                page_dir = doc_dir / entry["_rel_dir"]
                page_dir.mkdir(parents=True, exist_ok=True)
                try:
                    page_data = fetch_doc_page(workspace_id, doc_id, entry["id"])
                    content = _normalize(page_data.get("content") or "")
                except click.ClickException:
                    content = ""
                if images_dir:
                    content = _download_images(content, images_dir, page_dir)
                dest = page_dir / f"{entry['_safe_name']}.md"
                dest.write_text(
                    _render_doc_page(entry["name"], entry["id"], doc_id, workspace_id, content),
                    encoding="utf-8",
                )
                indent = "  " * entry["_depth"]
                if markdown_dir:
                    local_ref = _build_relative_file_reference(markdown_dir, dest)
                    if entry["id"] == page_id:
                        root_link = (entry["name"], local_ref)
                    file_list.append(f"{indent}- [{entry['name']}]({local_ref})")
                else:
                    file_list.append(f"{indent}- {entry['name']}: {dest}")
            if root_link:
                replacements = task_data.setdefault("_local_doc_link_replacements", {})
                replacements[link["url"]] = root_link
            sections.extend(file_list)
        else:
            # 内嵌模式（仅 --task 单独导出且未指定 output-dir 时使用）
            page_texts = []
            for j, entry in enumerate(page_entries):
                try:
                    page_data = fetch_doc_page(workspace_id, doc_id, entry["id"])
                    content = _normalize(page_data.get("content") or "")
                except click.ClickException:
                    content = ""
                if images_dir:
                    content = _download_images(content, images_dir, markdown_dir or images_dir.parent)
                heading = "###" if j == 0 else "####"
                page_texts.append(f"{heading} {entry['name']}\n\n{content}")
            sections.append(f"- [{root_name}]({link['url']})\n\n" + "\n\n".join(page_texts))

    return "\n".join(sections)


# ---------------------------------------------------------------------------
# 任务规范化 & 渲染
# ---------------------------------------------------------------------------

def normalize_task(
    task_id: str,
    data: dict,
    images_dir: Optional[Path] = None,
    linked_docs_dir: Optional[Path] = None,
    markdown_dir: Optional[Path] = None,
    requirements_docs_dir: Optional[Path] = None,
) -> dict:
    title = _normalize(data.get("name") or data.get("title") or f"ClickUp Task {task_id}")
    resolved_requirements_docs_dir = requirements_docs_dir or _infer_requirements_docs_dir(markdown_dir)
    description = _normalize(
        data.get("markdown_description") or data.get("markdownDescription")
        or data.get("description") or data.get("text_content") or data.get("textContent")
    )
    acceptance = _normalize(
        data.get("acceptance_criteria") or data.get("acceptanceCriteria") or data.get("acceptance")
    )
    comments = _format_comments(data.get("comments"))
    docs_content = _build_docs_section(
        data,
        linked_docs_dir=linked_docs_dir,
        images_dir=images_dir,
        markdown_dir=markdown_dir,
        requirements_docs_dir=resolved_requirements_docs_dir,
    )
    replacements = data.get("_local_doc_link_replacements")
    if isinstance(replacements, dict):
        description = _rewrite_clickup_doc_links(description, replacements)
        description = _remove_promoted_reference_doc_lines(description, replacements)
        acceptance = _rewrite_clickup_doc_links(acceptance, replacements)
        comments = _rewrite_clickup_doc_links(comments, replacements)
        custom_fields = _rewrite_clickup_doc_links(_format_custom_fields(data.get("custom_fields")), replacements)
    else:
        custom_fields = _format_custom_fields(data.get("custom_fields"))

    return {
        "task_id": task_id,
        "title": title,
        "list_name": data.get("_list_name", ""),
        "status": _format_status(data.get("status")) or "未知",
        "url": _normalize(data.get("url") or data.get("task_url")) or f"https://app.clickup.com/t/{task_id}",
        "assignees": _format_user_list(data.get("assignees")) or "未分配",
        "creator": _normalize((data.get("creator") or {}).get("username") if isinstance(data.get("creator"), dict) else data.get("creator")) or "未知",
        "priority": _format_priority(data.get("priority")) or "未设置",
        "tags": _format_tags(data.get("tags")) or "无",
        "date_created": _format_clickup_timestamp(data.get("date_created")) or "未知",
        "date_updated": _format_clickup_timestamp(data.get("date_updated")) or "未知",
        "due_date": _format_clickup_timestamp(data.get("due_date")) or "未设置",
        "custom_fields": custom_fields,
        "description": description or "无描述",
        "acceptance_criteria": acceptance,
        "comments": comments,
        "docs_content": docs_content,
        "raw_json": json.dumps(data, ensure_ascii=False, indent=2),
    }


def render_markdown(task: dict) -> str:
    sections = [
        f"# {task['title']}",
        "## 任务元信息\n\n" + _format_task_metadata(task),
    ]

    if task["description"] != "无描述":
        sections.append("## 核心需求描述\n\n" + task["description"])

    if task["docs_content"] != "无":
        sections.append("## 关联文档\n\n" + task["docs_content"])

    if task["acceptance_criteria"]:
        sections.append("## 验收标准\n\n" + task["acceptance_criteria"])

    if task["custom_fields"] != "无":
        sections.append("## 自定义字段\n\n" + task["custom_fields"])

    if task["comments"] != "无":
        sections.append("## 评论\n\n" + task["comments"])

    return "\n\n".join(sections) + "\n"


# ---------------------------------------------------------------------------
# 导出入口（可被其他脚本 import）
# ---------------------------------------------------------------------------

def export_task(
    task_value: str,
    output_dir: Optional[Path] = None,
    output_file: Optional[Path] = None,
) -> tuple:
    """导出单个 ClickUp 任务，返回 (output_path, task_dict, markdown_str)。"""
    _load_env()
    task_id = extract_task_id(task_value)

    # 先拉取任务数据，再用 title 确定文件名
    raw = fetch_task(task_id)
    title = _normalize(raw.get("name") or raw.get("title") or task_id)

    if output_file:
        dest = Path(output_file)
    else:
        base = Path(output_dir or DEFAULT_REQUIREMENTS_TASKS_DIR)
        dest = base / f"{_safe_filename(title)}.md"

    dest.parent.mkdir(parents=True, exist_ok=True)
    images_dir = dest.parent / "images"
    linked_docs_dir = dest.parent / "linked_docs"
    task = normalize_task(
        task_id,
        raw,
        images_dir=images_dir,
        linked_docs_dir=linked_docs_dir,
        markdown_dir=dest.parent,
    )
    content = render_markdown(task)
    dest.write_text(content, encoding="utf-8")
    return dest, task, content


def _task_output_path(tasks_dir: Path, task: dict) -> Path:
    safe_title = _safe_filename(task["title"])
    return tasks_dir / f"{safe_title}__{task['task_id']}.md"


def export_list(
    list_value: str,
    output_dir: Optional[Path] = None,
) -> List[Path]:
    """
    导出 List 下所有 Task，返回已写入的文件路径列表。
    - Task: <output_dir>/<title>__<task_id>.md
    - 默认输出到 workspace/knowledge/requirements/tasks/<list_name>
    - 关联文档默认复用 workspace/knowledge/requirements/docs
    """
    _load_env()
    list_id = extract_list_id(list_value)
    tasks_dir = output_dir
    try:
        list_info = fetch_list_info(list_id)
    except click.ClickException:
        workspace_id = extract_list_workspace_id(list_value)
        name_hint = output_dir.name if output_dir else ""
        if not workspace_id or not name_hint:
            raise
        list_info = resolve_workspace_list_by_name(workspace_id, name_hint)
        list_id = str(list_info.get("id") or list_id)
    list_name = _normalize(list_info.get("name") or list_id)
    tasks_dir = tasks_dir or (DEFAULT_REQUIREMENTS_TASKS_DIR / _safe_filename(list_name))
    tasks_dir.mkdir(parents=True, exist_ok=True)

    click.echo(f"List: {list_name} ({list_id})", err=True)
    raw_tasks = fetch_list_all_tasks(list_id, list_name=list_name)
    if not raw_tasks:
        click.echo("  没有找到任何 Task。", err=True)
        return []

    images_dir = tasks_dir / "images"
    linked_docs_dir = tasks_dir / "linked_docs"
    written: List[Path] = []
    for raw in raw_tasks:
        task_id = raw.get("id", "unknown")
        try:
            task = normalize_task(
                task_id,
                raw,
                images_dir=images_dir,
                linked_docs_dir=linked_docs_dir,
                markdown_dir=tasks_dir,
            )
            dest = _task_output_path(tasks_dir, task)
            dest.write_text(render_markdown(task), encoding="utf-8")
            written.append(dest)
        except Exception as e:
            click.echo(f"  跳过任务 {task_id}: {e}", err=True)

    click.echo(f"  导出 Tasks: {len(written)} 个", err=True)
    return written


def export_folder(
    folder_value: str,
    output_dir: Optional[Path] = None,
) -> List[Path]:
    """
    导出 Folder 下所有 Task 和 ClickUp Docs，返回已写入的文件路径列表。
    - Task: <output_dir>/tasks/<task_id>.md
    - Doc:  <output_dir>/docs/<doc_name>.md
    - 默认 output_dir 为 workspace/knowledge/requirements
    """
    _load_env()
    folder_id = extract_folder_id(folder_value)

    # 尝试从 URL 中提取 workspace_id，否则从 folder_info 里取
    workspace_id = None
    m = CLICKUP_FOLDER_URL_PATTERN.search(folder_value)
    if m:
        workspace_id = m.group("workspace_id")
    if not workspace_id:
        folder_info = fetch_folder_info(folder_id)
        workspace_id = str((folder_info.get("space") or {}).get("id") or "")
    else:
        folder_info = fetch_folder_info(folder_id)

    folder_name = folder_info.get("name") or folder_id

    folder_dir = output_dir or DEFAULT_REQUIREMENTS_ROOT
    folder_dir.mkdir(parents=True, exist_ok=True)

    click.echo(f"Folder: {folder_name} ({folder_id})", err=True)
    written: List[Path] = []

    # --- 导出 Tasks ---
    raw_tasks = fetch_folder_all_tasks(folder_id)
    if raw_tasks:
        tasks_dir = folder_dir / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        images_dir = tasks_dir / "images"
        linked_docs_dir = tasks_dir / "linked_docs"
        for raw in raw_tasks:
            task_id = raw.get("id", "unknown")
            try:
                task = normalize_task(
                    task_id,
                    raw,
                    images_dir=images_dir,
                    linked_docs_dir=linked_docs_dir,
                    markdown_dir=tasks_dir,
                )
                content = render_markdown(task)
                safe_title = _safe_filename(task["title"])
                dest = tasks_dir / f"{safe_title}.md"
                # 同名时加 task_id 后缀避免覆盖
                if dest.exists():
                    dest = tasks_dir / f"{safe_title}_{task_id}.md"
                dest.write_text(content, encoding="utf-8")
                written.append(dest)
            except Exception as e:
                click.echo(f"  跳过任务 {task_id}: {e}", err=True)
        click.echo(f"  导出 Tasks: {len(raw_tasks)} 个", err=True)

    # --- 导出 Docs ---
    if workspace_id:
        click.echo(f"  导出 Docs (workspace={workspace_id}) ...", err=True)
        doc_paths = export_folder_docs(workspace_id, folder_id, folder_dir)
        written.extend(doc_paths)
        click.echo(f"  导出 Docs: {len(doc_paths)} 个", err=True)
    else:
        click.echo("  无法确定 workspace_id，跳过 Docs 导出。", err=True)

    return written


def _render_doc_page(
    page_name: str,
    page_id: str,
    doc_id: str,
    workspace_id: str,
    content: str,
) -> str:
    """生成单个 Doc Page 的 Markdown 内容（含元信息头部）。"""
    url = f"https://app.clickup.com/{workspace_id}/docs/{doc_id}/{page_id}"
    return (
        f"# {page_name}\n\n"
        f"## 页面元信息\n\n"
        f"- Page ID: `{page_id}`\n"
        f"- Doc ID: `{doc_id}`\n"
        f"- 链接: {url}\n\n"
        f"## 内容\n\n"
        f"{content}"
    )


def export_folder_docs(
    workspace_id: str,
    folder_id: str,
    folder_dir: Path,
) -> List[Path]:
    """
    导出 Folder 下所有 ClickUp Docs，每个 Doc 写成一个 Markdown 文件。
    输出到 <folder_dir>/docs/<doc_name>.md，图片保存到 <folder_dir>/docs/images/。
    """
    docs = fetch_folder_docs(workspace_id, folder_id)
    if not docs:
        click.echo("  Folder 下没有找到任何 Doc。", err=True)
        return []

    docs_dir = folder_dir / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    images_dir = docs_dir / "images"

    written: List[Path] = []
    for doc in docs:
        doc_id = doc.get("id", "")
        doc_name = _normalize(doc.get("name") or doc_id)
        click.echo(f"  导出 Doc [{doc_name}] ({doc_id}) ...", err=True)

        pages = fetch_doc_all_pages(workspace_id, doc_id)
        if not pages:
            click.echo(f"    无页面内容，跳过。", err=True)
            continue

        # 每个页面写成独立文件，子页面放到以父页面命名的子目录下
        # 内容从 v3 API 获取 markdown，保留表格/标题/图片等格式
        doc_dir = docs_dir / _safe_filename(doc_name)
        for page in pages:
            page_dir = doc_dir / page["_rel_dir"]
            page_dir.mkdir(parents=True, exist_ok=True)
            try:
                page_data = fetch_doc_page(workspace_id, doc_id, page["id"])
                raw_content = _normalize(page_data.get("content") or "")
            except click.ClickException:
                raw_content = ""
            dest = page_dir / f"{page['_safe_name']}.md"
            raw_content = _download_images(raw_content, images_dir, page_dir)
            dest.write_text(
                _render_doc_page(page["name"], page["id"], doc_id, workspace_id, raw_content),
                encoding="utf-8",
            )
            written.append(dest)

    return written
