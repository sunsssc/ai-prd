from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from app.business.business_doc_updates.automation import BusinessDocUpdateService, BusinessDocUpdateStore
from app.integrations.agent_runtime import RuntimeEvent, RuntimeSession


class SequencedRuntime:
    provider = "test"

    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []
        self.metadata: list[dict[str, object]] = []
        self.session_ids: list[str | None] = []
        self._next_session = 1

    async def create_or_resume_session(self, *, runtime_session_id, working_directory, system_prompt):
        del runtime_session_id, system_prompt
        return RuntimeSession(provider=self.provider, working_directory=working_directory)

    async def send_message_stream(self, *, session, message, metadata):
        self.prompts.append(message)
        self.metadata.append(metadata)
        self.session_ids.append(session.session_id)
        if session.session_id is None:
            yield RuntimeEvent("session", {"session_id": f"session-{self._next_session}"})
            self._next_session += 1
        yield RuntimeEvent("message", {"content": json.dumps(self.responses.pop(0), ensure_ascii=False)})


class EmptyRuntime(SequencedRuntime):
    async def send_message_stream(self, *, session, message, metadata):
        del session, message, metadata
        yield RuntimeEvent("complete", {"result": ""})


@pytest.mark.anyio
async def test_pending_worker_refills_available_concurrency_slot(tmp_path: Path) -> None:
    _, _, store, service = _workspace(tmp_path, [])
    run = service.create_full_run()
    store.add_items(
        run.run_id,
        tuple((f"workspace/coinex/knowledge/business-docs/marketing/extra-{index}.md", "hash") for index in range(3)),
    )
    slow_path = "workspace/coinex/knowledge/business-docs/marketing/coupon.md"
    slow_release = asyncio.Event()
    fourth_started = asyncio.Event()
    started: list[str] = []

    async def process_item(item) -> None:
        started.append(item.source_path)
        if len(started) == 4:
            fourth_started.set()
        if item.source_path == slow_path:
            await slow_release.wait()
        store.complete_item(
            item_id=item.item_id,
            result_type="no_update_needed",
            update_id=None,
            update_path=None,
            evidence=[],
            review_status="not_required",
            review_feedback=None,
            generator_review=None,
            confidence=1.0,
            apply_status="not_applicable",
        )

    service._process_item = process_item
    worker = asyncio.create_task(service.process_pending_items(limit=3))
    await asyncio.wait_for(fourth_started.wait(), timeout=1)

    assert slow_path in started[:3]
    assert len(started) == 4

    slow_release.set()
    processed = await worker
    assert len(processed) == 4


def _proposal(
    *,
    replacement: str,
    risk_flags: list[str] | None = None,
    resolution: str | None = None,
    cross_repo_conflict: bool = False,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "result_type": "update_required",
        "stable_business_change": True,
        "summary": "优惠券领取后增加待激活状态。",
        "reason": "领域服务状态流转发生稳定变化。",
        "confidence": 0.96,
        "evidence": [{"repo_key": "coupon", "commit": "HEAD", "file": "coupon.py", "symbol": "CouponStatus"}],
        "risk_flags": risk_flags or [],
        "cross_repo_conflict": cross_repo_conflict,
        "replacement_markdown": replacement,
    }
    if resolution is not None:
        payload["review_resolution"] = resolution
    return payload


def _review(*, risk_flags: list[str] | None = None) -> dict[str, object]:
    return {
        "decision": "approve",
        "feedback": "证据充分且没有写入实现细节。",
        "confidence": 0.97,
        "missing_evidence": [],
        "boundary_violations": [],
        "risk_flags": risk_flags or [],
    }


