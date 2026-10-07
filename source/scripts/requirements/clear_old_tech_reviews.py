from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import click


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_WORKSPACE_ROOT = REPO_ROOT / "workspace"
DEFAULT_REVIEW_ROOT_RELATED = Path("knowledge/__reviews__/tech-review")
DEFAULT_META_ROOT_RELATED = Path("knowledge/requirements/__meta__")
DEFAULT_DB_RELATED = Path("runtime/db/assistant.sqlite3")
DEFAULT_REVIEW_TYPE = "tech_review"
DEFAULT_OLD_SKILL_NAME = "tech-review-prd"


@dataclass(frozen=True)
class ReviewCleanupTarget:
    path: Path
    display_path: str
    review_id: str
    review_type: str
    skill: str


@dataclass(frozen=True)
class MetaCleanupResult:
    scanned_files: int = 0
    updated_files: int = 0
    removed_reviews: int = 0
    removed_latest_keys: int = 0


@dataclass(frozen=True)
class DatabaseCleanupResult:
    review_reads: int = 0
    review_jobs: int = 0
    clickup_publish_jobs: int = 0
    skipped: bool = False


def split_frontmatter(markdown: str) -> tuple[dict[str, str], str]:
    normalized = markdown.replace("\r\n", "\n")
    match = re.match(r"^---\n([\s\S]*?)\n---(?:\n+|$)", normalized)
    if not match:
        return {}, normalized
    frontmatter: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        frontmatter[key.strip()] = value.strip()
    return frontmatter, normalized[match.end():]


def collect_review_targets(
    *,
    workspace_root: Path,
    review_root: Path,
    review_type: str,
    skill_name: str,
    all_tech_review: bool,
) -> list[ReviewCleanupTarget]:
    if not review_root.exists():
        return []

    targets: list[ReviewCleanupTarget] = []
    for review_file in sorted(review_root.rglob("review_*.md")):
        frontmatter, _ = split_frontmatter(review_file.read_text(encoding="utf-8", errors="replace"))
        file_review_type = frontmatter.get("review_type", "")
        file_skill = frontmatter.get("skill", "")
        if all_tech_review:
            if file_review_type and file_review_type != review_type:
                continue
        elif file_skill != skill_name:
            continue

        display_path = f"workspace/{review_file.relative_to(workspace_root).as_posix()}"
        targets.append(
            ReviewCleanupTarget(
                path=review_file,
                display_path=display_path,
                review_id=frontmatter.get("review_id", review_file.stem),
                review_type=file_review_type or review_type,
                skill=file_skill,
            )
        )
    return targets


def cleanup_review_files(*, targets: list[ReviewCleanupTarget], review_root: Path, apply: bool) -> int:
    if not apply:
        return 0
    deleted = 0
    for target in targets:
        if target.path.is_file():
            target.path.unlink()
            deleted += 1
    prune_empty_dirs(review_root)
    return deleted


def prune_empty_dirs(root: Path) -> None:
    if not root.exists():
        return
    for path in sorted((item for item in root.rglob("*") if item.is_dir()), key=lambda item: len(item.parts), reverse=True):
        try:
            path.rmdir()
        except OSError:
            pass


