"""
一次性迁移脚本：将 ClickUp 导出的「同名 .md 文件 + 目录」配对转为 _index.md 格式。

迁移前：
    docs/合约交易/25年交易重点优化需求.md      ← 主文档内容
    docs/合约交易/25年交易重点优化需求/         ← 子页面目录

迁移后：
    docs/合约交易/25年交易重点优化需求/_index.md ← 主文档内容（移入目录内）
    docs/合约交易/25年交易重点优化需求/         ← 子页面目录

用法:
    python migrate_index_md.py [--dry-run] [--root <path>]
"""

import shutil
from pathlib import Path

import click

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DOCS_ROOT = _REPO_ROOT / "workspace" / "knowledge" / "requirements" / "docs"


def find_pairs(root: Path) -> list[tuple[Path, Path]]:
    """递归查找所有「X.md + X/」配对。"""
    pairs = []
    for item in sorted(root.iterdir()):
        if item.is_dir() and not item.name.startswith((".", "_")):
            sibling_md = item.parent / f"{item.name}.md"
            if sibling_md.is_file():
                pairs.append((sibling_md, item))
            # 递归检查子目录
            pairs.extend(find_pairs(item))
    return pairs


@click.command()
@click.option("--root", type=click.Path(exists=True, path_type=Path), default=None, help="文档根目录")
@click.option("--dry-run", is_flag=True, default=False, help="仅显示将执行的操作，不实际移动文件")
def migrate(root: Path | None, dry_run: bool) -> None:
    docs_root = root or DEFAULT_DOCS_ROOT
    if not docs_root.is_dir():
        click.echo(f"目录不存在: {docs_root}", err=True)
        raise SystemExit(1)

    pairs = find_pairs(docs_root)
    if not pairs:
        click.echo("没有找到需要迁移的配对。")
        return

    click.echo(f"找到 {len(pairs)} 个配对：")
    for md_file, directory in pairs:
        dest = directory / "_index.md"
        rel_md = md_file.relative_to(docs_root)
        rel_dest = dest.relative_to(docs_root)

        if dest.exists():
            if dry_run:
                click.echo(f"  [将删除] {rel_md}（{rel_dest} 已存在）")
            else:
                md_file.unlink()
                click.echo(f"  [已删除] {rel_md}（{rel_dest} 已存在）")
            continue

        if dry_run:
            click.echo(f"  [将移动] {rel_md} → {rel_dest}")
        else:
            shutil.move(str(md_file), str(dest))
            click.echo(f"  [已移动] {rel_md} → {rel_dest}")

    if dry_run:
        click.echo("\n（dry-run 模式，未执行实际操作）")


if __name__ == "__main__":
    migrate()