def _workspace(tmp_path: Path, responses: list[dict[str, object]], *, priority: str = "P0"):
    repo = tmp_path / "workspace/coinex/knowledge/code/coupon"
    repo.mkdir(parents=True)
    _git(repo, "init")
    _git(repo, "checkout", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "coupon.py").write_text("STATUS = 'available'\n", encoding="utf-8")
    _git(repo, "add", "coupon.py")
    _git(repo, "commit", "-m", "initial")

    docs_root = tmp_path / "workspace/coinex/knowledge/business-docs"
    doc = docs_root / "marketing/coupon.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("# 优惠券\n\n优惠券领取后可直接使用。\n", encoding="utf-8")
    _git(docs_root, "init")
    _git(docs_root, "checkout", "-b", "main")
    _git(docs_root, "config", "user.email", "test@example.com")
    _git(docs_root, "config", "user.name", "Test")
    _git(docs_root, "add", "marketing/coupon.md")
    _git(docs_root, "commit", "-m", "initial")
    remote = tmp_path / "business-docs-remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare")
    _git(docs_root, "remote", "add", "origin", str(remote))
    _git(docs_root, "push", "-u", "origin", "main")
    skill = tmp_path / ".claude/skills/business-doc-updater/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# Skill\n\n默认不更新，只保留稳定业务语义。\n", encoding="utf-8")
    config = tmp_path / "workspace/config/code-auto-review.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {
                "repositories": [
                    {
                        "repo_full_name": "example-org/coupon",
                        "workspace_repo_path": str(repo),
                        "default_branch": "main",
                        "business_doc_enabled": True,
                        "business_doc_role": "domain",
                        "business_doc_priority": priority,
                        "business_doc_scopes": ["marketing/coupon.md"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    store = BusinessDocUpdateStore(str(tmp_path / "workspace/runtime/db/assistant.sqlite3"))
    service = BusinessDocUpdateService(
        base_dir=tmp_path,
        business_docs_root=docs_root,
        skills_root=tmp_path / ".claude/skills",
        store=store,
        runtime_client=SequencedRuntime(responses),
        project_config_path="workspace/config/code-auto-review.json",
    )
    return repo, doc, store, service


def _add_configured_repo(tmp_path: Path, *, name: str, role: str) -> Path:
    repo = tmp_path / f"workspace/coinex/knowledge/code/{name}"
    repo.mkdir(parents=True)
    _git(repo, "init")
    _git(repo, "checkout", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "rule.py").write_text("RULE = True\n", encoding="utf-8")
    _git(repo, "add", "rule.py")
    _git(repo, "commit", "-m", "initial")
    config_path = tmp_path / "workspace/config/code-auto-review.json"
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    template = payload["repositories"][0]
    payload["repositories"].append(
        {
            **template,
            "repo_full_name": f"example-org/{name}",
            "workspace_repo_path": str(repo),
            "business_doc_role": role,
        }
    )
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    return repo


@pytest.mark.anyio
async def test_incremental_uses_cursor_and_auto_applies_after_two_agent_checks(tmp_path: Path) -> None:
    replacement = "# 优惠券\n\n优惠券领取后先进入待激活状态，激活后方可使用。"
    repo, doc, store, service = _workspace(
        tmp_path,
        [_proposal(replacement=replacement), _review(), _proposal(replacement=replacement, resolution="agree")],
    )
    base_sha = _git(repo, "rev-parse", "HEAD")
    store.set_cursor(repo_key="coupon", analyzed_head=base_sha, last_run_id="baseline")
    (repo / "coupon.py").write_text("STATUS = 'pending_activation'\n", encoding="utf-8")
    _git(repo, "add", "coupon.py")
    _git(repo, "commit", "-m", "change status")
    head_sha = _git(repo, "rev-parse", "HEAD")

    run = service.enqueue_incremental(repo_key="coupon", current_head=head_sha)
    assert run is not None
    assert run.base_sha == base_sha
    assert run.head_sha == head_sha

    await service.process_pending_items()

    completed = service.get_run(run_id=run.run_id)
    item = service.get_run_items(run_id=run.run_id)[0]
    assert completed.status == "completed"
    assert item.review_status == "approve"
    assert item.generator_review == "agree"
    assert item.apply_status == "applied"
    assert doc.read_text(encoding="utf-8") == replacement + "\n"
    docs_repo = doc.parents[1]
    assert f"[business-doc-update:{item.update_id}]" in _git(docs_repo, "log", "-1", "--format=%s")
    assert _git(tmp_path / "business-docs-remote.git", "show", "refs/heads/main:marketing/coupon.md") == replacement
    assert store.get_cursor("coupon").analyzed_head == head_sha
    assert service.runtime_client.session_ids == [None, None, "session-1"]
    assert [metadata["task"] for metadata in service.runtime_client.metadata] == [
        "business_doc_update_generation",
        "business_doc_update_review",
        "business_doc_update_revision",
    ]
    assert service.runtime_client.metadata[2]["output_schema"]["properties"]["review_resolution"]["enum"] == [
        "agree",
        "disagree",
    ]


@pytest.mark.anyio
async def test_full_run_never_auto_applies_and_hash_conflict_blocks_manual_apply(tmp_path: Path) -> None:
    replacement = "# 优惠券\n\n领取后进入待激活状态。"
    _, doc, _, service = _workspace(
        tmp_path,
        [_proposal(replacement=replacement), _review(), _proposal(replacement=replacement, resolution="agree")],
    )

    run = service.create_full_run()
    await service.process_pending_items()
    item = service.get_run_items(run_id=run.run_id)[0]
    assert item.apply_status == "manual_required"
    detail = service.get_update_detail(update_id=item.update_id)
    assert detail.status == "pending"
    assert "## Markdown diff" in detail.content
    assert "+领取后进入待激活状态。" in detail.content

    doc.write_text("# 优惠券\n\n人工修改。\n", encoding="utf-8")
    with pytest.raises(ValueError, match="人工确认"):
        service.apply_update(update_id=item.update_id, user_id="user")


@pytest.mark.anyio
async def test_admin_can_apply_manual_proposal_and_item_status_is_updated(tmp_path: Path) -> None:
    replacement = "# 优惠券\n\n领取后进入待激活状态。"
    _, doc, store, service = _workspace(
        tmp_path,
        [_proposal(replacement=replacement), _review(), _proposal(replacement=replacement, resolution="agree")],
    )
    run = service.create_full_run()
    await service.process_pending_items()
    item = service.get_run_items(run_id=run.run_id)[0]
    docs_repo = doc.parents[1]
    unrelated = docs_repo / "local-notes.txt"
    unrelated.write_text("不属于本次更新\n", encoding="utf-8")
    _git(docs_repo, "add", "local-notes.txt")

    detail = service.apply_update(update_id=item.update_id, user_id="admin-1", actor_role="admin")

    assert detail.status == "applied"
    assert doc.read_text(encoding="utf-8") == replacement + "\n"
    assert _git(tmp_path / "business-docs-remote.git", "show", "refs/heads/main:marketing/coupon.md") == replacement
    assert _git(docs_repo, "diff", "--cached", "--name-only") == "local-notes.txt"
    assert "local-notes.txt" not in _git(tmp_path / "business-docs-remote.git", "ls-tree", "-r", "--name-only", "refs/heads/main")
    assert service.get_run_items(run_id=run.run_id)[0].apply_status == "applied"
    with sqlite3.connect(store.db_path) as connection:
        action = connection.execute(
            "SELECT action, user_id, actor_role FROM business_doc_update_actions WHERE update_id = ?",
            (item.update_id,),
        ).fetchone()
    assert action == ("applied", "admin-1", "admin")


@pytest.mark.anyio
async def test_manual_apply_can_retry_a_rejected_git_push(tmp_path: Path) -> None:
    replacement = "# 优惠券\n\n领取后进入待激活状态。"
    _, doc, _, service = _workspace(
        tmp_path,
        [_proposal(replacement=replacement), _review(), _proposal(replacement=replacement, resolution="agree")],
    )
    run = service.create_full_run()
    await service.process_pending_items()
    item = service.get_run_items(run_id=run.run_id)[0]
    remote = tmp_path / "business-docs-remote.git"
    hook = remote / "hooks/pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)

    with pytest.raises(ValueError, match="Git 提交或推送失败"):
        service.apply_update(update_id=item.update_id, user_id="admin-1", actor_role="admin")

    assert service.get_update_detail(update_id=item.update_id).status == "pending"
    assert doc.read_text(encoding="utf-8") == replacement + "\n"
    assert _git(doc.parents[1], "rev-list", "--count", "@{upstream}..HEAD") == "1"

    hook.unlink()
    detail = service.apply_update(update_id=item.update_id, user_id="admin-1", actor_role="admin")

    assert detail.status == "applied"
    assert _git(remote, "show", "refs/heads/main:marketing/coupon.md") == replacement


@pytest.mark.anyio
async def test_no_update_result_stops_without_review_or_pending_proposal(tmp_path: Path) -> None:
    no_update = {
        "result_type": "no_update_needed",
        "stable_business_change": False,
        "summary": "仅测试与内部重构。",
        "reason": "对外业务语义未变化。",
        "confidence": 0.99,
        "evidence": [{"repo_key": "coupon", "commit": "HEAD", "file": "coupon.py", "symbol": "internal"}],
        "risk_flags": [],
        "cross_repo_conflict": False,
        "replacement_markdown": "",
    }
    _, doc, _, service = _workspace(tmp_path, [no_update])

    run = service.create_full_run()
    await service.process_pending_items()

    item = service.get_run_items(run_id=run.run_id)[0]
    assert item.result_type == "no_update_needed"
    assert item.review_status == "not_required"
    assert item.update_id is None
    assert "默认不更新" in service.runtime_client.prompts[0]
    assert "不得启动子 Agent" in service.runtime_client.prompts[0]
    assert service.runtime_client.metadata[0]["output_schema"]["properties"]["result_type"]["enum"] == [
        "update_required",
        "no_update_needed",
        "new_doc_candidate",
    ]
    assert doc.read_text(encoding="utf-8") == "# 优惠券\n\n优惠券领取后可直接使用。\n"


@pytest.mark.anyio
async def test_empty_agent_reply_records_readable_failure(tmp_path: Path) -> None:
    _, _, _, service = _workspace(tmp_path, [])
    service.runtime_client = EmptyRuntime([])

    run = service.create_full_run()
    await service.process_pending_items()

    item = service.get_run_items(run_id=run.run_id)[0]
    assert item.status == "failed"
    assert item.error_message == "Agent 未返回最终 JSON。"


@pytest.mark.anyio
async def test_risk_flag_forces_incremental_proposal_to_manual_queue(tmp_path: Path) -> None:
    replacement = "# 优惠券\n\n领取后冻结资金并等待激活。"
    repo, doc, store, service = _workspace(
        tmp_path,
        [
            _proposal(replacement=replacement, risk_flags=["funds"]),
            _review(risk_flags=["funds"]),
            _proposal(replacement=replacement, risk_flags=["funds"], resolution="agree"),
        ],
    )
    base_sha = _git(repo, "rev-parse", "HEAD")
    store.set_cursor(repo_key="coupon", analyzed_head=base_sha, last_run_id="baseline")
    (repo / "coupon.py").write_text("STATUS = 'funds_frozen'\n", encoding="utf-8")
    _git(repo, "add", "coupon.py")
    _git(repo, "commit", "-m", "funds rule")
    run = service.enqueue_incremental(repo_key="coupon")

    await service.process_pending_items()

    item = service.get_run_items(run_id=run.run_id)[0]
    assert item.apply_status == "manual_required"
    assert service.get_update_detail(update_id=item.update_id).status == "pending"
    assert "直接使用" in doc.read_text(encoding="utf-8")


@pytest.mark.anyio
async def test_invalid_review_flags_are_explained_redacted_and_retryable(tmp_path: Path) -> None:
    replacement = "# 优惠券\n\n领取后进入待激活状态。"
    invalid_review = {
        **_review(),
        "risk_flags": ["资金风险"],
        "api_key": "must-not-be-stored",
    }
    _, _, _, service = _workspace(
        tmp_path,
        [_proposal(replacement=replacement), invalid_review],
    )

    run = service.create_full_run()
    await service.process_pending_items()

    failed = service.get_run_items(run_id=run.run_id)[0]
    review_prompt = service.runtime_client.prompts[1]
    assert failed.status == "failed"
    assert "审核 Agent risk_flags 无效" in failed.error_message
    assert '"risk_flags":["资金风险"]' in failed.error_message
    assert "must-not-be-stored" not in failed.error_message
    assert '"api_key":"[REDACTED]"' in failed.error_message
    assert "funds" in review_prompt
    assert "risk_control" in review_prompt
    assert "无风险时输出空数组 []" in review_prompt

    service.runtime_client.responses.extend(
        [_proposal(replacement=replacement), _review(), _proposal(replacement=replacement, resolution="agree")]
    )
    service.retry_run(run_id=run.run_id)
    await service.process_pending_items()

    retried = service.get_run_items(run_id=run.run_id)[0]
    assert retried.status == "completed"
    assert retried.error_message is None


@pytest.mark.anyio
async def test_active_incremental_run_is_serialized_and_catches_up_latest_head(tmp_path: Path) -> None:
    replacement = "# 优惠券\n\n领取后进入待激活状态。"
    repo, _, store, service = _workspace(
        tmp_path,
        [_proposal(replacement=replacement), _review(), _proposal(replacement=replacement, resolution="agree")],
    )
    base_sha = _git(repo, "rev-parse", "HEAD")
    store.set_cursor(repo_key="coupon", analyzed_head=base_sha, last_run_id="baseline")
    (repo / "coupon.py").write_text("STATUS = 'pending_activation'\n", encoding="utf-8")
    _git(repo, "add", "coupon.py")
    _git(repo, "commit", "-m", "first change")
    first_head = _git(repo, "rev-parse", "HEAD")
    first = service.enqueue_incremental(repo_key="coupon")

    (repo / "coupon.py").write_text("STATUS = 'activated'\n", encoding="utf-8")
    _git(repo, "add", "coupon.py")
    _git(repo, "commit", "-m", "second change")
    latest_head = _git(repo, "rev-parse", "HEAD")
    same_active = service.enqueue_incremental(repo_key="coupon", current_head=latest_head)

    assert same_active.run_id == first.run_id
    assert same_active.head_sha == first_head

    await service.process_pending_items()

    runs = service.list_runs()
    catch_up = next(run for run in runs if run.run_id != first.run_id)
    assert catch_up.base_sha == first_head
    assert catch_up.head_sha == latest_head
    assert catch_up.status == "pending"


@pytest.mark.anyio
async def test_newer_proposal_supersedes_pending_proposal(tmp_path: Path) -> None:
    first_text = "# 优惠券\n\n领取后进入待激活状态。"
    second_text = "# 优惠券\n\n领取后进入待激活状态，并在过期后失效。"
    repo, _, _, service = _workspace(
        tmp_path,
        [
            _proposal(replacement=first_text),
            _review(),
            _proposal(replacement=first_text, resolution="agree"),
            _proposal(replacement=second_text),
            _review(),
            _proposal(replacement=second_text, resolution="agree"),
        ],
    )
    first_run = service.create_full_run()
    await service.process_pending_items()
    first_item = service.get_run_items(run_id=first_run.run_id)[0]

    (repo / "coupon.py").write_text("STATUS = 'pending_activation_or_expired'\n", encoding="utf-8")
    _git(repo, "add", "coupon.py")
    _git(repo, "commit", "-m", "add expiration")
    second_run = service.create_full_run()
    await service.process_pending_items()
    second_item = service.get_run_items(run_id=second_run.run_id)[0]

    assert service.get_update_detail(update_id=first_item.update_id).status == "superseded"
    assert service.get_update_detail(update_id=second_item.update_id).status == "pending"


@pytest.mark.anyio
async def test_cross_repo_conflict_is_manual_and_only_trigger_repo_cursor_advances(tmp_path: Path) -> None:
    replacement = "# 优惠券\n\n领取后进入待激活状态。"
    repo, _, store, service = _workspace(
        tmp_path,
        [
            _proposal(replacement=replacement, risk_flags=["cross_repo_conflict"], cross_repo_conflict=True),
            _review(risk_flags=["cross_repo_conflict"]),
            _proposal(
                replacement=replacement,
                risk_flags=["cross_repo_conflict"],
                resolution="agree",
                cross_repo_conflict=True,
            ),
        ],
    )
    gateway = _add_configured_repo(tmp_path, name="gateway", role="gateway")
    base_sha = _git(repo, "rev-parse", "HEAD")
    gateway_sha = _git(gateway, "rev-parse", "HEAD")
    store.set_cursor(repo_key="coupon", analyzed_head=base_sha, last_run_id="baseline")
    store.set_cursor(repo_key="gateway", analyzed_head=gateway_sha, last_run_id="gateway-baseline")
    (repo / "coupon.py").write_text("STATUS = 'pending_activation'\n", encoding="utf-8")
    _git(repo, "add", "coupon.py")
    _git(repo, "commit", "-m", "domain change")

    run = service.enqueue_incremental(repo_key="coupon")
    snapshots = json.loads(run.snapshot_json)
    await service.process_pending_items()

    item = service.get_run_items(run_id=run.run_id)[0]
    assert [snapshot["role"] for snapshot in snapshots] == ["domain", "gateway"]
    assert item.apply_status == "manual_required"
    assert store.get_cursor("coupon").analyzed_head == _git(repo, "rev-parse", "HEAD")
    assert store.get_cursor("gateway").analyzed_head == gateway_sha


def test_full_snapshot_orders_domain_evidence_before_gateway_and_frontend(tmp_path: Path) -> None:
    _, _, _, service = _workspace(tmp_path, [])
    config_path = tmp_path / "workspace/config/code-auto-review.json"
    _add_configured_repo(tmp_path, name="frontend", role="frontend")
    _add_configured_repo(tmp_path, name="gateway", role="gateway")
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["repositories"] = list(reversed(payload["repositories"]))
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    run = service.create_full_run()
    snapshots = json.loads(run.snapshot_json)

    assert [snapshot["role"] for snapshot in snapshots] == ["domain", "gateway", "frontend"]


def test_missing_business_document_root_marks_full_run_failed(tmp_path: Path) -> None:
    _, doc, _, service = _workspace(tmp_path, [])
    docs_root = doc.parents[1]
    shutil.rmtree(docs_root)

    run = service.create_full_run()

    assert run.status == "failed"
    assert run.total_items == 0
    assert "业务文档根目录不存在" in run.error_message


def test_empty_business_document_root_marks_full_run_failed(tmp_path: Path) -> None:
    _, doc, _, service = _workspace(tmp_path, [])
    doc.unlink()

    run = service.create_full_run()

    assert run.status == "failed"
    assert run.total_items == 0
    assert "没有可分析的 Markdown" in run.error_message


def test_failed_empty_full_run_reopens_after_documents_are_restored(tmp_path: Path) -> None:
    _, doc, _, service = _workspace(tmp_path, [])
    original = doc.read_text(encoding="utf-8")
    doc.unlink()

    failed = service.create_full_run()
    doc.write_text(original, encoding="utf-8")
    reopened = service.create_full_run()

    assert reopened.run_id == failed.run_id
    assert reopened.status == "pending"
    assert reopened.total_items == 1
    assert reopened.error_message is None


def test_business_documents_can_be_read_through_workspace_knowledge_symlink(tmp_path: Path) -> None:
    _, _, store, original_service = _workspace(tmp_path, [])
    knowledge_mount = tmp_path / "workspace/coinex/knowledge"
    shared_knowledge = tmp_path / "shared-knowledge"
    knowledge_mount.rename(shared_knowledge)
    knowledge_mount.symlink_to(shared_knowledge, target_is_directory=True)
    service = BusinessDocUpdateService(
        base_dir=tmp_path,
        business_docs_root=knowledge_mount / "business-docs",
        skills_root=tmp_path / ".claude/skills",
        store=store,
        runtime_client=original_service.runtime_client,
        project_config_path="workspace/config/code-auto-review.json",
    )

    run = service.create_full_run()
    items = service.get_run_items(run_id=run.run_id)

    assert run.status == "pending"
    assert run.total_items == 1
    assert items[0].source_path == "workspace/coinex/knowledge/business-docs/marketing/coupon.md"
    assert service.list_updates(source_path="/coinex/knowledge/business-docs/marketing/coupon.md") == []


def test_recovery_requeues_running_item_and_recreates_missed_incremental_run(tmp_path: Path) -> None:
    repo, _, store, service = _workspace(tmp_path, [])
    base_sha = _git(repo, "rev-parse", "HEAD")
    store.set_cursor(repo_key="coupon", analyzed_head=base_sha, last_run_id="baseline")
    (repo / "coupon.py").write_text("STATUS = 'pending_activation'\n", encoding="utf-8")
    _git(repo, "add", "coupon.py")
    _git(repo, "commit", "-m", "missed change")

    recovered = service.recover_incremental_runs()
    claimed = store.claim_pending_items(limit=1)
    assert len(recovered) == 1
    assert claimed[0].status == "running"

    service.recover_incremental_runs()

    assert service.get_run_items(run_id=recovered[0].run_id)[0].status == "pending"


def test_invalid_scope_cannot_escape_business_document_root(tmp_path: Path) -> None:
    _, _, _, service = _workspace(tmp_path, [])
    config_path = tmp_path / "workspace/config/code-auto-review.json"
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["repositories"][0]["business_doc_scopes"] = ["../secrets.md"]
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="路径无效"):
        service.repository_configs()

    with pytest.raises(FileNotFoundError):
        service.list_updates(source_path="workspace/coinex/knowledge/business-docs/../../config/code-auto-review.json")

    with pytest.raises(FileNotFoundError):
        service.list_updates(source_path="/coinex/knowledge/business-docs/../../config/code-auto-review.json")


@pytest.mark.anyio
async def test_public_knowledge_path_can_read_pending_updates(tmp_path: Path) -> None:
    _, _, _, service = _workspace(
        tmp_path,
        [
            _proposal(replacement="# 优惠券\n\n优惠券领取后进入待激活状态。"),
            _review(),
            _proposal(replacement="# 优惠券\n\n优惠券领取后进入待激活状态。", resolution="agree"),
        ],
    )
    run = service.create_full_run()
    await service.process_pending_items(limit=1)

    public_path = "/coinex/knowledge/business-docs/marketing/coupon.md"
    updates = service.list_updates(source_path=public_path)
    badge = service.build_doc_update_badge(source_path=public_path)

    assert len(updates) == 1
    assert updates[0].source_path == "workspace/coinex/knowledge/business-docs/marketing/coupon.md"
    assert updates[0].status == "pending"
    assert badge == {
        "type": "business_doc_update",
        "pending_count": 1,
        "latest_update_id": updates[0].update_id,
    }
    assert service.get_run(run_id=run.run_id).status == "completed"


def test_missing_required_repository_fails_full_run(tmp_path: Path) -> None:
    config = tmp_path / "workspace/config/code-auto-review.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {
                "repositories": [
                    {
                        "repo_full_name": "example-org/missing",
                        "workspace_repo_path": str(tmp_path / "missing"),
                        "default_branch": "main",
                        "business_doc_enabled": True,
                        "business_doc_role": "domain",
                        "business_doc_priority": "P0",
                        "business_doc_scopes": ["risk.md"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    service = BusinessDocUpdateService(
        base_dir=tmp_path,
        business_docs_root=tmp_path / "workspace/coinex/knowledge/business-docs",
        skills_root=tmp_path / ".claude/skills",
        store=BusinessDocUpdateStore(str(tmp_path / "assistant.sqlite3")),
        runtime_client=SequencedRuntime([]),
        project_config_path="workspace/config/code-auto-review.json",
    )

    run = service.create_full_run()

    assert run.status == "failed"
    assert "不是 Git 仓库" in run.error_message


def test_store_migrates_legacy_actions_without_losing_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "assistant.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE business_doc_update_actions (
                action_id TEXT PRIMARY KEY,
                update_id TEXT NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('applied', 'ignored')),
                user_id TEXT NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL
            );
            INSERT INTO business_doc_update_actions VALUES ('a1', 'u1', 'applied', 'user1', 'ok', 'now');
            """
        )

    BusinessDocUpdateStore(str(db_path))

    with sqlite3.connect(db_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(business_doc_update_actions)")}
        row = connection.execute("SELECT action, actor_role FROM business_doc_update_actions WHERE action_id = 'a1'").fetchone()
    assert "actor_role" in columns
    assert row == ("applied", "user")


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise AssertionError((result.stderr or result.stdout).strip())
    return result.stdout.strip()
