# HiveMake Runner

Run project agents through a small REST API using the Python
[Agent Client Protocol SDK](https://github.com/agentclientprotocol/python-sdk).
Each project selects Claude or Codex in an `agent.yaml` marker. A host daemon
owns fresh conversations; a Flask container exposes their sessions and results.

This is a trusted-network prototype. It has **no REST authentication** and
automatically approves ACP permission requests. Access to its API grants the
ability to run agents with the host service account's permissions. Keep the API
on a trusted Docker network, loopback, or an explicitly selected tailnet address.

## Architecture

```text
REST caller → Flask / Gunicorn container → Unix socket → Python host daemon
                                                          ├─ Claude ACP adapter
                                                          ├─ Codex ACP adapter
                                                          └─ SQLite run results
```

- Agents and their commands execute directly on the host, using its installed
  development tools, workspace files, provider logins, and harness configuration.
- The API container mounts only the socket directory. It needs no project files,
  provider credentials, host home directory, or Docker socket.
- Sessions belong to the daemon and are created fresh. Existing terminal
  conversations are not attached or resumed.
- Only one run per agent can be active, including across different sessions.
  A second submission receives `409 agent_busy`; nothing is queued.
- Restarting Flask leaves host runs alone. Restarting the daemon expires sessions,
  retains run results, and marks unfinished runs `interrupted`.
- Cancellation invalidates the session and stops its adapter process group. The
  systemd unit also kills the daemon's cgroup at shutdown. Processes deliberately
  detached into another process group are not individually tracked by this prototype.

## Requirements

Linux, Python 3.12+, Node.js 22+, and Docker Compose for the API deployment.
Use an existing host account authenticated with both desired harnesses. Install
development tools needed by your projects on that host and ensure they appear in
the daemon's `PATH` (systemd does not source your interactive shell startup files).

The API and host use only public dependencies. Neither requires another HiveMake
repository, a HiveMake token, an API key, or a private package registry. Provider
authentication is still required by the agent runtime; existing subscription
logins are expected to be available to the service account. Login renewal remains
an operator task. The runner does not launch an interactive login flow or fall
back to paid API keys on its own.

## Install the host runner

```bash
git clone https://github.com/reallybadapps/hivemake-runner.git
cd hivemake-runner
python3 -m venv .
source bin/activate
pip install -r host-requirements.txt
npm ci --prefix adapters --ignore-scripts
cp examples/runner.yaml runner.yaml
```

Edit `runner.yaml`: configure absolute discovery roots, the socket path, the
private state directory, and absolute paths to the installed adapter executables.
The examples use generic paths; they are not an existing deployment.

For a manual local launch, choose writable socket/state directories, then run:

```bash
python -m runner.daemon --config /absolute/path/to/runner.yaml
```

For systemd, copy `deploy/hivemake-runner.service` to your system unit directory
and edit its `User`, `Group`, `HOME`, `PATH`, installation paths, and configuration
path. Use your authenticated host account, not root or an empty new account.
The example's `RuntimeDirectory` and `StateDirectory` create owner-only
`/run/hivemake-runner` and `/var/lib/hivemake-runner` directories.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now hivemake-runner
```

The runner sets a restrictive umask and creates an owner-only Unix socket. The
API must run with the same numeric UID to connect. Mount the **directory**, not
the socket file, so replacing the socket on restart remains visible in Docker.

## Register projects

Place this file at the root of a project within a configured discovery root:
Use `examples/agent.example.yaml` as a template; its example filename is deliberately
not discoverable until copied to a project's `agent.yaml`.

```yaml
# agent.yaml
version: 1
id: example-developer
name: Example Developer
harness: codex  # or claude
```

The directory containing the marker is the workspace. `id` is a stable lowercase
identifier (letters, numbers, `_`, `-`, maximum 64 characters). `name` is optional.
Existing project instructions and MCP settings remain the harness's responsibility;
the runner does not rewrite or copy them. Verify their loading in your deployment
smoke test because adapter behavior can differ from interactive CLI behavior.

Discovery runs at startup and on `POST /agents/refresh`. It scans nested projects,
skips common dependency folders and virtual environments, and does not follow
directory or marker symlinks. Overlapping roots are deduplicated. Duplicate IDs
are excluded entirely, and invalid markers/unreadable roots appear in `errors`.
After changing a definition, refresh and create a fresh session. An active run
finishes with the definition it started with.

`ready` means the configured adapter executable is available; it is not proof
of valid provider authentication. Adapters start lazily on the first prompt.

## Run the API container

```bash
cp example.env .env
# Edit RUNNER_UID, RUNNER_GID, RUNNER_SOCKET_DIR for the host daemon.
docker compose --env-file .env -f docker-compose.example.yaml up -d --build
```

The default published address is `127.0.0.1:7183`. To expose on the tailnet, set
`RUNNER_BIND_IP` to the host's tailnet address. For Docker-only access, remove the
`ports` section and attach the container to your intended private Docker network.
Do not bind this unauthenticated prototype to all host interfaces.

If your shell lacks the Docker supplementary group, use
`sg docker -c 'docker compose --env-file .env -f docker-compose.example.yaml up -d --build'`.

No Swagger UI or permissive browser CORS is enabled. Flask-smorest builds an
OpenAPI definition internally. `GET /health` checks connectivity to the daemon
and returns 503 if it is unavailable.

## REST workflow

```bash
curl http://127.0.0.1:7183/agents
curl -X POST http://127.0.0.1:7183/sessions \
  -H 'Content-Type: application/json' -d '{"agent_id":"example-developer"}'
# Substitute the returned session ID:
curl -X POST http://127.0.0.1:7183/sessions/SESSION_ID/runs \
  -H 'Content-Type: application/json' -d '{"prompt":"Explain this project briefly."}'
# Substitute the returned run ID:
curl http://127.0.0.1:7183/runs/RUN_ID
```

| Endpoint | Behavior |
| --- | --- |
| `GET /health` | API and daemon readiness |
| `GET /agents` | Registry, adapter availability, busy flags, discovery errors |
| `POST /agents/refresh` | Rescan configured roots |
| `POST /sessions` | `{ "agent_id": "..." }`; returns 201 with session ID |
| `GET /sessions/{id}` | Session status and current run |
| `DELETE /sessions/{id}` | Close idle session; 409 if active |
| `POST /sessions/{id}/runs` | `{ "prompt": "..." }`; 202 or 409 `agent_busy` |
| `GET /runs/{id}` | Persisted status, text response, events, errors, stop reason |
| `POST /runs/{id}/cancel` | Request cancellation; poll until terminal |

Run states: `running`, `cancelling`, `completed`, `cancelled`, `failed`, and
`interrupted`. A completed protocol turn is not a claim that the requested task
succeeded; inspect the response and `stop_reason` (which can include refusal or
token limits). Follow-up prompts use the same live session. Runs that fail,
time out, or are cancelled expire that session. Close idle/expired sessions to
release the session limit (32 by default).

The default run timeout is 30 minutes, startup timeout 60 seconds, and cancellation
grace 5 seconds. Prompts are limited to 65,536 characters. Results retain up to
1,048,576 response characters and 1,000 events, with a 16,384-character serialized
limit per event; `truncated` reports omitted data. Events contain structured ACP
updates, while the response text is ordinary model output.

There is no automatic retry or idempotency contract. A 504 from a submission can
mean the runner accepted it before the response was lost. Check the session's
`active_run` before retrying. Multiple agent IDs or nested workspaces are not a
filesystem lock: operators must avoid directing separate agents at conflicting
edits in the same files.

## Permissions and private data

The prototype selects `allow_once` when offered, otherwise `allow_always`. It
does not silently weaken harness sandbox settings. If no approval choice exists,
the permission response is cancelled. No client-side terminal or filesystem
capability is advertised: adapters use their own harness tools.

The marker's workspace is a working directory, **not an OS security boundary**.
Effective access is controlled by the host account and harness configuration.
Future permission requests requiring human intervention should use HiveMake's
escalation feature; it is deliberately not integrated in this prototype.

Prompts, replies, and tool events are stored in `runs.sqlite3` under `state_dir`
and may contain private code or secrets. No automatic retention policy exists
yet. Keep the state directory private and out of Git/backups intended for public
distribution. Runtime logs contain IDs/statuses, not prompts or adapter stderr.
Provider errors return an error category/type rather than raw potentially secret
messages. Consult the provider's local diagnostics for detailed authentication
failures. The host inherits its service environment, so avoid injecting unused
provider API keys when subscription login is intended.

## Development and verification

```bash
source bin/activate
pip install -r requirements.txt -r host-requirements.txt -r dev-requirements.txt
ruff check runner blueprints hivemake_runner.py tests
pytest
docker build -t hivemake-runner:local .
```

The tests drive an actual ACP subprocess over stdio without credentials or model
usage, including permission approval, cancellation of stuck processes and their
children, session continuity, restart recovery, discovery errors, and the complete
Flask-to-Unix-socket path. They do not prove provider login, subscription access,
instruction loading, or tool execution through a real Claude/Codex session.

Before deploying for real work, create temporary marked projects for **both**
harnesses and verify: a text response; a follow-up that recalls the first turn;
a permitted file edit; MCP/instruction loading; cancellation; API restart during
a run; daemon restart and interrupted status. Use disposable workspaces.

Regenerate Python pins deliberately with `uv pip compile requirements.in -o
requirements.txt` and the equivalent host command. Adapter versions are pinned
in `adapters/package.json` and `package-lock.json`; install with `npm ci`. Adapter
updates can change capabilities and should repeat both harness smoke tests.

## Build and publish

```bash
export RUNNER_IMAGE_REPOSITORY=ghcr.io/reallybadapps/hivemake-runner
./build-publish.sh
```

The script increments a local ignored `VERSION` only after successful pushes,
publishes numbered and `latest` tags, and prints repository digests. Registry
authentication stays in the operator's Docker configuration. It is never passed
into the build. Deploy pinned image digests when handing an image to a host.

The Docker context is an allowlist containing only the API code and its public
requirements. Local configuration, host state, adapters, virtual environments,
credentials, and project workspaces are excluded from the image.

## Deferred

Queueing, Telegram and HiveMake callers, HiveMake permission escalation, REST
authentication, conversation recovery, automatic login, result retention policy,
and cross-agent workspace scheduling are outside this prototype.

MIT licensed. See [LICENSE](LICENSE).
