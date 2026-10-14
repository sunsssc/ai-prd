# ai-prd

Agents for a product and engineering team. ai-prd gives people one assistant that answers from the company's code, requirements and docs, with sources. Its background agents review pull requests, review changed requirements, keep business docs in sync with code and scan files and URLs for other services, without anyone starting them.

It was built at Vinotech (engineering for the CoinEx exchange) in 2026 and used by 100+ product and engineering staff until the company wound down in September 2026. This is a public snapshot: internal names, emails, domains and IDs have been replaced, and deployment files are left out. The original repository was private, so its commit history is not included here.

For the problem, design decisions and results, read the [case study](CASE_STUDY.md).

## Agents that run on their own

| Agent | Trigger | What it does | Output |
| --- | --- | --- | --- |
| PR review | GitHub poll every 5 minutes; open non-draft PR with a pending review request; deduplicated by PR head | Reviews the PR in its own git worktree, against the linked ClickUp requirement and a review policy | Report emailed to the PR author |
| Requirement review | Hourly ClickUp sync; trivial edits skipped | Checks the changed spec with a requirement-check Skill | Review file with an unread badge |
| Business-doc update | After an hourly code sync brings new commits | A generator agent proposes doc edits; a separate reviewer agent checks them | Git commit when the gate passes, otherwise a human queue |
| Security scan | A signed request from another internal service | Inspects a file, folder or URL with restricted tools and sandbox settings; prompts mark its content as untrusted | JSON verdict (safe or unsafe) with evidence, or failed status when target URL content is unavailable |
| Assistant | A person's question | Answers from company knowledge, Skills and tools | Streamed answer with a citation trace |

The hourly ClickUp job imports requirements into the local knowledge base. Authorized UI edits can write content back to ClickUp after permission and conflict checks, then refresh the local copy. Automatic requirement-review report posting is off by default.

## The doc-update gate

1. A generator agent proposes the edit and rates its confidence.
2. A reviewer agent is instructed to check it against code evidence in a separate session. It sees the proposal and its reasons without the generator's tool-call history.
3. The generator reviews the feedback once. If it changes any proposal field other than its agreement decision, the proposal goes to a human; the old review cannot approve new content.
4. The edit commits automatically only if both agents agree at 0.85 confidence or higher, neither raises a risk flag (funds, permissions, risk control, rule deletion and others), the reviewer lists no missing evidence or boundary violations, neither reports a cross-repository conflict, and the doc file is unchanged since the run began. Everything else waits in a human queue.

Code enforces these conditions on the agents' returned judgments and the file state; it does not independently establish semantic correctness: [`automation.py`](source/backend/app/business/business_doc_updates/automation.py).

## Agent runtime

- **Two vendors, one interface.** A shared session and event interface supports the Claude Agent SDK and `codex app-server` over JSON-RPC, selected per task, with a global default ([`agent_runtime/`](source/backend/app/integrations/agent_runtime/)). Production started on Claude and later moved to Codex, with gpt-5.6-terra as the primary production model. `AI_PROVIDER=mock` uses a fake runtime by default; task profiles can override it.
- **Task profiles.** `AI_TASK_RUNTIMES` can override the provider, model and connection settings for each task. For example, keep Codex for chat and review while selecting Claude for security scans and Skill import analysis, which require native-tool allowlists. Unsupported combinations fail explicitly; there is no automatic fallback.
- **Guardrails.** Sandboxes restrict writes; the Codex adapter does not provide a per-job filesystem read allowlist. Supported Codex MCP requests can open approval cards; Claude has no equivalent approval-card bridge. Test-data writes require a separate typed confirmation. PR jobs share a deadline, and URL scans require a successful bounded fetch before a verdict.

See [configuration and enforcement boundaries](source/docs/architecture/task-runtimes-and-guardrails.md) for a mixed-runtime example and the limits of each control.

## Layout

| Path | Contents |
| --- | --- |
| `source/backend` | FastAPI app (Python 3.11+) and its tests |
| `source/frontend` | React and Vite |
| `source/docs` | Design notes and runtime configuration |
| `evaluation` | Sanitized methodology, counts and arithmetic check |
| `source/scripts` | ClickUp sync and maintenance scripts |

## PR review harness

The review harness uses two fresh sessions with the configured model: exploration gathers candidate evidence and change coverage; verification re-reads the pinned base/head source, checks counterevidence and looks for missed changes before producing the final report. The packaged `pr-evidence-review` Skill is loaded explicitly by the service. Only the verified-stage report enters the existing report/email flow; a failed or empty stage fails the job, and oversized candidate notes are rejected rather than silently truncated. These are model-guided evidence checks, not an automatic guarantee of correctness. The second session adds a model turn and its cost. The Codex adapter also waits for retryable errors and uses completed answer messages for the saved report. Report risk metadata recognizes the policy’s P0–P3 finding headings and ignores fenced examples.

Authors can add a `Related PRs` or `关联 PR` section with full GitHub PR URLs. The service fetches explicitly linked PRs from enabled repositories in the same configured organization, pins each base/head pair, and supplies all contexts to both review stages. It rejects missing or disallowed context and changes detected before delivery. One combined report records the reviewed SHAs; linked PRs do not imply simultaneous deployment. See [usage, limits and retry behavior](source/docs/product/pr-code-cross-repo-review.md).

The offline results below separate the original QA evaluation from a 20-case PR development replay of the two-stage harness. The replay measures the complete service flow with the same model, not a held-out production test or an evaluation of the cross-repository addition. For background review, I prioritize reducing developers' time spent checking noise over minimizing model usage. Human time savings were not quantified.

## Offline evaluation

During development, with gpt-5.6-terra at high reasoning unchanged, the two-stage harness completed a 20-case PR development replay: valid findings improved from 17/26 (65%) to 20/24 (83%), while known-defect recall stayed at 11/13 (85%). Unresolved findings remain in the denominator. The separate original QA evaluation passed 19/20 (95%). Codex graded against frozen references and source evidence; the results did not receive an independent human review. Two sessions per review increased compute and latency; these exposed development cases do not establish a held-out advantage or quantify production finding acceptance. See the [sanitized evaluation appendix](evaluation/README.md) for the rubric, per-case counts, full-text baseline, cost and limitations.

## Tests

Backend: 553 pass. Eight are skipped because the repo-level `deployment/` and `.claude/skills` folders are not part of this snapshot.

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
