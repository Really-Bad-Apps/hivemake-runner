# Codex connector: phase 1

Status: experimental transport and retained terminal connection implemented;
live attachment is not enabled in the daemon. Terminal connections explicitly
apply **Workspace access with `on-request`**. This is the accepted permission
policy, replacing the original requirement to inherit terminal permissions.
This is not an end-to-end ticket reply implementation.

## Component boundary

`runner/codex_connector.py` translates JSONL framing on stdin/stdout into text
WebSocket messages over an existing local Unix socket. It forwards JSON objects
without interpreting RPC methods, changing policies, or managing conversations.
It uses the runner's existing aiohttp dependency; no Node bridge dependency is
needed. The upstream codex-acp package remains unmodified and pinned separately.

`scripts/codex-connector` is the executable for the adapter's `CODEX_PATH`.
Its only accepted argument is `app-server`. Set `HIVEMAKE_CODEX_SOCKET` to an
absolute existing socket path and put the runner virtualenv's bin directory on
PATH. Missing sockets fail; there is no fallback server launch. The connector
is a child of the adapter, not a standalone service. EOF or connection failure
closes that connection only; no cancel, session-close, or shutdown RPC is sent.

The transport has an 8 MiB message limit, backpressure, bounded connection setup,
and generic diagnostics that do not log message contents. It is local-only in
this first version. A future runner attachment manager will own one retained
adapter/connector pair per attached terminal session. That manager is not built.

`runner/terminal_session.py` provides the retained ACP connection for that future
manager. It loads the supplied exact session UUID, applies `workspace-write`
through ACP, and reuses its adapter for successive prompts. It never creates a
new session, injects autonomous instructions, or sends a session cancel/close.
Failures detach without automatic replay; possible submission remains uncertain.
Its local concurrency guard does not establish that the native terminal is idle.
Ownership, workspace association, liveness and native idle validation still
belong to the future attachment layer; this class is not a registration API.

Permission callbacks remain pending while the original terminal handles the
decision. Returning an automatic approval or denial here would race the human.
Local pending callbacks are retired when the turn finishes or the connection
closes. This behavior is separate from autonomous `ACPSession` callbacks, which
are unchanged.

## Reproducible live probe

Use only a disposable native Codex terminal session. The probe can change that
session's configuration through upstream adapter behavior. Start it with
`codex -s read-only -a never` in a disposable directory and send a harmless prompt
so its thread exists. Keep the terminal open and idle. Obtain its exact UUID.

Run with the runner virtualenv active:

```bash
python scripts/probe_codex_connector.py \
  --thread <disposable-session-uuid> \
  --cwd /absolute/disposable/workspace \
  --adapter /absolute/path/to/codex-acp \
  --socket /absolute/path/to/app-server-control.sock \
  --state-db /absolute/path/to/state_5.sqlite \
  --report /private/path/probe-report.json \
  --workspace-access \
  --ack-disposable-session
```

The probe uses Python ACP SDK initialize/load/prompt and expects a unique text
answer. It captures before/load/prompt/detach snapshots of cwd, sandbox policy,
approval mode, model and effort. Database reads are diagnostic evidence only;
they are not the proposed live-session discovery or routing implementation.
Reports contain local paths; keep them private. No provider/owner credentials
are written. Reports are atomically saved with owner-only file permissions,
including when adapter startup fails. Workspace mode exercises the retained terminal connection and
leaves permission decisions to the native terminal. The original failing probe
is still reproducible with `--preserve-read-only` instead of `--workspace-access`.
Exit 2 indicates a failed compatibility gate. The workspace check accepts only
the expected workspace/temporary-directory writes, restricted network and
`on-request`, while checking cwd/model/effort are unchanged. Instructions and
MCP configuration still need separate compatibility checks.

For an interactive approval check, supply `--prompt /private/path/prompt.txt`
with a harmless request that explicitly asks for an escalated `printf` command.
Observe and decide it in the original terminal. The report records permission
callback count, response, and stop reason. Custom prompts do not match the
default random response marker, so their exit status alone does not establish
approval-routing success: inspect the terminal and report together.

## Verified results (2026-10-21)

Native Codex 0.163.0, Python ACP SDK 0.12.1, disposable CLI sessions:

| Check | codex-acp 2.1.1 | codex-acp 2.2.2 |
|---|---|---|
| ACP prompt returns expected assistant text | Pass | Pass |
| Same text appears in original native terminal | Pass | Pass |
| Original terminal answers a new user prompt after detach | Pass | Pass |
| Recorded settings unchanged immediately after load | Pass | Pass |
| Recorded settings unchanged after prompt | **Fail** | **Fail** |

With documented `INITIAL_AGENT_MODE=read-only` and
`CODEX_CONFIG={"sandbox_mode":"read-only","approval_policy":"never"}`,
the sandbox stayed read-only but the prompt changed `approval_mode` from `never`
to `on-request`. Detaching did not restore it. Restoring after delivery would not
solve the problem: execution would already have used the wrong policy.

In the pinned adapter's `AgentMode.ts`, read-only uses approval `on-request`.
`CodexAcpClient.sendPrompt()` supplies that preset explicitly to every native
`turn/start`, overriding configuration. No exposed mode represents the tested
read-only/never combination. This is an adapter behavior, not a framing problem.

Related upstream work:

- https://github.com/agentclientprotocol/codex-acp/issues/477
- https://github.com/agentclientprotocol/codex-acp/pull/503
- https://github.com/agentclientprotocol/codex-acp/pull/348

The accepted workspace preset avoids the original permission-preservation gate
without modifying upstream. A subsequent native Codex 0.163.0 / adapter 2.1.1
probe using the retained connection passed: workspace/on-request was applied,
cwd/model/effort were unchanged, the answer appeared in the original terminal,
and that terminal remained usable after detach. A separate approval probe showed
the request in the original terminal; rejecting there cancelled the ACP turn.
Accepting the harmless `printf` diagnostic in another native terminal test let
the ACP turn finish with `end_turn`, command exit code 0 and the expected output.
Both terminals remained usable after detach. The ACP client itself supplied
neither decision.

Do not silently rewrite native RPC policies or patch the installed adapter.
Busy turns, reconnect/retry, instructions/MCP preservation, session ownership
and live-session validation remain release gates for public attachment/delivery.

Automated checks cover bidirectional framing, embedded newlines, cancellation,
EOF, server close, invalid frames, missing sockets, size limits and continued
server availability after disconnect. Terminal connection tests cover retained
process reuse, exact session IDs, explicit mode selection, failed-load cleanup,
concurrent prompt rejection, detach-only cancellation and human-owned approvals.
Review regressions also cover cancellation during adapter creation, a stalled
WebSocket upgrade, lossless JSON numeric framing, and private failure reports.
Full runner suite: 88 tests passed; lint clean.
