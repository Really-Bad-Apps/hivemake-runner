# Install a HiveMake runner for an OS user

Reusable Linux installation guide for humans and host-owning agents. Validated
on Vulcan as `foo` on 2026-10-29. Run commands in Bash as the account that will
execute agents. Only the lingering command below needs sudo.
Run each command in order and stop on any error. This is a runbook, not an
unattended script; resolve placeholders and inspect existing installations first.

Validated runner code: `551d4fd95e7525d951c4eee3a5a055aecfc6a7ec`.
Validated environment: Python 3.13, Node 24, Claude ACP 0.87.0, Codex ACP 2.1.1.
Real production acceptance covered Claude executing `whoami` and `hostname`,
with the caller's blocking MCP request returning the dispatcher-applied result.
It did **not** prove the worker's outbound MCP connection or long-running jobs.

## What you are installing

One runner instance executes every agent it launches as **its own OS user**.
A runner owned by `jason` cannot become `foo` just because a project's files
belong to foo. Install a separate instance for each execution account. One
instance can discover multiple projects belonging to that account.

Each instance needs its own configuration, state directory, Unix socket and
TCP listener port. On the same machine, the users share a Tailscale IP but must
use different ports. Example: Vulcan/jason uses 7184; Vulcan/foo uses 7185.
Each has a separate generated runner UUID and a distinct display name.

Three different credentials are involved:

| Credential | Purpose | Location |
| --- | --- | --- |
| HiveMake **owner runner key** | Registers the runner with your HiveMake account | Private `runner.env` |
| Agent's HiveMake MCP bearer key | Identifies the agent when it calls HiveMake tools | Harness MCP configuration |
| Claude/Codex provider login | Allows the adapter to use the intended provider subscription | This OS user's provider configuration and/or service environment |

The MCP bearer key is **not** a runner key. The runner strips the owner runner
key from child-agent environments; provider credentials must reach the adapter.

### Current operational limits

- The Tailscale listener has **no application authentication** in this version.
  Tailnet ACLs/firewall rules must restrict access to approved dispatch sources.
  Anyone able to reach the listener can execute agents as this OS user. Never
  bind it publicly or use `0.0.0.0`. Registration credentials do not secure it.
- ACP permission requests are automatically approved (`allow_once` preferred).
  Project instructions and working directories are not OS isolation. Use the
  intended account and permissions; do not run the daemon as root.
- The runner requires a positive, finite `run_timeout`; this guide uses its
  existing 1800-second default. It cannot currently express unlimited execution.
  Blocking MCP's lack of an application wait deadline does not remove this limit
  or intermediary/client limits. Address this before long deployment jobs.
- Restarting the runner interrupts active runs and expires in-memory sessions.
  Preserve its state directory; do not clone another instance's `identity.json`.

## 1. Gather the installation values

Before changing anything, confirm the target host/account, project directory,
provider, available port, and authority to install. Follow that host's ownership
rules. Do not change another project's code or silently take over its admin work.
If guiding a human interactively, give one command at a time and wait for output.

Read-only checks:

```bash
whoami
hostname
command -v git curl node npm claude codex
node --version
tailscale ip -4
```

Requirements: Linux with systemd user services, Git access to the runner repo,
Node **22 or newer**, npm, Tailscale, and provider authentication for this account.
Missing `codex` is not a blocker for a Claude-only setup. Installing adapters
does not authenticate either provider. Have the host owner install missing OS
prerequisites through its normal process.

Choose values below. Replace the example workspace, agent identity, display
name and Tailscale IP. Keep this Bash session open: subsequent commands use
these variables. Paths must be absolute. The agent ID must match
`[a-z0-9][a-z0-9_-]{0,63}`; it is a local discovery ID, not the HiveMake UUID.

