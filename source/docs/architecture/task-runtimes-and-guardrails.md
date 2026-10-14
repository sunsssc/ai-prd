# Task runtimes and guardrails

## Select a runtime per task

`AI_PROVIDER`, `AI_MODEL`, `AI_API_KEY`, `AI_BASE_URL` and `AI_CLI_PATH` are the global defaults. `AI_TASK_RUNTIMES` is a JSON object with optional task profiles:

| Task key | Consumer |
| --- | --- |
| `assistant` | Chat assistant and ordinary Skill creation |
| `code_review` | Both PR review stages |
| `requirement_review` | Requirement review worker |
| `business_doc_update` | Document generator and reviewer |
| `security_scan` | File, folder and URL scans |
| `skill_read` | Skill import analysis (`/skills/import/generate`) |

For example, use Codex by default and Claude for tasks requiring a native-tool allowlist:

```sh
AI_PROVIDER=codex
AI_MODEL="<your-codex-model>"
AI_TASK_RUNTIMES='{"security_scan":{"provider":"claude","model":"<your-claude-model>"},"skill_read":{"provider":"claude","model":"<your-claude-model>"}}'
```

Replace model placeholders with models available to your deployment. Each profile accepts `provider`, `model`, `api_key`, `base_url` and `cli_path`. Supported providers are `claude`, `codex` and `mock`; `claude_code` and `anthropic` are aliases for Claude. Unknown task names, providers and profile fields are rejected.

Omitted fields inherit global settings only when the selected provider matches the global provider. Switching providers requires an explicit model and does not inherit the global API key, endpoint or CLI path. Without an explicit key or CLI path, the selected adapter uses its existing credential and executable discovery behavior; the Claude adapter passes the validated absolute CLI path to the SDK, including when discovery starts from an empty setting; Claude defaults to its Anthropic endpoint. Explicit empty strings opt out of inherited string settings. Claude supports a profile's custom `base_url`; Codex rejects it. Supply credentials through deployment secrets, not committed examples.

There is no automatic provider fallback. Native-tool allowlists are supported by the Claude adapter; the Codex adapter rejects an explicit allowlist before starting or resuming a session. Security scans and Skill import analysis therefore need a Claude profile in a Codex-default deployment. Ordinary Skill creation uses the assistant profile. Existing task-specific effort and turn settings remain separate from these profiles. Restart the service after changing configuration: settings and service instances are cached.

Implementation: [config](../../backend/app/core/config.py), [runtime factory and service wiring](../../backend/app/core/dependencies.py), [Skill import route](../../backend/app/api/workspace.py). [Tests](../../backend/tests/test_task_runtimes.py) cover profile parsing, inheritance, cross-provider isolation of configuration, capability rejection and consumer wiring, without external model calls.

## Enforcement boundaries

- **Sandbox:** Claude uses bubblewrap and AppArmor and refuses to start when its sandbox is unavailable. Codex uses its own sandbox. The default workspace policy protects company knowledge from writes and allows personal and temporary writes. The Codex adapter does not translate read mounts into a filesystem read allowlist; this is not uniform per-job read isolation.
- **Git guard:** chat turns roll back tracked edits only when the repository started clean and HEAD is unchanged. New files are retained with a warning. A dirty starting state or moved HEAD prevents rollback and raises a warning.
- **Tools:** custom tools are registered per task. Claude uses the SDK `tools` option for native inventory, separately from permission rules. Sandbox policy is another independent control.
- **Approval cards:** the interactive bridge handles supported Codex MCP tool approval requests (form mode with an empty schema, matched to the active thread and turn). Other elicitation forms are declined. The decision is persisted before resuming the call and pending approvals expire. The Claude adapter terminates the turn when it detects an approval-required tool error; it does not open the same UI approval card. Other tool errors can be returned to the model. Test-data writes have a separate application gate: an exact typed confirmation, valid for ten minutes.
- **Untrusted input:** scan prompts tell the model to treat target content as evidence and not execute it. Bash remains available; this is not an execution ban or a prompt-injection guarantee. Code validates verdict fields and enforces a fetch budget. The service resolves each URL hop, rejects non-public addresses and connects only to the validated IPs, keeping the original Host header and TLS hostname verification. Environment proxies are disabled for these fetches. A URL scan must first fetch the submitted URL successfully with nonempty, size-bounded content; that fetch consumes the same budget as follow-up requests. A safe verdict means no clear malicious evidence in inspected content.
- **Access:** Google sign-in, a department check and admin-granted agent access apply. Company-domain users absent from a successfully loaded OA directory are blocked. An unconfigured or failed directory lookup is not evidence of departure; other account checks still apply.
- **Deadlines:** PR exploration and verification share a one-hour job deadline by default. Overdue model calls are cancelled and late reports are refused. Synchronous Git/GitHub preparation keeps its own operation timeouts, with the job deadline checked before execution and delivery. Security-scan stale-job recovery changes status but does not cancel a running model call. Claude turn caps and one running turn per chat session are separate limits.

See also [PR scheduling](../product/pr-code-auto-review.md) and [linked-PR context and delivery checks](../product/pr-code-cross-repo-review.md).

## Document-update boundaries

The generator and reviewer use separate sessions with the same task runtime. The reviewer receives the proposal, its reasons, source-document text, diff and repository references, without the generator's tool-call history. It is instructed to inspect code evidence independently. The application does not certify which source was read or whether either model's semantic judgment is correct.

The automatic gate checks the models' agreement, confidence and reported risk/evidence flags, equality of the reviewed and final proposals (except the agreement field), and the source-document hash. These are enforceable checks on outputs and state, not independent proof that no risk or cross-repository conflict exists. Otherwise the proposal enters the human queue.

Business docs are maintained in their own git repository. Later R&D documents written in ClickUp are not merged into it. Applying an edit fetches and compares the local docs checkout with the remote; a mismatch blocks the edit. There is no scheduled pull for that checkout, so a direct push requires an operator to refresh it. ClickUp requirement imports and authorized UI edits are separate flows; automatic requirement-review report posting is off by default.
