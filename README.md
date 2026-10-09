# ai-prd

Agents for a product and engineering team. ai-prd gives people one assistant that answers from the company's code, requirements and docs, with sources. Its background agents review pull requests, review changed requirements, keep business docs in sync with code and scan files and URLs for other services, without anyone starting them.

It was built at Vinotech (engineering for the CoinEx exchange) in 2026 and used by 100+ product and engineering staff until the company wound down in September 2026. This is a public snapshot: internal names, emails, domains and IDs have been replaced, and deployment files are left out.

For the problem, design decisions and results, read the [case study](CASE_STUDY.md).

## Agents that run on their own

| Agent | Trigger | What it does | Output |
| --- | --- | --- | --- |
| PR review | GitHub poll every 5 minutes; first review request on a non-draft PR | Reviews the PR in its own git worktree, against the linked ClickUp requirement and a review policy | Report emailed to the PR author |
| Requirement review | Hourly ClickUp sync; trivial edits skipped | Checks the changed spec with a requirement-check Skill | Review file with an unread badge |
| Business-doc update | After each hourly code sync | A generator agent proposes doc edits; a separate reviewer agent checks them | Git commit when the gate passes, otherwise a human queue |
| Security scan | A signed request from another internal service | Checks a file, folder or URL read-only, treating its content as untrusted | JSON verdict (safe or unsafe) with evidence |
| Assistant | A person's question | Answers from company knowledge, Skills and tools | Streamed answer with a citation trace |

## The doc-update gate

1. A generator agent proposes the edit and rates its confidence.
2. A reviewer agent checks it adversarially in a separate session. It sees the proposal and its reasons, but not the generator's tool calls, so it has to check the code itself.
3. The generator revises once.
4. The edit commits automatically only if both agents agree at 0.85 confidence or higher, neither raises a risk flag (funds, permissions, risk control, rule deletion and others), no other repo conflicts with it, and the doc file is unchanged since the run began. Everything else waits in a human queue.

The gate is checked in code, not by either agent: [`automation.py`](source/backend/app/business/business_doc_updates/automation.py).

## Agent runtime

- **Two vendors, one interface.** The same tools and events run on the Claude Agent SDK or on `codex app-server` over JSON-RPC, chosen per deployment ([`agent_runtime/`](source/backend/app/integrations/agent_runtime/)). Production started on Claude and later moved to Codex. `AI_PROVIDER=mock` runs everything against a fake runtime.
- **Tools per job.** Each job gets only the tools it needs, through an in-process MCP server on Claude or as dynamic tools on Codex: the internal knowledge base, the test-data platform, git refs, and URL fetch for security scans.
- **Sandbox.** Agents run in an OS sandbox: bubblewrap and AppArmor on the Claude runtime, Codex's own sandbox on Codex. Company knowledge is read-only; only a personal folder and `/tmp` are writable. In the chat assistant, a git guard reverts edits to tracked files in a code repo after each turn; new files or a moved HEAD raise a warning.
- **Untrusted input.** The security-scan agent treats scanned content as evidence, never as instructions, and fetches URLs on a budget.
- **Approvals.** Tool approvals appear as cards in the UI and are saved before the call runs. Test-data writes run only after the user types an exact confirmation phrase. Each user's agent access is approved by an admin.
- **Limits.** Turn caps on the Claude runtime, one running turn per session, and stuck PR reviews are marked failed after an hour.

## Layout

| Path | Contents |
| --- | --- |
| `source/backend` | FastAPI app (Python 3.11+) and its tests |
| `source/frontend` | React and Vite |
| `source/docs` | Design notes, in Chinese |
| `source/scripts` | ClickUp sync and maintenance scripts |

## Tests

Backend: 425 pass. Eight are skipped because the repo-level `deployment/` and `.claude/skills` folders are not part of this snapshot.

```sh
cd source/backend
uv run --extra dev pytest
```

Frontend: 105 pass.

```sh
cd source/frontend
pnpm install
node --test src/utils/*.test.js
```

Running it as a service needs the original integrations configured: ClickUp, the internal knowledge base, the test-data platform, GitHub access and SMTP. See [`config.py`](source/backend/app/core/config.py).
