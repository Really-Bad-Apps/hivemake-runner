# HiveMake Runner

A host daemon that runs Codex and Claude through the Python Agent Client Protocol
SDK. Deploy it on machines containing your projects and provider logins.
The REST gateway lives in **HiveMake server**; this repository contains no Flask
API container or Docker image build.

```text
Client → HiveMake API → Tailscale HTTP → runner daemon → Codex / Claude
                         ↑
             Owner-key registration + heartbeat
```

## Install

Linux, Python 3.12+, Node.js 22+, and an account already authenticated with the
agent providers are required. Agents run directly on that host as that account.

```bash
git clone https://github.com/Really-Bad-Apps/hivemake-runner.git
cd hivemake-runner
python3 -m venv .
source bin/activate
pip install -r requirements.txt
npm ci --prefix adapters --ignore-scripts
cp examples/runner.yaml runner.yaml
```

Edit `runner.yaml` with absolute discovery roots, state/socket paths, adapter
commands, the host's Tailscale IPv4 address, and your HiveMake origin. Give the
host a readable `display_name`; when omitted it defaults to its hostname.

Obtain an owner runner API key from your HiveMake installation. Store it in a
private service environment file (`chmod 600`), outside the repository:

```dotenv
HIVEMAKE_OWNER_API_KEY=REPLACE_WITH_OWNER_KEY
```

Use `deploy/hivemake-runner.service` as a systemd template. Adjust `User`, `Group`,
`HOME`, `PATH`, installation paths, and `EnvironmentFile` for your host. The
service must use the account with the intended provider subscription logins.
Systemd does not source interactive shell startup files.

For a manual launch with the key already exported:

```bash
python -m runner.daemon --config /absolute/path/to/runner.yaml
```

For a systemd installation:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now hivemake-runner
```

The state directory stores `identity.json` (a generated UUID) and `runs.sqlite3`.
Preserve it across upgrades and reinstalls to retain the runner identity and
results. Renaming the display name does not change the ID. Do not clone a state
directory onto another live host: independent hosts need independent IDs.
Corrupt identity files cause startup failure rather than silent re-registration.

## Registration and availability

The daemon registers immediately after its listener starts, then every 30 seconds
with its ID, display name, endpoint and current project inventory. Registration
uses `Authorization: Bearer <owner-key>` and is scoped to that owner. The key is
removed from the environment before child agents start. Registration failures
are retried without logging the key or response body. No redirects are followed.
The HiveMake origin must use HTTPS or HTTP over a literal Tailscale IPv4 address.

HiveMake marks a runner offline after 90 seconds without a successful heartbeat.
New requests are rejected when offline or unreachable; there is no queue or
automatic retry of work submissions. Existing local runs continue if HiveMake
is unreachable. Revoking the registration key blocks registration and dispatch;
replace the key and restart the service to rotate it.

Omit `hivemake_url` only for standalone development without registration. The
Unix socket remains available for local diagnostics. It is owner-only.

## Discover projects

Place an `agent.yaml` in a project under one of the configured discovery roots:

```yaml
version: 1
id: example-developer
name: Example Developer
harness: codex  # or claude
```

The marker's directory is the workspace. Discovery runs at daemon startup and
on explicit refresh. It scans nested projects, skips dependency directories and
virtual environments, and does not follow symlinks. Duplicate IDs on one host
are excluded with discovery errors. Different runner hosts can use the same
agent ID: API calls identify both the runner UUID and agent ID.

`ready` means the adapter executable exists, not that provider login was verified.
Project instructions and MCP configuration remain the harness's responsibility;
verify their loading before using real projects. A workspace is a working
directory, not an OS security boundary.

## Shared runner instruction profiles

Write invocation-specific instructions once in a shared UTF-8 Markdown file.
Register its name in the daemon's `runner.yaml`:

```yaml
instruction_profiles:
  ticket-read-only:
    file: /etc/hivemake-runner/instructions/ticket-read-only.md
```

Then reference it from each project's `agent.yaml` (the same syntax for either harness):

```yaml
version: 1
id: example-developer
harness: codex
runner:
  instruction_profile: ticket-read-only
