# Install shared runner instruction profiles

Two profiles are provided together:

| Profile | Intended use |
| --- | --- |
| [ticket-read-only](../examples/instructions/ticket-read-only.md) | Inspect and answer; no file, configuration, Git or operational changes. |
| [ticket-admin](../examples/instructions/ticket-admin.md) | Host-owner-customized maintenance, ticket-requested deployments, configuration and cron changes. |

These are behavioral instructions, not enforced tool permissions or a sandbox.
Normal project instructions still load. Claude receives appended system-prompt
instructions; the pinned Codex adapter receives an initial user-level context
block. Direct human launches are unaffected. See [installation](INSTALL.md) for
the daemon, credentials, binding and production-test consent requirements.

## Customize the admin profile before installing it

The runner reads literal Markdown. It **does not substitute variables**, parse
the scope list, enforce allowed paths, or reject unfilled template placeholders.
Replace all `{{...}}` markers yourself in a host-specific copy. Use explicit
lists and absolute paths; write NONE for a category that is not allowed. The
template instructs the agent not to change anything if markers remain, but the
installer is responsible for producing and reviewing a completed profile.

Fill in the host/account, project instruction paths, compose project/file,
eligible services, editable configuration files, maintenance operations, passive
verification sources, cron policy and configuration repository. Permission on
Vulcan is not permission on Apollo, Athena, Zeus or another host. Only the owning
host administrator applies its copy. Do not copy Vulcan-specific `.env-hyperopt`,
Redis files, sibling-host names, cronwrap conventions or health endpoints blindly.

Commit permission defaults to **ALLOWED** for ticket-related changes in the
configured owned repository; NONE grants no repository scope. Running unattended
does not prohibit commits. Existing host approval requirements still apply; do
not ask again for approval already given for those commits. Push permission
defaults to **NOT ALLOWED** and requires separate owner configuration.
A host's no-remote repository does
not establish a push policy for other hosts. Protected runner/MCP/instruction
files remain excluded even when located inside a generally editable directory.

The generic template deliberately uses passive deployment evidence. Unlike the
original Vulcan example's broad endpoint-probe wording, a new production probe
or test still requires Jason's express consent for that activity and host.
It also follows the current dispatch contract: return the result for the
dispatcher to apply, rather than directly mutating tickets through MCP tools.

## PostgreSQL migration requests

Both worker profiles require `outcome: reject` for any ticket requesting
PostgreSQL migration execution, including a migration bundled into deployment.
Reject before operational changes; do not use a human-driver hold or perform a
partial deployment. The rejection must direct the creator to Athena
(`athena-server-admin-agent`) for Athena-hosted databases, or to the actual
database host owner otherwise. If the host is unknown, reject and tell the
creator to establish ownership before routing; do not assume every DB is on
Athena. SQL review and passive evidence inspection remain subject
to the normal read-only rules. The dispatcher applies the rejection.

Merge this rule into existing host-customized profiles; updating this repository
does not update installed copies. Preserve each host's scope and permissions.
The shared dispatcher prompt must also preserve explicit rejection rules instead
of converting every prohibited request into `needs_human`.

## Rollout order for an existing runner

### Deployment drains and escalation

The admin profile includes the named service's existing, host-approved drain
recipe in deployment authority, including its in-container CLI and passive
status checks. These steps do not need duplicate entries in the maintenance
whitelist. Require the documented command, target, timeout, exit status and
drained/idle state; failure or ambiguity stops rotation and returns `needs_human`.
This grants no arbitrary exec, database-shell, queue-purge or shared Redis
mutation permission. PostgreSQL migration execution still requires rejection.

The dispatcher must turn `needs_human` into a formal escalation with human
notifications, and observe human approval to allow another attempt within the
installed profile. Deploy the matching API/dispatcher fix (core 0.50.1 or later);
older dispatchers leave notes and holds instead. A profile update alone cannot
repair that server behavior.

Runner self-upgrade currently preserves installed profiles; it does not deliver
this text. Existing hosts need an owner-controlled, human-driven profile merge
using the procedure below. Preserve host-specific scope and restrictions. A
runner binary upgrade alone must not be reported as a completed policy rollout.

### Installation procedure

Use a **separate human-driven session**, not the unattended session whose limits
you are changing. Confirm the installation's OS user and unit type. The example
paths below describe foo's user service; adapt them deliberately for each host.
Do not overwrite an existing profile or switch a reference without reviewing it.

