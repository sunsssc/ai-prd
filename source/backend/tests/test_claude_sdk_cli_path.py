"""Exercise the real SDK options boundary without starting a CLI or model."""
from pathlib import Path

import claude_agent_sdk
import pytest

from app.integrations.agent_runtime.claude_runtime import ClaudeCodeAgentRuntimeClient


@pytest.mark.anyio
@pytest.mark.parametrize("configured", [None, "", "relative", "absolute"])
async def test_sdk_receives_the_resolved_absolute_cli_path(tmp_path, monkeypatch, configured):
    cli = tmp_path / "bin" / "claude"
    cli.parent.mkdir()
    cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    cli.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(cli.parent))
    configured_path = {"relative": "bin/claude", "absolute": str(cli)}.get(configured, configured)
    captured = []

    async def capture_query(*, prompt, options):
        assert isinstance(options, claude_agent_sdk.ClaudeAgentOptions)
        captured.append(options)
        if False:
            yield None

    monkeypatch.setattr(claude_agent_sdk, "query", capture_query)
    runtime = ClaudeCodeAgentRuntimeClient(
        model="test-model", max_turns=1, permission_mode="default",
        allowed_tools=["Read"], cli_path=configured_path,
    )
    session = await runtime.create_or_resume_session(
        runtime_session_id=None, working_directory=str(tmp_path / "job"), system_prompt="test",
    )
    async for _ in runtime.send_message_stream(session=session, message="test", metadata={}):
        pass

    assert len(captured) == 1
    # The SDK must execute the same binary we validated, even if its cwd differs.
    assert captured[0].cli_path == str(cli.resolve())
    assert Path(captured[0].cli_path).is_absolute()
