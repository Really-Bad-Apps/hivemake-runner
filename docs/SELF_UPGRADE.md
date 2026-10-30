# Ticket-authorized runner upgrades

The admin worker can carry out an explicitly requested upgrade without asking
for the same permission again. Read-only workers cannot. The restart runs in an
independent user-systemd job, outside the daemon/worker cgroup. The ticket stays
pending until the new runner is verified; staging files is not completion.

## One-time host setup

Existing installations need this release and the updated, host-customized
`ticket-admin.md` profile installed from a separate authorized admin session.
Older runners do not have the handoff protocol. Preserve your profile's host
scope and existing restrictions when merging its new upgrade section. Do not
replace a customized profile with an unfilled template.

Add this owner-controlled policy to `runner.yaml`, adapting paths and agent ID:

```yaml
self_upgrade:
  service: hivemake-runner.service
  releases: /home/foo/.local/share/hivemake-runner/releases
  environment: /home/foo/.config/hivemake-runner/runner.env
  agents: [vulcan-admin-agent]
```

This example is not Vulcan-specific: substitute the account and registered local
admin ID on each host. Paths must be absolute, with no spaces or systemd
specifier characters. The daemon must be the MainPID of the named **user** unit.
The unit must use exactly the named required `EnvironmentFile`. The standard
adapter commands are supported; custom wrappers require a separate upgrade.
Keep profile files outside release directories. Do not put unrelated agent
markers in the releases folder or broaden discovery roots to include it.

Enable this once while the runner is idle. A running worker cannot change this
policy or its own profile to grant itself the exception. Existing provider and
HiveMake credentials remain private on the host. No remote caller can access
the maintenance endpoints: they are available only on the mode-0600 Unix socket.
This remains a behavioral policy within the same OS account, not an OS sandbox.

## Worker procedure

1. Read the ticket and corrections. Require the reviewed **full commit SHA** and
   scope. Do not infer that an arbitrary new main commit is approved.
2. Create a separate checkout under the configured releases directory, with
   origin `https://github.com/Really-Bad-Apps/hivemake-runner.git`, checked out at
   that SHA. Never pull into, replace dependencies in, or delete the active
   release. Keep previous releases for rollback.
3. In that new checkout, prepare `.venv` and adapters using the installation
   guide's pinned dependency procedure (`uv pip install` with
   `--override git-overrides.txt`, and `npm ci --ignore-scripts`). Require a clean
   tracked checkout. Do not run production tests without specific permission.
4. From the **active runner checkout**, using its venv interpreter, request the
   handoff (replace paths and SHA):

   ```bash
   .venv/bin/python -m runner.upgrade request \
     --config /home/foo/.config/hivemake-runner/runner.yaml \
     --revision FULL_REVIEWED_SHA \
     --release /home/foo/.local/share/hivemake-runner/releases/FULL_REVIEWED_SHA \
     --stage-logging
   ```

   The runner injects `HIVEMAKE_RUN_ID` and `HIVEMAKE_RUNNER_SOCKET` into managed
   dispatcher workers. Requests outside that context are rejected. If the active
   installation uses a root-level virtual environment, use `bin/python` instead.
   `--stage-logging` is only for a ticket requesting local-only logging setup:
   sets `DEBUG_LOCAL=true`, `LOG_LEVEL=INFO` and empties `LOKI_ENDPOINT`,
   `LOKI_USER`, `LOKI_PASSWORD`, `LOKI_CA_BUNDLE_PATH`. Omit it to preserve logging.
5. Return the command's JSON object with `outcome: runner_upgrade` as the final
   response. Do no more operational work after submitting the handoff. Do not
   mutate ticket lifecycle or directly restart your own service.

## Completion and recovery

The daemon rejects new work while maintenance is reserved. Existing work drains
without a fixed drain deadline. The external job creates private backups under
`state_dir/upgrades/<execution-id>`, then switches adapter paths and writes only
`90-hivemake-upgrade.conf` in the named user unit's drop-in directory. Existing
identity, state, bindings, profiles, credentials and unrelated configuration
are preserved. The environment file must use one assignment per line; unsupported
multiline values are rejected before stopping the service.

Verification requires a different process/boot ID, exact revision, preserved
runner UUID and agent inventory, and successful normal HiveMake registration.
For logging staging it also requires local logging mode. This uses the local
control socket and normal registration, not a synthetic production test ticket.
Startup verification is bounded to 120 seconds; ticket calls and draining are
not limited by that bound.

An ordinary switch/startup failure restores the backed-up config, environment
and previous drop-in, then starts the previous runner and reports `needs_human`.
It never reports a failed upgrade as success. A killed job or host power loss
cannot guarantee automatic rollback: once a daemon is running, its watchdog
marks a lost handoff failed; if no daemon can start, a separate admin must restore
the private backups and start the service. Consult the user journal for
`hivemake-upgrade-<execution-id>.service` and private `state_dir/upgrade.json`.
Do not paste backup environment contents into tickets. Do not retry an ambiguous
upgrade blindly. Preserve the execution ID for recovery.

A failed handoff does not change the read-only template. Ordinary service
operations still follow the admin profile's configured host scope.
