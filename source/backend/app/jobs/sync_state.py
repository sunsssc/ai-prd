from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def _read_existing_state(target_path: Path) -> dict[str, object]:
    if not target_path.is_file():
        return {}

    try:
        payload = json.loads(target_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}

    return payload if isinstance(payload, dict) else {}


def _extract_previous_last_change(payload: dict[str, object]) -> dict[str, object] | None:
    last_change = payload.get("last_change")
    if isinstance(last_change, dict) and isinstance(last_change.get("changed_at"), str):
        return last_change

    last_changed_at = payload.get("last_changed_at")
    last_added = payload.get("last_added")
    last_updated = payload.get("last_updated")
    last_changed_files = payload.get("last_changed_files")

    if isinstance(last_changed_at, str):
        changed_files = last_changed_files if isinstance(last_changed_files, list) else []
        changed_count = int(last_added or 0) + int(last_updated or 0)
        return {
            "changed_at": last_changed_at,
            "added": int(last_added or 0),
            "updated": int(last_updated or 0),
            "changed_count": changed_count or len(changed_files),
            "changed_files": changed_files,
        }

    changed_files = payload.get("changed_files")
    added_count = int(payload.get("added") or 0)
    updated_count = int(payload.get("updated") or 0)
    changed_count = added_count + updated_count
    synced_at = payload.get("synced_at")
    if changed_count <= 0 and not isinstance(changed_files, list):
        return None
    if not isinstance(synced_at, str):
        return None
    if changed_count <= 0 and isinstance(changed_files, list):
        changed_count = len(changed_files)

    return {
        "changed_at": synced_at,
        "added": added_count,
        "updated": updated_count,
        "changed_count": changed_count,
        "changed_files": changed_files if isinstance(changed_files, list) else [],
    }


def write_sync_state(
    *,
    base_dir: Path,
    scope: str,
    added: int,
    updated: int,
    changed_files: list[str],
    errors: int,
    synced_at: datetime | None = None,
) -> None:
    state_dir = base_dir / "workspace/runtime/sync-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    target_path = state_dir / f"{scope}.json"
    synced_time = synced_at or datetime.now(timezone.utc)
    synced_at_text = synced_time.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    previous_payload = _read_existing_state(target_path)
    changed_count = int(added) + int(updated)
    if changed_count <= 0 and changed_files:
        changed_count = len(changed_files)

    payload = {
        "last_run": {
            "synced_at": synced_at_text,
            "added": int(added),
            "updated": int(updated),
            "changed_count": changed_count,
            "errors": int(errors),
        },
        "last_change": _extract_previous_last_change(previous_payload),
    }
    if changed_count > 0 or changed_files:
        payload["last_change"] = {
            "changed_at": synced_at_text,
            "added": int(added),
            "updated": int(updated),
            "changed_count": changed_count,
            "changed_files": changed_files,
        }

    temp_path = target_path.with_suffix(f"{target_path.suffix}.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(target_path)
