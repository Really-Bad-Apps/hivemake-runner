# Live terminal compatibility findings

Tested with native Codex 0.163.0, codex-acp 2.1.1 and Python ACP SDK 0.12.1.
The adapter is unmodified. All native sessions and projects in these tests were
disposable; no production session received a diagnostic prompt.
The reusable probe reproduced the busy-turn behavior in a second disposable
session (about 3.1 seconds to ACP completion during a 15-second MCP call).
Full runner regression suite: 94 tests passed; lint clean.

Decision after these probes: native steering is accepted for return messages,
including during human-driven work. The former idle-only contract is withdrawn.
These findings inform rollout acceptance rather than block it on an idle guard.

## Results

| Check | Result |
|---|---|
| Project AGENTS.md instruction fingerprint still followed through ACP | Pass |
| Project-configured developer instruction fingerprint still followed | Pass |
| Project-configured MCP tool actually called through ACP | Pass |
| Original terminal uses those instructions and MCP tool after detach | Pass |
| Observe a later human turn becoming active through ACP metadata | Pass after an ACP prompt |
| Initial authoritative idle snapshot from ACP load | Not supplied in the tested load updates |
| Deliver a reply during native work | Verified: native steering (accepted behavior) |
| Attach the tested CLI-only instructions/MCP override session through the shared socket | **Fail: thread not loaded there** |

Instruction checks are behavioral evidence, not a byte-for-byte proof that every
possible configuration survives. The independent MCP audit records real tool
invocations and fresh return tokens, rather than trusting an agent's claim that
it called a tool. A passing project-configured case does not establish support
for CLI overrides, arbitrary profiles, additional roots or all MCP transports.

## Busy-turn finding and accepted behavior

The native terminal started a fixture MCP call with a 15-second delay. ACP
reported `session_info_update._meta.codex.threadStatus.type = active`. The probe
then deliberately submitted a harmless ACP prompt while that call was running.
The assistant answered the new prompt and ACP returned `end_turn` approximately
2.7 seconds after submission, before the fixture call finished. The original
terminal displayed the injected message during its existing work. Native turn
history showed no separate new turn for that injected prompt.

This is steering, not deferred delivery. Knowing the last observed status is
useful, but an idle observation followed by prompt submission leaves a race with
the human starting a turn. The initial load supplied a title update, not an idle
snapshot; silence after load must not be interpreted as idle either.
The pinned adapter's prompt path starts a native turn
without an atomic idle precondition. Its session list maps stored thread metadata
to session IDs/cwd/title/time; that list is not proof of liveness or idleness.

The native generated `TurnStartParams` explicitly discusses requests steering
already-active turns and exposes no expected-idle precondition. Native
`expectedTurnId` belongs to `turn/steer`, which requires an active turn; it is not
an idle-only start guard. See [OpenAI's app-server documentation](https://learn.chatgpt.com/docs/app-server).

The original idle-only contract would require an atomic admission capability;
a local lock or a preflight read cannot provide it. The accepted design instead
delivers the return message through ACP and lets the native server incorporate
it, even during active human work. No idle wait or native RPC rewriting is needed.
Runner-originated ACP prompts remain serialized, and uncertain submissions must
not be blindly replayed. Public automatic delivery still awaits registration,
ownership validation, durable delivery plumbing and deployment acceptance.

## Exact shared-server identity matters

In the CLI-only override test, the native terminal could call the fixture and
follow its instructions, but a diagnostic `thread/read` through the configured
shared socket returned `notLoaded`. ACP load then failed with
`thread_active_writer`. This establishes that the selected shared server did not
own that live session; it does not establish that the adapter erased its settings.
Do not start a replacement conversation or infer ownership from persisted history.
Native read RPCs were used only as test evidence, not added to runner routing.

## Reproduce with a disposable project

Create a new temporary directory. Put this in its `AGENTS.md`, using a fresh
fingerprint:

```text
For PROBE_CONTEXT requests, include AGENT_<fresh-token> in your final answer.
Call connector_context as requested and report the returned marker.
Do not read files to recover fingerprints.
```

Create `.codex/config.toml` in that directory (replace paths and token):

```toml
developer_instructions = "For PROBE_CONTEXT requests, also include DEV_<fresh-token> in your final answer."

[mcp_servers.connector_probe]
command = "/absolute/path/to/python3"
args = ["/absolute/path/to/hivemake-runner/scripts/fixtures/context_mcp.py", "/absolute/disposable/project/audit.jsonl"]
```

Launch an ordinary native terminal:

```bash
codex -C /absolute/disposable/project -s workspace-write -a on-request
```

Trust only that fixture directory when prompted. Ask the terminal:

```text
PROBE_CONTEXT baseline: call connector_context with stage="baseline" and delay=0.
Report instruction fingerprints and tool result. Do not read files or use other tools.
```

Wait for completion, obtain that exact terminal session UUID, and leave it open.
With the runner virtualenv active, run:

```bash
python scripts/probe_terminal_context.py \
  --thread <exact-disposable-session-uuid> \
  --cwd /absolute/disposable/project \
  --adapter /absolute/path/to/codex-acp \
  --socket /absolute/path/to/app-server-control.sock \
  --report /private/path/context-report.json \
  --busy-audit /absolute/disposable/project/audit.jsonl \
  --ack-disposable-session
```

The probe first requests the same instruction/tool check through ACP. It then
prints when to ask the native terminal for `connector_context` with stage `busy`
and delay `15`. The probe watches the fixture audit and deliberately submits its
second prompt during that call. **Omit `--busy-audit` for a context-only probe.**
Watching a test fixture is diagnostic synchronization, not agent ticket polling.

After detach, ask the terminal for `PROBE_CONTEXT post` with stage `post` and
delay `0`. Compare its response with the baseline and ACP response. Compare MCP
audit start/end times with report submission/completion times. Keep reports and
terminal transcripts private; reports include conversation updates. The report
writer uses mode 0600. A diagnostic process exiting successfully means evidence
was recorded, not that end-to-end ticket delivery is ready for deployment.