1. Prepare/review the fully customized file. Back up the current profile,
   `runner.yaml` and project `agent.yaml` privately with unique names. Keep
   backup agent markers outside discovery roots. Preserve unrelated settings.
2. Install the Markdown at an absolute path under the user's private configuration
   directory, for example
   `/home/foo/.config/hivemake-runner/instructions/ticket-admin.md`.
   Use mode 700 on the directory and 600 on the file, owned by the service user.
   The text must be nonempty UTF-8, NUL-free and at most 64 KiB.
3. Merge a new mapping into `runner.yaml` **without changing agent.yaml yet**:

   ```yaml
   instruction_profiles:
     ticket-admin:
       file: /home/foo/.config/hivemake-runner/instructions/ticket-admin.md
     ticket-read-only:
       file: /home/foo/.config/hivemake-runner/instructions/ticket-read-only.md
   ```

   Register only files you actually installed. Both profiles can coexist; merge
   into existing mappings rather than duplicating YAML keys. For a fresh daemon,
   prepare the mapping and marker before its first start instead.
4. Validate the candidate config/profile and a scratch copy of the proposed
   marker with the installed runner's `Config.load()`, `load_instructions()` and
   `discover()` functions, following the host's rules for validation. Do not
   start an adapter, model prompt or synthetic ticket. A scratch marker must
   stay outside live discovery roots. Check that no `{{...}}` markers remain.
5. Coordinate a restart window with the operator: prevent new dispatch, let
   existing runs finish, and confirm the runner is idle using its session/run
   state and available process/log evidence. The absence of child processes or
   one old completion log alone does not prove there is no queued/starting run.
   Do not restart a daemon that owns your current session. If idle state cannot
   be established, stop and ask the operator rather than interrupting work.
6. Restart the idle runner so it loads the new mapping. For a user unit, as its
   owning OS user:

   ```bash
   systemctl --user restart hivemake-runner.service
   systemctl --user status hivemake-runner.service --no-pager --full
   ```

   Confirm it stayed running and registered. There is no `daemon-reload` needed
   for runner YAML alone; that is for changes to the systemd unit itself.
7. **After the new mapping is loaded**, merge the reference into the project's
   existing `agent.yaml`, keeping its identity/name/harness:

   ```yaml
   runner:
     instruction_profile: ticket-admin
   ```

   Reference `ticket-read-only` instead for inspection-only projects. Wait for
   discovery (default 60 seconds) or request a normal inventory refresh, and
   confirm the agent is ready. Resume the operator's intended dispatch state
   only when the new marker has been discovered. Do not globally change autonomy.

This order avoids the live-discovery race: `agent.yaml` refreshes periodically,
but profile mappings load only on daemon startup. Referencing a new profile
before restart makes the agent unavailable with `instruction_profile_not_found`.
Do not rely on racing the 60-second scan by editing both files and restarting
"straight away."

If an already-referenced profile's path is unchanged and only its Markdown text
changes, no daemon restart is needed. Each new session rereads and snapshots the
file. Existing sessions keep their old snapshot; even an idle pre-created
session retains its old rules. Coordinate closure/recreation when changing scope.
The reported profile/hash/delivery metadata on an authorized new session records
which version it received. Do not launch a production test just to get that hash
without the required consent.

## Verification and rollback

Report the installed path, profile name, file SHA-256, permissions, runner ID,
mapping/marker change, backups, readiness and restart result. Keep credentials
and private transcripts out of the report. Inventory readiness is configuration
evidence, not proof the model obeys the profile. Real acceptance requires express
consent for the host and proposed test (including refusal tests).

For rollback, coordinate the same idle/dispatch window. Restore the previous
agent reference while its old mapping/file still exists and wait for discovery;
then restore old configuration/profile files and restart if the mapping changed.
Preserve the runner's identity and state. Keep a mapping until no live marker
references it; do not delete a shared profile still used by other projects.

### Ticket-authorized runner maintenance

The admin template includes a narrow self-upgrade exception when the host has
installed the [safe upgrade workflow](SELF_UPGRADE.md). A ticket grants the
requested operation within that installed policy; it does not require another
human session for every upgrade. The read-only template grants no such exception.
Existing hosts need a one-time profile/policy bootstrap before using the handoff.
