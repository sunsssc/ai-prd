"""
清理 ClickUp 导出中 Page ID 重复的文件。

ClickUp 页面改名后（如去掉 @作者名 后缀），旧文件不会被同步脚本删除，
导致同一 Page ID 出现多个 .md 文件。本脚本扫描所有 .md 文件，
对每个 Page ID 仅保留路径最短（层级最浅）的那个，删除其余副本。

用法:
    python cleanup_duplicate_pages.py [--dry-run] [--root <path>]
"""

import re
from pathlib import Path

import click

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DOCS_ROOT = _REPO_ROOT / "workspace" / "knowledge" / "requirements" / "docs"

PAGE_ID_PATTERN = re.compile(r"^- Page ID:\s*`([^`]+)`", re.MULTILINE)


def extract_page_id(file_path: Path) -> str | None:
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    match = PAGE_ID_PATTERN.search(content)
    return match.group(1) if match else None


def find_duplicates(root: Path) -> dict[str, list[Path]]:
    """扫描所有 .md 文件，按 Page ID 分组，返回有重复的组。"""
    page_id_to_files: dict[str, list[Path]] = {}
    for md_file in root.rglob("*.md"):
        if md_file.name.startswith("."):
            continue
        page_id = extract_page_id(md_file)
        if page_id:
            page_id_to_files.setdefault(page_id, []).append(md_file)

    return {pid: files for pid, files in page_id_to_files.items() if len(files) > 1}


def pick_keeper(files: list[Path]) -> Path:
    """
    选择保留的文件：
    1. 排除 _linked 目录（它是任务关联副本，不是正式位置）
    2. 优先 _index.md（当前导出格式）
    3. 路径最短的（层级最浅）
    4. 最新修改的
    """
    def sort_key(f: Path) -> tuple:
        in_linked = any(p.startswith("_linked") for p in f.parts)
        is_index = f.name == "_index.md"
        return (in_linked, not is_index, len(f.parts), -f.stat().st_mtime)

    return sorted(files, key=sort_key)[0]


def cleanup_empty_parents(file_path: Path, stop_at: Path) -> list[Path]:
    """删除文件后，递归清理空的父目录（直到 stop_at）。"""
    removed = []
    parent = file_path.parent
    while parent != stop_at and parent.is_dir():
        remaining = [c for c in parent.iterdir() if not c.name.startswith(".")]
        if remaining:
            break
        parent.rmdir()
        removed.append(parent)
        parent = parent.parent
    return removed


@click.command()
@click.option("--root", type=click.Path(exists=True, path_type=Path), default=None, help="文档根目录")
@click.option("--dry-run", is_flag=True, default=False, help="仅显示将执行的操作")
def cleanup(root: Path | None, dry_run: bool) -> None:
    docs_root = root or DEFAULT_DOCS_ROOT
    if not docs_root.is_dir():
        click.echo(f"目录不存在: {docs_root}", err=True)
        raise SystemExit(1)

    duplicates = find_duplicates(docs_root)
    if not duplicates:
        click.echo("没有找到重复的 Page ID。")
        return

    total_to_remove = 0
    for page_id, files in sorted(duplicates.items()):
        keeper = pick_keeper(files)
        to_remove = [f for f in files if f != keeper]
        total_to_remove += len(to_remove)

        rel_keeper = keeper.relative_to(docs_root)
        click.echo(f"\nPage ID: {page_id}")
        click.echo(f"  [保留] {rel_keeper}")
        for f in to_remove:
            rel = f.relative_to(docs_root)
            if dry_run:
                click.echo(f"  [将删除] {rel}")
            else:
                f.unlink()
                removed_dirs = cleanup_empty_parents(f, docs_root)
                click.echo(f"  [已删除] {rel}")
                for d in removed_dirs:
                    click.echo(f"  [清理空目录] {d.relative_to(docs_root)}")

    click.echo(f"\n共 {len(duplicates)} 组重复，{'将' if dry_run else '已'}删除 {total_to_remove} 个文件。")
    if dry_run:
        click.echo("（dry-run 模式，未执行实际操作）")


if __name__ == "__main__":
    cleanup()
