from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from app.integrations.agent_runtime.codex_app_server_client import CodexAppServerClient


@pytest.mark.anyio
async def test_codex_app_server_client_reads_jsonl_response_larger_than_64_kib(
    tmp_path: Path,
) -> None:
    fake_cli = tmp_path / "fake-codex"
    fake_cli.write_text(
        f"""#!{sys.executable}
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    request_id = request.get("id")
    if request_id is None:
        continue
    if request["method"] == "thread/resume":
        result = {{
            "thread": {{
                "id": "thread-large",
                "turns": [{{"content": "x" * 70_000}}],
            }}
        }}
    else:
        result = {{}}
    print(json.dumps({{"id": request_id, "result": result}}), flush=True)
""",
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)
    client = CodexAppServerClient(cli_path=str(fake_cli), env=os.environ.copy())

    try:
        await client.initialize()
        result = await client.request(
            "thread/resume",
            {"threadId": "thread-large"},
        )
    finally:
        await client.close()

    assert result["thread"]["id"] == "thread-large"
    assert len(result["thread"]["turns"][0]["content"]) == 70_000