```bash
export HM_RUNNER_ROOT="$HOME/hivemake-runner"
export HM_RUNNER_CONFIG="$HOME/.config/hivemake-runner"
export HM_RUNNER_STATE="$HOME/.local/state/hivemake-runner"
export HM_RUNNER_WORKSPACE='/absolute/path/to/agent-project'
export HM_RUNNER_AGENT_ID='server-admin'
export HM_RUNNER_AGENT_NAME='Server Admin'
export HM_RUNNER_HARNESS='claude'
export HM_RUNNER_DISPLAY='REPLACE_WITH_HOST — USER'
export HM_RUNNER_IP='REPLACE_WITH_TAILSCALE_IPV4'
export HM_RUNNER_PORT='7185'
export HM_RUNNER_ORIGIN='https://api.hivemake.ai'
export HM_RUNNER_REV='551d4fd95e7525d951c4eee3a5a055aecfc6a7ec'
```

Inspect the selected port before continuing:

```bash
ss -ltn "sport = :$HM_RUNNER_PORT"
```

No listener row should appear. If occupied, choose another port and arrange
access from the HiveMake dispatch host through the host/network owner. Do not
stop an existing runner to free its port. This guide assumes a **fresh install**;
if the checkout, configuration or service already exists, inspect it first and
use the upgrade section instead of overwriting it.

## 2. Install the pinned code and dependencies

