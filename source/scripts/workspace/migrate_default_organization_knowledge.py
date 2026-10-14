from __future__ import annotations

import argparse
import shutil
from pathlib import Path


SCOPES = ("requirements", "business-docs", "code")


def migrate(base_dir: Path, organization_key: str, *, dry_run: bool) -> list[str]:
    workspace_dir = base_dir / "workspace"
    legacy_root = workspace_dir / "knowledge"
    target_root = workspace_dir / organization_key / "knowledge"
    actions: list[str] = []

    for scope in SCOPES:
        source = legacy_root / scope
        target = target_root / scope
        if not source.exists():
            continue
        if target.exists():
            if any(target.iterdir()):
                raise RuntimeError(f"目标目录已存在且非空，停止迁移避免覆盖：{target}")
            if not dry_run:
                target.rmdir()
        actions.append(f"{source} -> {target}")
        if dry_run:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))

    return actions


def main() -> None:
    parser = argparse.ArgumentParser(description="迁移历史全局 knowledge 到默认组织目录。")
    parser.add_argument("--base-dir", default=str(Path(__file__).resolve().parents[3]))
    parser.add_argument("--organization-key", default="coinex")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    actions = migrate(Path(args.base_dir).resolve(), args.organization_key.strip().lower(), dry_run=args.dry_run)
    if not actions:
        print("没有需要迁移的历史 knowledge 目录。")
        return
    print("\n".join(actions))


if __name__ == "__main__":
    main()
