from __future__ import annotations

import json
import re
from pathlib import Path

import click


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_WORKSPACE_ROOT = REPO_ROOT / "workspace"
REVIEW_ROOT_RELATED = Path("knowledge/__reviews__/tech-review")
REQUIREMENTS_META_ROOT_RELATED = Path("knowledge/requirements/__meta__")


def split_frontmatter(markdown: str) -> tuple[dict[str, str], str, str]:
    normalized = markdown.replace("\r\n", "\n")
    match = re.match(r"^---\n([\s\S]*?)\n---(?:\n+|$)", normalized)
    if not match:
        return {}, "", normalized
    frontmatter: dict[str, str] = {}
    raw_frontmatter = match.group(1)
    for line in raw_frontmatter.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        frontmatter[key.strip()] = value.strip()
    return frontmatter, raw_frontmatter, normalized[match.end():]


def format_frontmatter(frontmatter: dict[str, str]) -> str:
    lines = ["---"]
    lines.extend(f"{key}: {value}" for key, value in frontmatter.items())
    lines.append("---")
    return "\n".join(lines)


def detect_risk_level(markdown: str) -> str:
    overall_risk = detect_labeled_risk_level(markdown, labels=("整体问题级别", "综合问题级别", "整体风险等级", "综合风险等级"))
    if overall_risk:
        return overall_risk
    explicit_risk = highest_risk_level(detect_labeled_risk_levels(markdown, labels=("问题级别", "严重级别", "风险等级")))
    if explicit_risk:
        return explicit_risk
    conclusion_risk = detect_conclusion_risk_level(markdown)
    if conclusion_risk:
        return conclusion_risk
    text = markdown.lower()
    if any(keyword in text for keyword in ["严重", "critical", "high risk", "risk: high"]):
        return "high"
    if any(keyword in text for keyword in ["low risk", "risk: low"]):
        return "low"
    return "medium"


def detect_conclusion_risk_level(markdown: str) -> str | None:
    for line in markdown.splitlines():
        normalized = line.strip().lstrip("-*>#0123456789.、）) ").lower()
        if normalized.startswith(("高风险", "critical", "high risk")):
            return "high"
        if normalized.startswith(("中风险", "medium risk", "moderate risk")):
            return "medium"
        if normalized.startswith(("低风险", "low risk")):
            return "low"
    return None


def detect_labeled_risk_level(markdown: str, *, labels: tuple[str, ...]) -> str | None:
    levels = detect_labeled_risk_levels(markdown, labels=labels)
    return levels[0] if levels else None


def detect_labeled_risk_levels(markdown: str, *, labels: tuple[str, ...]) -> list[str]:
    levels: list[str] = []
    label_pattern = "|".join(re.escape(label) for label in labels)
    for line in markdown.splitlines():
        if not any(label in line for label in labels):
            continue
        if "/" in line or "：" not in line and ":" not in line:
            continue
        pattern = rf"(?:\*+)?(?:{label_pattern})(?:\*+)?\s*[:：]\s*(?:`|\*|\s)*([^\s`*|/，,。；;）)]+)"
        match = re.search(pattern, line, flags=re.IGNORECASE)
        if not match:
            continue
        risk_level = normalize_risk_level(match.group(1))
        if risk_level:
            levels.append(risk_level)
    return levels


def highest_risk_level(levels: list[str]) -> str | None:
    order = {"low": 1, "medium": 2, "high": 3}
    return max(levels, key=lambda level: order.get(level, 0), default=None)


def normalize_risk_level(value: str) -> str | None:
    normalized = value.strip().lower()
    if normalized.startswith(("高", "high")):
        return "high"
    if normalized.startswith(("中", "medium", "moderate")):
        return "medium"
    if normalized.startswith(("低", "low")):
        return "low"
    return None


def rewrite_review_file(review_file: Path, *, dry_run: bool) -> tuple[str | None, str | None, str | None]:
    original = review_file.read_text(encoding="utf-8")
    frontmatter, _, body = split_frontmatter(original)
    if not frontmatter:
        return None, None, "缺少 frontmatter"

    current_risk = frontmatter.get("risk_level")
    detected_risk = detect_risk_level(body)
    if current_risk == detected_risk:
        return current_risk, detected_risk, None

    frontmatter["risk_level"] = detected_risk
    if not dry_run:
        review_file.write_text(f"{format_frontmatter(frontmatter)}\n\n{body.rstrip()}\n", encoding="utf-8")
    return current_risk, detected_risk, None


def sync_meta_files(
    *,
    meta_root: Path,
    review_id: str,
    review_path: str,
    detected_risk: str,
    dry_run: bool,
) -> int:
    updated_files = 0
    for meta_file in sorted(meta_root.rglob("*.json")):
        payload = json.loads(meta_file.read_text(encoding="utf-8"))
        reviews = payload.get("reviews")
        if not isinstance(reviews, list):
            continue

        changed = False
        for review in reviews:
            if not isinstance(review, dict):
                continue
            if review.get("review_id") != review_id and review.get("path") != review_path:
                continue
            if review.get("risk_level") == detected_risk:
                continue
            review["risk_level"] = detected_risk
            changed = True

        if not changed:
            continue
        updated_files += 1
        if not dry_run:
            meta_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return updated_files


@click.command()
@click.option(
    "--workspace-root",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=DEFAULT_WORKSPACE_ROOT,
    show_default=True,
    help="workspace 根目录。",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="只输出将要修改的记录，不落盘。",
)
def main(workspace_root: Path, dry_run: bool) -> None:
    workspace_root = workspace_root.resolve()
    review_root = workspace_root / REVIEW_ROOT_RELATED
    meta_root = workspace_root / REQUIREMENTS_META_ROOT_RELATED
    if not review_root.exists():
        raise click.ClickException(f"评审目录不存在: {review_root}")
    if not meta_root.exists():
        raise click.ClickException(f"需求元数据目录不存在: {meta_root}")

    scanned_reviews = 0
    updated_reviews = 0
    updated_meta_files = 0
    skipped_reviews = 0

    for review_file in sorted(review_root.rglob("review_*.md")):
        scanned_reviews += 1
        frontmatter, _, body = split_frontmatter(review_file.read_text(encoding="utf-8"))
        review_id = frontmatter.get("review_id")
        if not review_id:
            skipped_reviews += 1
            click.echo(f"跳过 {review_file}: 缺少 review_id")
            continue

        current_risk, detected_risk, error = rewrite_review_file(review_file, dry_run=dry_run)
        if error:
            skipped_reviews += 1
            click.echo(f"跳过 {review_file}: {error}")
            continue
        if not detected_risk:
            skipped_reviews += 1
            continue

        review_path = f"workspace/{review_file.relative_to(workspace_root).as_posix()}"
        meta_updates = sync_meta_files(
            meta_root=meta_root,
            review_id=review_id,
            review_path=review_path,
            detected_risk=detected_risk,
            dry_run=dry_run,
        )
        updated_meta_files += meta_updates

        if current_risk != detected_risk:
            updated_reviews += 1
            click.echo(f"{review_id}: review 问题级别 {current_risk} -> {detected_risk}, meta files {meta_updates}")
        elif meta_updates:
            click.echo(f"{review_id}: review 问题级别 已是 {detected_risk}, 同步 meta files {meta_updates}")

        del body

    action = "将修复" if dry_run else "已修复"
    click.echo(
        f"{action} review 文件 {updated_reviews} 个，meta 文件 {updated_meta_files} 个；"
        f"扫描 review {scanned_reviews} 个，跳过 {skipped_reviews} 个。"
    )


if __name__ == "__main__":
    main()
