# Native-terminal attachments retired

The Codex-specific connector, live-thread validation, terminal session wrapper,
and attachment execution manager are removed. This does not change ordinary
ACP sessions, owner registration, instruction profiles, execution admission or
run-result storage. The runner never stops the human's native Codex server.

## Compatibility

Old `/attachments/{id}` and `/deliveries/{id}` requests return HTTP 410 with
`terminal_replies_retired`. They cannot open a native socket, create a session,
submit a prompt or replay a result. `codex_socket` remains accepted only as an
ignored configuration key with a warning, so existing installations can upgrade
without losing ordinary runner service. Remove that key during maintenance.

The shared `runs.sqlite3` file must not be deleted or replaced. Historical
`terminal_attachments` and `terminal_deliveries` rows are retained. On startup,
old active/pending leases expire; admitted deliveries become failed-before-
submission, and submitted deliveries become uncertain. Completed, failed and
uncertain outcomes are preserved; none is replayed. Fresh installations do not
create those legacy tables.

## Preserved mechanisms

`runner/delivery_journal.py` retains stable event IDs, compatible payload
fingerprints, duplicate admission/conflict detection and restart recovery,
without any connector or prompt-sending capability. Its historical table and
field names remain compatible with existing records. Tests cover repeated
admission after restart and preservation of ambiguous outcomes. Future session
routing must supply its own destination authorization and ownership policy.

## Deployment order

Disable terminal registration/new return addresses and the terminal dispatcher
in the API release before replacing the runner. Inspect pending/sending records
and active leases before rollout; do not blindly replay an ambiguous result.
Drain normal runner work before restart. Back up its state and retain the prior
release for rollback; do not restore old delivery state to retry uncertain work.
No cleanup here requires dropping database tables or killing human terminals.

The source before retirement is retained in Git at
`feebd9717ba11dc1db9b3659f763e6a862a9004f`. Persistent/per-task session policy is
tracked separately in HiveMake ticket `becf00a4`; blocking MCP calls in `ef8f64a5`.
Neither feature ships with this retirement.
