from __future__ import annotations

from pathlib import Path

import pytest

from app.integrations.agent_runtime import (
    AssistantRuntimeError,
    perform_claude_runtime_startup_check,
    resolve_claude_cli_path,
)


def test_resolve_claude_cli_path_prefers_configured_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli_path = tmp_path / "claude"
    cli_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    cli_path.chmod(0o755)
    monkeypatch.setenv("PATH", "")

    resolved = resolve_claude_cli_path(str(cli_path))

    assert resolved == str(cli_path)


def test_resolve_claude_cli_path_raises_for_invalid_configured_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")

    with pytest.raises(AssistantRuntimeError, match="AI_CLI_PATH"):
        resolve_claude_cli_path("/tmp/not-found-claude")


def test_perform_startup_check_reports_cli_found_without_sdk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli_path = tmp_path / "claude"
    cli_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    cli_path.chmod(0o755)
    monkeypatch.setenv("PATH", "")

    report = perform_claude_runtime_startup_check(str(cli_path))

    assert report["provider"] == "claude_code"
    assert isinstance(report["sdk_importable"], bool)
    assert report["cli_found"] is True
    assert report["cli_path"] == str(cli_path)
