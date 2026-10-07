"""
ClickUp 增量同步脚本（CLI 入口）。

用法:
    python sync.py --folder <folder_id_or_url> [--output-dir <requirements_root>] [--force]

选项:
    --folder      ClickUp folder_id（纯数字）或 Folder 页面链接
    --output-dir  输出根目录（默认 workspace/knowledge/requirements）
    --force       强制全量同步，忽略本地文件的修改时间

环境变量 (可写入同目录 .env 或 .env.local):
    CLICKUP_API_TOKEN        必填
    CLICKUP_API_BASE_URL     默认 https://api.clickup.com/api/v2
    CLICKUP_API_V3_BASE_URL  默认 https://api.clickup.com/api/v3
"""

import fcntl
import os
import sys
from pathlib import Path
from typing import Optional

import click

# 指向 backend 包，使 app.utils.clickup 可以正常 import
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.utils.clickup.export_task import (  # noqa: E402
    CLICKUP_FOLDER_URL_PATTERN,
    DEFAULT_REQUIREMENTS_ROOT,
    _load_env,
    _safe_filename,
    extract_folder_id,
    fetch_folder_info,
)
from app.utils.clickup.sync import (  # noqa: E402
    SyncStats,
    _sync_docs,
    _sync_grouped_task_lists,
)

SCRIPT_DIR = Path(__file__).resolve().parent
LOCK_FILE = SCRIPT_DIR / ".sync.lock"


@click.command()
@click.option(
    "--folder", "folder_value", default=None,
    help="ClickUp folder_id（纯数字）或 Folder 页面链接，默认读取环境变量 CLICKUP_FOLDER_ID",
)
@click.option(
    "--output-dir", type=click.Path(path_type=Path), default=None,
    help="输出根目录（默认 workspace/knowledge/requirements）",
)
@click.option(
    "--force", is_flag=True,
    help="强制全量同步，忽略本地文件修改时间",
)
def main(
    folder_value: Optional[str],
    output_dir: Optional[Path],
    force: bool,
) -> None:
    """ClickUp 增量同步：只下载本地文件过期或不存在的内容。"""
    _load_env()

    folder_value = folder_value or os.getenv("CLICKUP_FOLDER_ID")

    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise click.ClickException("另一个同步进程正在运行，请稍后重试。")

    try:
        base = output_dir or DEFAULT_REQUIREMENTS_ROOT
        base.mkdir(parents=True, exist_ok=True)

        stats = SyncStats()

        if force:
            click.echo("--force 模式：强制全量同步", err=True)

        task_list_specs_raw = os.getenv("CLICKUP_TASK_LISTS", "")
        task_list_specs = [s.strip() for s in task_list_specs_raw.split(",") if s.strip()]
        if task_list_specs:
            _sync_grouped_task_lists(task_list_specs, base / "tasks", stats, force)
        else:
            click.echo("未配置 CLICKUP_TASK_LISTS，跳过 Task 同步。", err=True)

        if folder_value:
            folder_id = extract_folder_id(folder_value)
            workspace_id: Optional[str] = None
            m = CLICKUP_FOLDER_URL_PATTERN.search(folder_value)
            if m:
                workspace_id = m.group("workspace_id")
            folder_info = fetch_folder_info(folder_id)
            if not workspace_id:
                workspace_id = str((folder_info.get("space") or {}).get("id") or "")
            if workspace_id:
                _sync_docs(workspace_id, folder_id, base / "docs", stats, force)
            else:
                click.echo("无法确定 workspace_id，跳过 Docs 同步。", err=True)
        else:
            click.echo("未配置 --folder / CLICKUP_FOLDER_ID，跳过 Docs 同步。", err=True)

        stats.print_summary()

    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


if __name__ == "__main__":
    main()
