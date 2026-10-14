from __future__ import annotations

from pathlib import Path

import click

from markdown_image_paths import rewrite_shared_images_references


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DOCS_ROOT = REPO_ROOT / "workspace/knowledge/requirements/docs"


@click.command()
@click.option(
    "--docs-root",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=DEFAULT_DOCS_ROOT,
    show_default=True,
    help="需求 markdown 根目录。",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="只输出将要修改的文件，不落盘。",
)
def main(docs_root: Path, dry_run: bool) -> None:
    docs_root = docs_root.resolve()
    images_dir = docs_root / "images"
    if not docs_root.exists():
        raise click.ClickException(f"目录不存在: {docs_root}")
    if not images_dir.exists():
        raise click.ClickException(f"共享图片目录不存在: {images_dir}")

    updated_files = 0
    updated_refs = 0
    for markdown_file in sorted(docs_root.rglob("*.md")):
        content = markdown_file.read_text(encoding="utf-8")
        rewritten, replacements = rewrite_shared_images_references(
            content,
            markdown_file=markdown_file,
            shared_images_dir=images_dir,
        )
        if replacements <= 0:
            continue

        updated_files += 1
        updated_refs += replacements
        if not dry_run:
            markdown_file.write_text(rewritten, encoding="utf-8")
        click.echo(f"{markdown_file.relative_to(docs_root)}: 修正 {replacements} 处图片引用")

    action = "将修改" if dry_run else "已修改"
    click.echo(f"{action} {updated_files} 个文件，合计 {updated_refs} 处图片引用。")


if __name__ == "__main__":
    main()