def cleanup_meta_files(
    *,
    meta_root: Path,
    review_ids: set[str],
    review_paths: set[str],
    review_type: str,
    all_tech_review: bool,
    apply: bool,
) -> MetaCleanupResult:
    if not meta_root.exists():
        return MetaCleanupResult()

    scanned_files = 0
    updated_files = 0
    removed_reviews = 0
    removed_latest_keys = 0

    for meta_file in sorted(meta_root.rglob("*.json")):
        scanned_files += 1
        try:
            payload = json.loads(meta_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue

        changed = False
        reviews = payload.get("reviews")
        if isinstance(reviews, list):
            kept_reviews = []
            for item in reviews:
                if isinstance(item, dict) and should_remove_meta_review(
                    item,
                    review_ids=review_ids,
                    review_paths=review_paths,
                    review_type=review_type,
                    all_tech_review=all_tech_review,
                ):
                    removed_reviews += 1
                    changed = True
                    continue
                kept_reviews.append(item)
            payload["reviews"] = kept_reviews

        latest_reviews = payload.get("latest_reviews")
        if isinstance(latest_reviews, dict):
            latest_review_id = latest_reviews.get(review_type)
            should_remove_latest = all_tech_review or latest_review_id in review_ids
            if should_remove_latest and review_type in latest_reviews:
                latest_reviews.pop(review_type, None)
                removed_latest_keys += 1
                changed = True
                if latest_reviews:
                    payload["latest_reviews"] = latest_reviews
                else:
                    payload.pop("latest_reviews", None)

        if changed:
            updated_files += 1
            if apply:
                meta_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return MetaCleanupResult(
        scanned_files=scanned_files,
        updated_files=updated_files,
        removed_reviews=removed_reviews,
        removed_latest_keys=removed_latest_keys,
    )


def should_remove_meta_review(
    item: dict,
    *,
    review_ids: set[str],
    review_paths: set[str],
    review_type: str,
    all_tech_review: bool,
) -> bool:
    if all_tech_review and item.get("review_type") == review_type:
        return True
    return item.get("review_id") in review_ids or item.get("path") in review_paths


def cleanup_database(
    *,
    db_path: Path,
    review_ids: set[str],
    review_paths: set[str],
    review_type: str,
    skill_name: str,
    all_tech_review: bool,
    apply: bool,
) -> DatabaseCleanupResult:
    if not db_path.exists():
        return DatabaseCleanupResult(skipped=True)

    with sqlite3.connect(db_path) as connection:
        review_reads = delete_review_reads(
            connection=connection,
            review_ids=review_ids,
            review_paths=review_paths,
            all_tech_review=all_tech_review,
            apply=apply,
        )
        clickup_publish_jobs = delete_clickup_publish_jobs(
            connection=connection,
            review_ids=review_ids,
            review_paths=review_paths,
            all_tech_review=all_tech_review,
            apply=apply,
        )
        review_jobs = delete_review_jobs(
            connection=connection,
            review_ids=review_ids,
            review_paths=review_paths,
            review_type=review_type,
            skill_name=skill_name,
            all_tech_review=all_tech_review,
            apply=apply,
        )
        if apply:
            connection.commit()

    return DatabaseCleanupResult(
        review_reads=review_reads,
        review_jobs=review_jobs,
        clickup_publish_jobs=clickup_publish_jobs,
    )


def delete_review_reads(
    *,
    connection: sqlite3.Connection,
    review_ids: set[str],
    review_paths: set[str],
    all_tech_review: bool,
    apply: bool,
) -> int:
    clauses: list[str] = []
    params: list[str] = []
    add_in_clause(clauses, params, "review_id", review_ids)
    add_in_clause(clauses, params, "review_path", review_paths)
    if all_tech_review:
        clauses.append("review_path LIKE ?")
        params.append("workspace/knowledge/__reviews__/tech-review/%")
    return execute_delete_or_count(
        connection=connection,
        table="requirement_review_reads",
        clauses=clauses,
        params=params,
        apply=apply,
    )


def delete_clickup_publish_jobs(
    *,
    connection: sqlite3.Connection,
    review_ids: set[str],
    review_paths: set[str],
    all_tech_review: bool,
    apply: bool,
) -> int:
    clauses: list[str] = []
    params: list[str] = []
    add_in_clause(clauses, params, "review_id", review_ids)
    add_in_clause(clauses, params, "review_path", review_paths)
    if all_tech_review:
        clauses.append("review_path LIKE ?")
        params.append("workspace/knowledge/__reviews__/tech-review/%")
    return execute_delete_or_count(
        connection=connection,
        table="requirement_review_clickup_publish_jobs",
        clauses=clauses,
        params=params,
        apply=apply,
    )


def delete_review_jobs(
    *,
    connection: sqlite3.Connection,
    review_ids: set[str],
    review_paths: set[str],
    review_type: str,
    skill_name: str,
    all_tech_review: bool,
    apply: bool,
) -> int:
    clauses: list[str] = []
    params: list[str] = []
    add_in_clause(clauses, params, "review_id", review_ids)
    add_in_clause(clauses, params, "review_path", review_paths)
    if all_tech_review:
        clauses.append("review_type = ?")
        params.append(review_type)
    else:
        clauses.append("skill_name = ?")
        params.append(skill_name)
    return execute_delete_or_count(
        connection=connection,
        table="requirement_review_jobs",
        clauses=clauses,
        params=params,
        apply=apply,
    )


def add_in_clause(clauses: list[str], params: list[str], column: str, values: set[str]) -> None:
    cleaned = sorted(value for value in values if value)
    if not cleaned:
        return
    placeholders = ", ".join("?" for _ in cleaned)
    clauses.append(f"{column} IN ({placeholders})")
    params.extend(cleaned)


def execute_delete_or_count(
    *,
    connection: sqlite3.Connection,
    table: str,
    clauses: list[str],
    params: list[str],
    apply: bool,
) -> int:
    if not clauses:
        return 0
    where = " OR ".join(f"({clause})" for clause in clauses)
    operation = "DELETE" if apply else "SELECT COUNT(*)"
    sql = f"{operation} FROM {table} WHERE {where}"
    cursor = connection.execute(sql, params)
    if apply:
        return cursor.rowcount
    row = cursor.fetchone()
    return int(row[0]) if row else 0


@click.command()
@click.option(
    "--workspace-root",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=DEFAULT_WORKSPACE_ROOT,
    show_default=True,
    help="workspace 根目录。",
)
@click.option(
    "--db-path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="assistant.sqlite3 路径；默认使用 workspace/runtime/db/assistant.sqlite3。",
)
@click.option(
    "--skill-name",
    default=DEFAULT_OLD_SKILL_NAME,
    show_default=True,
    help="只清理该 skill 生成的旧评审。",
)
@click.option(
    "--review-type",
    default=DEFAULT_REVIEW_TYPE,
    show_default=True,
    help="评审类型。",
)
@click.option(
    "--all-tech-review",
    is_flag=True,
    help="清理全部 tech-review 目录和 tech_review 元数据；会包含新旧需求评审。",
)
@click.option(
    "--apply",
    "apply_changes",
    is_flag=True,
    help="真正删除文件并写入 meta / SQLite；不加时只 dry-run。",
)
def main(
    workspace_root: Path,
    db_path: Path | None,
    skill_name: str,
    review_type: str,
    all_tech_review: bool,
    apply_changes: bool,
) -> None:
    workspace_root = workspace_root.resolve()
    review_root = workspace_root / DEFAULT_REVIEW_ROOT_RELATED
    meta_root = workspace_root / DEFAULT_META_ROOT_RELATED
    resolved_db_path = (db_path or workspace_root / DEFAULT_DB_RELATED).resolve()

    targets = collect_review_targets(
        workspace_root=workspace_root,
        review_root=review_root,
        review_type=review_type,
        skill_name=skill_name,
        all_tech_review=all_tech_review,
    )
    review_ids = {target.review_id for target in targets if target.review_id}
    review_paths = {target.display_path for target in targets if target.display_path}

    meta_result = cleanup_meta_files(
        meta_root=meta_root,
        review_ids=review_ids,
        review_paths=review_paths,
        review_type=review_type,
        all_tech_review=all_tech_review,
        apply=apply_changes,
    )
    db_result = cleanup_database(
        db_path=resolved_db_path,
        review_ids=review_ids,
        review_paths=review_paths,
        review_type=review_type,
        skill_name=skill_name,
        all_tech_review=all_tech_review,
        apply=apply_changes,
    )
    deleted_files = cleanup_review_files(targets=targets, review_root=review_root, apply=apply_changes)

    action = "已清理" if apply_changes else "将清理"
    scope = f"全部 {review_type}" if all_tech_review else f"skill={skill_name}"
    click.echo(f"{action}旧技术评审记录（{scope}）：")
    click.echo(f"- review 文件：匹配 {len(targets)} 个，删除 {deleted_files if apply_changes else 0} 个")
    click.echo(
        "- meta 文件：扫描 "
        f"{meta_result.scanned_files} 个，{'更新' if apply_changes else '将更新'} {meta_result.updated_files} 个，"
        f"{'移除' if apply_changes else '将移除'} reviews {meta_result.removed_reviews} 条，"
        f"latest_reviews {meta_result.removed_latest_keys} 个"
    )
    if db_result.skipped:
        click.echo(f"- SQLite：跳过，文件不存在 {resolved_db_path}")
    else:
        click.echo(
            "- SQLite："
            f"{'删除' if apply_changes else '将删除'} reads {db_result.review_reads} 行，"
            f"jobs {db_result.review_jobs} 行，"
            f"ClickUp 发布任务 {db_result.clickup_publish_jobs} 行"
        )
    if not apply_changes:
        click.echo("当前为 dry-run；确认无误后追加 --apply 执行清理。")


if __name__ == "__main__":
    main()