If `uv` is absent, install it for this account without sudo. This uses Astral's
[official standalone installer](https://docs.astral.sh/uv/getting-started/installation/):

```bash
curl -LsSf https://astral.sh/uv/0.13.6/install.sh | sh
```

The commands below assume uv is at `$HOME/.local/bin/uv`, the default used in
the validated installation. Adjust that path if your existing installation is
elsewhere. Never install runner packages into system Python.

```bash
git clone https://github.com/Really-Bad-Apps/hivemake-runner.git "$HM_RUNNER_ROOT"
git -C "$HM_RUNNER_ROOT" checkout --detach "$HM_RUNNER_REV"
"$HOME/.local/bin/uv" venv --python 3.13 "$HM_RUNNER_ROOT/.venv"
"$HOME/.local/bin/uv" pip install --python "$HM_RUNNER_ROOT/.venv/bin/python" -r "$HM_RUNNER_ROOT/requirements.txt"
npm ci --prefix "$HM_RUNNER_ROOT/adapters" --ignore-scripts
```

Use the committed lockfile. This revision retains both adapter versions and
patches vulnerable dependencies, including a Zod 4.6.6 override. Its npm audit
reported zero vulnerabilities on 2026-10-29; later advisory results may differ.
If npm reports vulnerabilities, inspect `npm audit --prefix
"$HM_RUNNER_ROOT/adapters"` and send findings to the runner owner. Do not run
`npm audit fix --force`: it can replace the adapter with an unreviewed version.
Do not create a lockfile in your home directory to work around an `ENOLOCK` error.

## 3. Define the project and runner

The directory containing `agent.yaml` is the agent's workspace. Use the
existing project containing its `CLAUDE.md`/`AGENTS.md` and harness configuration.
Do not create a new empty directory merely to give the runner a discovery root.
The directory must already exist and must not be a symlink.

```bash
install -d -m 700 "$HM_RUNNER_CONFIG" "$HM_RUNNER_STATE" "$HOME/.config/systemd/user"
```

Create the marker and configuration. This command validates the selected
identity and refuses to overwrite either existing file. If either exists,
inspect and merge it deliberately instead.

```bash
"$HM_RUNNER_ROOT/.venv/bin/python" <<'PY'
import os
import re
from pathlib import Path
import yaml

env = os.environ
workspace = Path(env['HM_RUNNER_WORKSPACE'])
root = Path(env['HM_RUNNER_ROOT'])
config_dir = Path(env['HM_RUNNER_CONFIG'])
state = Path(env['HM_RUNNER_STATE'])
agent_id = env['HM_RUNNER_AGENT_ID']
harness = env['HM_RUNNER_HARNESS']
assert workspace.is_absolute() and workspace.is_dir() and not workspace.is_symlink()
assert re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', agent_id)
assert harness in ('claude', 'codex')
marker = workspace / 'agent.yaml'
config_path = config_dir / 'runner.yaml'
assert not marker.exists() and not config_path.exists(), 'Inspect existing files first'
agent = dict(version=1, id=agent_id, name=env['HM_RUNNER_AGENT_NAME'], harness=harness)
adapter = 'claude-agent-acp' if harness == 'claude' else 'codex-acp'
config = {
    'roots': [str(workspace)],
    'socket': str(state / 'runner.sock'),
    'state_dir': str(state),
    'commands': {harness: [str(root / 'adapters/node_modules/.bin' / adapter)]},
    'listen_host': env['HM_RUNNER_IP'],
    'listen_port': int(env['HM_RUNNER_PORT']),
    'hivemake_url': env['HM_RUNNER_ORIGIN'],
    'display_name': env['HM_RUNNER_DISPLAY'],
    'max_sessions': 1,
    'discovery_interval': 60,
    'startup_timeout': 60,
    'run_timeout': 1800,
    'cancel_grace': 5,
}
with marker.open('x') as output:
    yaml.safe_dump(agent, output, sort_keys=False)
with config_path.open('x') as output:
    yaml.safe_dump(config, output, sort_keys=False)
print('Created', marker, 'and', config_path)
PY
```

This starts nothing. Discovery is recursive, skips dependency/venv directories
and symlinks, and refreshes about every 60 seconds. Use narrow roots where
possible: choosing a home directory also discovers nested project markers.
Duplicate local agent IDs within one runner make those entries unavailable.
Keep old marker copies outside discovery roots. `max_sessions: 1` is a
conservative starting capacity; increase deliberately for multi-project hosts.
To support both providers, add both adapter paths to `commands` and authenticate
both providers under the service account.

### Optional: restrict unattended sessions with a shared profile

The repository includes both `ticket-read-only` and a customizable `ticket-admin`
template in `examples/instructions/`. Choose the intended scope with the host
owner and follow [the profile installation guide](INSTRUCTION_PROFILES.md).
For a fresh installation, add its mapping and marker reference before first
startup. On an existing runner, load the mapping before publishing a new marker
reference. Profiles are behavioral instructions, not an enforced sandbox.

## 4. Give the service its credentials

In HiveMake **account settings → Runner keys**, create an owner runner key named
for this host and account. Save its once-displayed secret privately. Do not use
the agent bearer from `.mcp.json`, paste secrets into chat, or commit them.

Create a private environment file using a hidden prompt. The command refuses to
overwrite an existing file. Do not run it with shell tracing (`set -x`) enabled.

```bash
(
  set -eu
  umask 077
  set -C
  read -rsp 'HiveMake owner runner key: ' hm_owner_key
  printf '\n'
  test -n "$hm_owner_key"
  printf 'HIVEMAKE_OWNER_API_KEY=%s\n' "$hm_owner_key" > "$HM_RUNNER_CONFIG/runner.env"
)
```

### Claude

Check this account's provider authentication without launching a model prompt:

```bash
claude auth status
```

**Systemd does not source `.bashrc` or inherit your interactive shell's token.**
On Vulcan, this reported `loggedIn: true` with `authMethod: oauth_token`, but the
runner still failed with `RequestError` because the token existed only in the
shell. Check presence without printing it:

```bash
if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
  echo 'Claude OAuth token is present in this shell'
else
  echo 'No Claude OAuth token in this shell; check the provider login method'
fi
```

If this is your intended authentication method, append the token **once**:

```bash
(
  set -eu
  test -n "${CLAUDE_CODE_OAUTH_TOKEN:-}"
  printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\n' "$CLAUDE_CODE_OAUTH_TOKEN" >> "$HM_RUNNER_CONFIG/runner.env"
  chmod 600 "$HM_RUNNER_CONFIG/runner.env"
)
```

These are raw keys/tokens, without a `Bearer ` prefix. `runner.env` is a systemd
environment file, not a shell script: no `export`, shell substitutions, or
references to variables in `.bashrc`. Do not copy all shell variables into it.
For a stored provider login, authenticate as this OS user and keep its HOME
correct in the service. Do not copy another user's credential files. Token
rotation requires updating the private environment file and restarting during
an idle window. Do not silently fall back to API-key billing.

### Codex

Use `harness: codex`, the `codex-acp` adapter path and the intended account's
existing provider login. Ensure its login/configuration is available under the
service HOME and explicitly provide any required environment via `runner.env`.
The Claude token step does not authenticate Codex. The real production
acceptance behind this guide validated Claude only; separately authorize and
verify Codex before calling that installation accepted.

## 5. Check the project's MCP configuration

The runner does not copy another user's MCP setup into this workspace. Preserve
the existing agent-specific bearer key and other tools. For a Claude project,
the relevant `.mcp.json` entry should use one of these transports:

| Endpoint | Intended behavior |
| --- | --- |
| `https://mcp.hivemake.ai/mcp` | Existing immediate-return/polling compatibility |
| `https://mcp.hivemake.ai/blocking/mcp` | Wait for autonomous recipients to respond |

For the blocking rollout, update **only** this agent's HiveMake entry to the
second URL, keeping `type: "http"` and its Authorization header. Make a backup
with mode 600 first; the backup contains the same secret. Do not use SSE or an
`/sse` URL. Do not print the whole configuration to chat. For Codex, configure
the equivalent HTTP MCP server through that harness's supported configuration;
do not assume Claude's `.mcp.json` is consumed by Codex.

Project loading, trust and MCP authorization remain harness responsibilities.
An inventory entry saying `ready` proves only that the adapter executable is
available. It does not prove provider authentication or MCP connectivity.
If the old agent used a five-minute cron poller, coordinate its retirement with
the host owner before autonomous binding; back up and disable only that job,
under its actual OS user. Do not remove unrelated cron entries.

## 6. Install a persistent systemd user service

Enable lingering so the account's user services can start at boot and survive
logout. This changes the named account's service lifecycle, not its privileges.

```bash
sudo loginctl enable-linger "$(id -un)"
loginctl show-user "$(id -un)" -p Linger
```

Expect `Linger=yes`. Run the remaining commands as the target account, **without
sudo**. Use `systemctl --user`; do not accidentally control a same-named system
service or another user's runner. A normal login/SSH session supplies the user
service bus; if it is unavailable, have the host owner establish that session
instead of guessing another user's runtime directory.

Create the service without overwriting an existing unit. The unquoted heredoc
expands these known installation paths now, producing absolute paths in the unit.
If Node or your provider executable lives elsewhere (for example under nvm),
include its actual directory in PATH; systemd does not initialize shell managers.

```bash
(
  set -eu
  set -C
  cat > "$HOME/.config/systemd/user/hivemake-runner.service" <<EOF
[Unit]
Description=HiveMake Runner for $(id -un)

[Service]
Type=simple
WorkingDirectory=$HM_RUNNER_ROOT
Environment="HOME=$HOME"
Environment="PATH=$HM_RUNNER_ROOT/adapters/node_modules/.bin:$HOME/.npm-global/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
EnvironmentFile=$HM_RUNNER_CONFIG/runner.env
ExecStart=$HM_RUNNER_ROOT/.venv/bin/python -m runner.daemon --config $HM_RUNNER_CONFIG/runner.yaml
UMask=0077
KillMode=control-group
TimeoutStopSec=30
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF
)
systemctl --user daemon-reload
```

For unusual installation paths containing spaces or systemd `%` specifiers,
have the host owner quote/escape the unit correctly before loading it. The
standard home-directory paths above avoid those cases.

Before starting: arrange the listener's tailnet access restrictions, confirm
credential files are private, and coordinate any existing binding/autonomy that
could dispatch work immediately when the runner registers. Then:

```bash
systemctl --user enable --now hivemake-runner.service
systemctl --user status hivemake-runner.service --no-pager --full
```

Expect `active (running)` and a `runner_ready` message with a runner UUID,
display name and discovered-agent count. Record the UUID; do not invent or
reuse one. If count is unexpected, inspect discovery before binding agents.

## 7. Register, bind and authorize acceptance

In HiveMake's runner list, confirm this account's display name is **online** and
the expected project is present. Startup logs alone do not prove registration
worked. Heartbeats normally run every 30 seconds; a runner without successful
heartbeats becomes offline after about 90 seconds.

Open the existing agent's page → **Runner binding**. Select the new runner and
the matching local project, then save. Automatic ticket handling requires both
a binding and **Autonomy enabled**. Saving a binding with autonomy already on
can immediately process eligible queued tickets. Prefer keeping autonomy off
until ready; if the operator deliberately controls incoming work and leaves it
on, respect that choice. Do not globally toggle other agents.

**Installation is not permission to run production tests.** Obtain Jason's
express consent naming the target host and exact test, including tests launched
locally against a production API. Do not silently run setup test prompts,
synthetic tickets, benchmarks or containers on production hosts.

A minimal first acceptance request is:

> May I send one ticket to this server agent asking it to run only `whoami` and
> `hostname`, return the output and resolve it, with no configuration changes?
> I will submit through the blocking MCP endpoint and verify it waits for the
> dispatcher-applied result.

After consent, submit exactly that test. Record the ticket ID, runner execution
ID, actual OS user/hostname, returned wait status and elapsed time. Progress
messages may expose the ticket ID before the result when the client supplies a
progress token. If interrupted, resume with `wait_for_ticket(ticket_id)`; use
`recover_outbound_tickets` with the original title if the ID was lost. Inspect
matches instead of creating duplicates.

A completed identity check does not prove the worker can call its own MCP
tools. Authorize that separately, as well as clarification/resume and long-run
tests. Keep the runner timeout and external proxy/client limits explicit.

## 8. Troubleshooting without exposing secrets

For HiveMake API/MCP failures, query Loki first through `loki-mcp`:
`{application="hivemake-server"}` and `{application="hivemake-mcp"}`; narrow
by ticket UUID and time. The **runner itself has no Loki integration** at this
revision. It logs to stderr, normally captured by systemd, and intentionally
discards adapter stderr. Error messages are reduced to error codes/types to
avoid leaking provider credentials. This limits current diagnosis.

Start with:

```bash
systemctl --user status hivemake-runner.service --no-pager --full
journalctl --user -u hivemake-runner.service -n 80 --no-pager
```

If the user journal is empty, an authorized host administrator can try:

```bash
sudo journalctl _UID="$(id -u)" _SYSTEMD_USER_UNIT=hivemake-runner.service -n 80 --no-pager
```

Both journal queries returned no entries during the Vulcan installation even
though `systemctl status` showed startup lines. Empty journal output is not
proof of successful execution. Inspect stored failure metadata read-only:

```bash
"$HM_RUNNER_ROOT/.venv/bin/python" <<'PY'
import json
import os
import sqlite3
from pathlib import Path

path = Path(os.environ['HM_RUNNER_STATE']) / 'runs.sqlite3'
with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
    rows = db.execute('SELECT body FROM runs ORDER BY rowid DESC LIMIT 5').fetchall()
    for (body,) in rows:
        run = json.loads(body)
        fields = ('id', 'status', 'error', 'error_type', 'stop_reason')
        print(json.dumps({key: run.get(key) for key in fields}, indent=2))
    if not rows:
        print('No saved runs.')
PY
```

Do not paste whole transcripts, `runner.env`, provider credential files or
`.mcp.json` into tickets/chat. `runs.sqlite3` contains private prompts/results.

| Symptom | Check next |
| --- | --- |
| `adapter_error`, `RequestError` | Provider credentials in the actual service environment. Shell login success is insufficient. On Vulcan, adding `CLAUDE_CODE_OAUTH_TOKEN` and restarting fixed it; this is not the only possible cause. |
| `run_not_completed_or_truncated` in ticket notes | Read the runner's saved status/error fields. This is the dispatcher's summary, not the adapter's detailed diagnosis. |
| Caller waits after a failed execution | The ticket may remain open. Stop that client wait if needed, inspect the failed execution, and obtain consent before another production test. Do not keep filing duplicates. |
| `runner_ready` but absent/offline in HiveMake | Correct owner runner key, API origin, outbound connectivity and registration diagnostics. Do not substitute the agent MCP key. |
| Online but dispatch cannot reach runner | Correct Tailscale IP/port and narrowly scoped inbound access from the dispatch host. Registration alone does not prove inbound reachability. |
| Agent missing/unavailable | Marker schema, absolute root, symlinks, duplicate IDs, adapter executable, instruction-profile errors; allow discovery refresh time. |
| Wrong OS user | Service ownership, HOME and selected runner binding. Changing workspace does not switch users. |
| Dies after logout / absent after boot | Linger enabled and user unit enabled. Confirm the selected Tailscale address is available. |
| `Address already in use` or lock failure | Another instance owns the port/state/socket. Allocate separate paths/port; do not delete its lock/state. |

## 9. Upgrade and stop safely

Ask the runner owner for a reviewed revision. Coordinate an idle window: stop
new dispatch by disabling autonomy for affected bindings, let active work finish,
and confirm no active runs before restarting. Back up configuration, credentials
and the **whole state directory while stopped**, with restrictive permissions.
Do not copy only the SQLite main file while WAL writes are active. These backups
contain secrets and transcripts; never publish them.

After confirming an idle window:

```bash
systemctl --user stop hivemake-runner.service
git -C "$HM_RUNNER_ROOT" status --short
```

Do not overwrite a dirty checkout. Set `HM_RUNNER_REV` to the approved commit;
fetch and select it, reinstall the pinned dependencies, then start and inspect:

```bash
git -C "$HM_RUNNER_ROOT" fetch origin
git -C "$HM_RUNNER_ROOT" checkout --detach "$HM_RUNNER_REV"
"$HOME/.local/bin/uv" pip install --python "$HM_RUNNER_ROOT/.venv/bin/python" -r "$HM_RUNNER_ROOT/requirements.txt"
npm ci --prefix "$HM_RUNNER_ROOT/adapters" --ignore-scripts
systemctl --user start hivemake-runner.service
systemctl --user status hivemake-runner.service --no-pager --full
```

Run `systemctl --user daemon-reload` first if the unit changed. Restore intended
autonomy only when ready for real work. Rollback also requires a coordinated
idle window and a compatible prior revision/state format; do not blindly
overwrite newer durable state with an old backup.

To retire an instance, first disable dispatch/remove its bindings and finish
active work, then `systemctl --user disable --now hivemake-runner.service`.
Retain private state/backups according to the owner's policy. Do not disable
lingering globally if other user services rely on it.

## Installation handoff record

Return this information to the operator, **without secrets**:

- Host, OS user, workspace(s), local agent ID(s), harness.
- Runner revision, provider/adapter versions, dependency audit date/result.
- Runner UUID and display name; Tailscale IP/port and access restriction owner.
- Install/config/state/socket paths; unit name; enabled/active/linger status.
- Owner-key label only; provider authentication method and whether its required
  environment was explicitly supplied to systemd.
- HiveMake agent binding, intended autonomy, and old-poller disposition.
- MCP endpoint and whether actual worker tool access was tested.
- Specifically authorized acceptance ticket IDs/results and outstanding limits.

Do not report “fully tested” based on registration or adapter discovery alone.