```

An example profile is provided in `examples/instructions/ticket-read-only.md`.
Copy it to the configured location and customize it there. Profile paths must be
absolute; names use lowercase letters, digits, underscores and hyphens (up to 64
characters, beginning with a letter or digit). Missing profiles, unreadable files,
invalid UTF-8, empty content, NUL characters or files over 64 KiB prevent new
sessions for that project. Discovery reports the project as unavailable on refresh.
Files are checked again at session creation, even if discovery previously succeeded.
Projects without a profile retain their existing behavior.

The daemon reads and snapshots the file when a session is created, before an
adapter is launched. Editing the Markdown affects new sessions immediately;
existing sessions retain their original text, even if their first prompt has not
yet been sent. Close and recreate a session to adopt a revision. Restart the daemon
to change profile-name/path mappings; refresh discovery to change project references.
Session and persisted run responses include `instructions` with the profile name,
SHA-256 of the original file bytes, and delivery method, but not its path or text.
Private ACP transcripts may still include the instructions echoed by a harness.

Delivery is implemented inside the runner for the pinned adapters:

| Harness | Delivery | Existing project rules |
| --- | --- | --- |
| Claude | `session/new` metadata appends to the `claude_code` system-prompt preset | Normal project instruction loading remains enabled |
| Codex | A separate text block precedes the first request in the same ACP prompt | Normal `AGENTS.md` loading remains enabled |

Codex ACP 2.1.1 does not expose supplemental system/developer instructions at session
creation. Its fallback is **user-level conversation context**, not a system message;
it generates no extra initialization turn. Follow-ups reuse the initialized
conversation. Claude uses its adapter's native append mechanism, not a replacement
system prompt. Keep the pinned adapter versions, and verify these integration paths
when upgrading them.

The runner never writes `CLAUDE.md`, `AGENTS.md`, or project configuration to inject
instructions. Direct human launches are unaffected. These profiles are behavioral
instructions, not filesystem restrictions or a sandbox. Existing permission handling
is unchanged; the agent is expected to follow the shared profile just as it follows
project instructions. Ticket routing and human escalation are separate features.

## Use through HiveMake

Use your HiveMake origin and owner runner key (or a human login for dispatch).
The server returns a key's secret only once, at creation.

```bash
# List hosts; select a runner's generated id from the response.
curl "$HIVEMAKE_URL/api/runners" -H "Authorization: Bearer $HIVEMAKE_OWNER_API_KEY"

curl "$HIVEMAKE_URL/api/runners/$RUNNER_ID/agents" \
  -H "Authorization: Bearer $HIVEMAKE_OWNER_API_KEY"

curl -X POST "$HIVEMAKE_URL/api/runners/$RUNNER_ID/sessions" \
  -H "Authorization: Bearer $HIVEMAKE_OWNER_API_KEY" \
  -H 'Content-Type: application/json' -d '{"agent_id":"example-developer"}'

curl -X POST "$HIVEMAKE_URL/api/runners/$RUNNER_ID/sessions/$SESSION_ID/runs" \
  -H "Authorization: Bearer $HIVEMAKE_OWNER_API_KEY" \
  -H 'Content-Type: application/json' -d '{"prompt":"Explain this project."}'

curl "$HIVEMAKE_URL/api/runners/$RUNNER_ID/runs/$RUN_ID" \
  -H "Authorization: Bearer $HIVEMAKE_OWNER_API_KEY"
```

All execution endpoints are under `/api/runners/{runner_id}`:

| Method / suffix | Purpose |
| --- | --- |
| GET `/agents` | Current discovered projects, availability and errors |
| POST `/agents/refresh` | Rescan local roots; next heartbeat updates registry inventory |
| POST `/sessions` | Fresh conversation, body `{"agent_id":"..."}` |
| GET `/sessions/{id}` | Session state and active run |
| DELETE `/sessions/{id}` | Close idle session (409 if busy) |
| POST `/sessions/{id}/runs` | Submit `{"prompt":"..."}`; returns 202 |
| GET `/runs/{id}` | Status, response, events, errors and stop reason |
| POST `/runs/{id}/cancel` | Cancel; poll until finished |

One run per agent may be active, across all sessions. Overlap returns
`409 agent_busy`. Follow-ups use the same live session. Timeout, failure or
cancellation expires the session. Close unused sessions to release capacity.
A timeout submitting work can mean it was accepted: inspect the session's
`active_run` before deciding to retry.

Restarting HiveMake does not affect local work. Restarting the daemon expires
sessions, retains results, and marks unfinished runs `interrupted`. SQLite uses
WAL and FULL synchronous writes. Failed result writes remain in memory with
`persistence_pending: true`; new submissions receive 503 until storage recovers.
Pending in-memory results are not durable across crashes. No transcript retention
policy is implemented; keep state files private.

## Prototype trust boundary

**HiveMake → daemon has no application authentication for this testing phase.**
Bind the daemon only to its Tailscale address and restrict tailnet access. Anyone
who can reach its listener can execute agents as the service account. The owner
key authenticates registration with HiveMake; it does not protect this listener.
Tailscale encrypts inter-host traffic. Do not expose the listener publicly.

ACP permission requests are automatically approved (`allow_once` preferred).
Harness sandbox settings are not weakened. Future human approval through
HiveMake escalation is not implemented. Provider subscription logins remain on
the host; the runner does not initiate login or fall back to API-key billing.
Prompts, responses and events can contain private data. Logs omit prompt content,
adapter stderr and registration credentials. Keep credentials, state and local
configuration out of Git.

## Development

```bash
source bin/activate
pip install -r requirements.txt -r dev-requirements.txt
ruff check runner tests
pytest
```

Tests exercise real ACP subprocesses using a deterministic fake agent, including
permissions, cancellation, process cleanup, recovery, registration and identity.
They do not prove provider authentication or project MCP configuration. Verify
both real harnesses in disposable projects before directing production work.

The retired standalone Flask gateway remains in Git history at the prototype
release. Retire its deployment only after migrating clients to HiveMake and
verifying the new registration and dispatch path.
