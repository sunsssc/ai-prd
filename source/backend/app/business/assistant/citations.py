from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from uuid import uuid4

from app.business.assistant.models import AnswerCitationRecord, TurnContextRecord


def build_answer_citations(
    *,
    turn_id: str,
    assistant_message_id: str | None,
    usage_records: Iterable[TurnContextRecord],
) -> list[AnswerCitationRecord]:
    citations: list[AnswerCitationRecord] = []
    seen_keys: set[tuple[str, str | None]] = set()
    now = datetime.now(timezone.utc)

    for usage in usage_records:
        path = usage.source_uri or usage.metadata.get("path")
        dedupe_key = (usage.source_type, path)
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)

        citations.append(
            AnswerCitationRecord(
                citation_id=str(uuid4()),
                turn_id=turn_id,
                assistant_message_id=assistant_message_id,
                citation_type=usage.source_type,
                label=usage.label,
                path=path,
                line_start=_to_int(usage.metadata.get("line_start")),
                line_end=_to_int(usage.metadata.get("line_end")),
                snippet=usage.content,
                metadata=usage.metadata,
                created_at=now,
            )
        )

    return citations


def _to_int(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None
