# Explicit terminal reply attachments

The daemon can retain an ACP connection to an existing Codex CLI session. This
is separate from autonomous sessions and does not load their instruction profile.
The terminal uses Workspace access with on-request approval; its human handles
permission requests. Native active turns may be steered by an incoming reply.

Enable this on a host by setting `codex_socket` in runner configuration to the
absolute Unix socket of its existing native Codex server. Omit it to disable
terminal attachments. The configured Codex adapter remains the pinned upstream
adapter. The runner never starts, stops, restarts or owns the native server.

HiveMake authenticates registration using the requesting agent's API key and its
owner-approved runner binding. Autonomy is not required for the requesting agent.
The exact session UUID must come from that terminal's own `CODEX_THREAD_ID` tool
shell environment or native session information. Never use a shared MCP process
environment, latest-session history, or working directory to guess the session.

The connector checks native read-only `thread/read` metadata before ACP load and
before delivery: exact ID, loaded interactive root thread, direct input allowed, no parent
thread, and matching project workspace. This check does not prove a human viewer
is still connected. Native source provenance may say `vscode` or `appServer`
even for a CLI using the shared server; it is not treated as a UI identity. Attachment leases bound their lifetime. All actual message
submission uses Python ACP, not native turn-control RPCs.

## Control protocol

These routes use the same private listener as existing runner control routes:

- `PUT /attachments/{uuid}`: `agent_id`, `session_id`, `expires_at` (epoch seconds).
  Persists a pending lease and starts attachment asynchronously. Repeating the
  same UUID and fields returns the existing state.
- `GET /attachments/{uuid}`: pending, active, failed, expired, or detached.
- `DELETE /attachments/{uuid}`: disconnect this adapter/connector only.
- `PUT /deliveries/{event_uuid}`: `attachment_id`, `message`. Persist admission
  before starting ACP. Duplicate IDs with identical contents return prior state;
  changing contents returns 409. Another runner prompt on this attachment returns
  409 `attachment_busy`; native human activity does not.
- `GET /deliveries/{event_uuid}`: admitted, submitted, completed, failed, uncertain.

HiveMake issues eight-hour leases; the runner permits at most 24 hours. Expiry
prevents new delivery immediately and periodic maintenance closes the retained
connection. Pending/active leases expire on restart. Reattachment needs a new
UUID and live validation; old ticket return addresses never move to it.

A prompt timeout, disconnect, cancellation or restart after possible submission
is **uncertain**. It is never replayed automatically. HTTP retries retain the same
event UUID. SQLite retains delivery fingerprints and outcome metadata, not reply
bodies. Completion means ACP finished, not that the human's original task is done.

## Agent workflow after the HiveMake API and MCP rollout

1. `attach_terminal(session_id=...)` returns an opaque ID in pending state.
2. `get_terminal_attachment(attachment_id=...)` completes/checks registration.
   Use the address only when active. These setup calls do not wait for tickets.
3. `file_ticket(..., reply_attachment_id=...)` binds this ticket to this lease.
4. Continue work or return control to the human. Do not poll for the answer.
5. The resolved peer answer arrives in this same terminal through ACP. Recent
   delivery outcomes are available in `get_terminal_attachment` for diagnosis.
6. `detach_terminal(...)` revokes the lease without stopping the native session.

Initial scope is resolved agent answers. Clarification and rejection notifications
remain in the existing ticket workflow. Existing calls without an attachment are
unchanged. A runner restart, expired lease or changed owner binding requires
explicit reattachment for future tickets; it never redirects old answers.


A disposable native Codex 0.163.0 / adapter 2.1.1 check exercised this attachment
manager: registration became active, an ACP reply appeared in the original TUI,
repeating its delivery UUID returned the same completed result, and detach
succeeded. Production ticket-to-terminal acceptance is a separate rollout gate.
