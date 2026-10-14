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

import sys
from pathlib import Path
from typing import Optional

import click

# 指向 backend 包，使 app.utils.clickup 可以正常 import
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.utils.clickup.export_task import (  # noqa: E402
    _load_env,
    _require_env_vars,
    export_folder,
    export_list,
    export_task,
)


@click.command()
@click.option(
    "--task", "task_value", default=None,
    help="ClickUp task_id 或任务链接，如 86a123xyz 或 https://app.clickup.com/t/86a123xyz",
)
@click.option(
    "--folder", "folder_value", default=None,
    help="ClickUp folder_id 或 Folder 页面链接，"
         "如 90100000001 或 https://acme.clickup.com/9000000001/v/o/f/90100000001",
)
@click.option(
    "--list", "list_value", default=None,
    help="ClickUp list_id 或 List 页面链接，"
         "如 8cabcde-6638 或 https://acme.clickup.com/9000000001/v/l/8cabcde-6638",
)
@click.option("--output-file", type=click.Path(path_type=Path), help="输出文件路径（仅 --task 模式）")
@click.option("--output-dir", type=click.Path(path_type=Path), help="输出目录")
@click.option("--show-raw", is_flag=True, help="同时输出原始 ClickUp JSON，便于排查字段结构")
def main(
    task_value: Optional[str],
    folder_value: Optional[str],
    list_value: Optional[str],
    output_file: Optional[Path],
    output_dir: Optional[Path],
    show_raw: bool,
) -> None:
    """读取 ClickUp 任务并导出为 AI 友好的本地 Markdown 文件。

    \b
    导出单个任务:
        python export_task.py --task https://app.clickup.com/t/86axyz

    导出某个 List:
        python export_task.py --list https://acme.clickup.com/9000000001/v/l/8cabcde-6638

    导出整个 Folder:
        python export_task.py --folder https://acme.clickup.com/9000000001/v/o/f/90100000001
    """
    _load_env()
    _require_env_vars("CLICKUP_API_TOKEN")

    selected_modes = [bool(task_value), bool(folder_value), bool(list_value)]
    if sum(selected_modes) > 1:
        raise click.UsageError("--task、--list 和 --folder 不能同时使用，请选其一。")
    if not any(selected_modes):
        raise click.UsageError("请指定 --task、--list 或 --folder。")

    if task_value:
        dest, task, _ = export_task(task_value, output_dir=output_dir, output_file=output_file)
        click.echo(f"AI 友好需求文件已输出: {dest}")
        if show_raw:
            raw_path = dest.with_suffix(".raw.json")
            raw_path.write_text(task["raw_json"], encoding="utf-8")
            click.echo(f"原始任务 JSON 已输出: {raw_path}")

    elif list_value:
        if output_file:
            raise click.UsageError("--output-file 在 --list 模式下无效，请使用 --output-dir。")
        written = export_list(list_value, output_dir=output_dir)
        click.echo(f"\n共导出 {len(written)} 个任务:")
        for p in written:
            click.echo(f"  {p}")

    else:  # folder_value
        if output_file:
            raise click.UsageError("--output-file 在 --folder 模式下无效，请使用 --output-dir。")
        written = export_folder(folder_value or "", output_dir=output_dir)
        click.echo(f"\n共导出 {len(written)} 个文件:")
        for p in written:
            click.echo(f"  {p}")


if __name__ == "__main__":
    main()
