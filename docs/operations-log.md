# Operations channel

`sloperator-operations.service` is independent of the bot process. Set
`SLOPERATOR_LOG_CHANNEL` to the channel ID; invite the bot to that channel.
It sends short deterministic summaries (no summarization agent), keeps evidence
on the server, and reports Claude `/usage` in a separate post in each UTC
half-hour slot. The first report is sent on enablement. Slot state survives
restarts. Unavailable quota is reported as unknown, never as zero.

## Coverage

| Source | How it is captured |
| --- | --- |
| All embedded schedules, including Jira workers/reviewers and recovered runs | Transactional SQLite triggers on `scheduled_agent_runs`; scheduler decisions/errors from journald |
| Every Slack trigger, DM, mention and follow-up, including new trigger names | Transactional triggers on requests and sessions, without a trigger-name allowlist |
| Claude execution, communication gates, SQL helpers, retries and recovery turns | The shared `run_claude` execution boundary |
| Codex turns, including admin and SQL helpers | The shared App Server `run_turn` boundary |
| Child Claude agents of recorded Sloperator sessions | Incremental subagent transcripts; prompts and thinking are not sent |
| Admin SQL execution and visualization | Shared method boundaries, including exceptions and cancellation |
| Direct scheduler/agent Slack messages, updates and attachments | Shared observed Slack client: delivery is recorded only after API success; errors separately |
| All user cron commands, enabled and disabled, including new/unmanaged commands | Complete shell-command wrapper; coverage reconciled every minute; original schedule/environment/redirections/exit status preserved |
| Detached `cron_retry` attempts | Incremental retry protocol in all `scripts/logs/*.cron.*.log` files, including rotation |
| Bot and analytics updater services, including newly named `sloperator*`/`ug-ai-analyst*` units | Persistent journald cursor, including systemd restart/crash records |
| Unexpected unwrapped user cron starts | CRON journal records, plus a coverage warning if wrapping fails |
| Monitoring failures | An explicit collector failure event; logging never silently claims full coverage |

Scope is Sloperator and its analytics automations. Unrelated operating-system
services and unrelated interactive user agent sessions are excluded. Cron command
completion means the wrapper observed the process exit; it does not assert that
a business deliverable was produced. Agent return, business blocker/failure, and
Slack delivery are separate events. In particular a `completed` agent row carrying
`Experiment finalisation failed:` is displayed as a failure.

Very frequent service events are grouped in short digests. Raw events and
references (original files, source threads, journal commands) are in
`data/operations/deliveries/<first-id>-<last-id>.json`. Cron run evidence is in
`data/operations/cron-runs/`; original job log files remain in place. Slack posts
give the actual hostname and absolute server path. These are server locations,
not invented publicly accessible web links; use SSH to read them. No raw-log HTTP
endpoint or public access to credentials is introduced.

## Delivery and recovery

Events live in `operation_events` in the existing SQLite database. Inserts and
state transitions are recorded in the same transaction as the task state, so a
short run cannot be missed by polling. A durable outbox marks delivery only after
Slack accepts it. Rate limits and network outages leave the queue intact. A
stable `client_msg_id` is supplied on retries; an ambiguous network failure after
Slack accepts a message can still cause a duplicate (at-least-once delivery).

Cron wrappers write atomic spool records even while the collector/bot is down.
The collector detects a wrapper process disappearing without a terminal event.
Its independent systemd service restarts after failure. Cursor/checkpoint state
and usage slots survive restarts. Existing historical runs are not re-posted on
initial installation. A collector outage can delay logs but should not lose them.

Cron changes are backed up to `data/operations/crontab-before-*.txt` before
installation. Instrumented commands point to immutable command specifications in
`data/operations/cron-specs/`; the admin UI unwraps them for inspection and retains
its existing enable/disable controls. Commands using cron's unescaped `%` stdin
syntax fail coverage explicitly instead of being silently altered. Coverage
rechecks the crontab immediately before replacement to reduce edit races.

## Deployment and rollback

1. Run the operations tests and the existing runner/store/bot/admin tests.
2. Set `SLOPERATOR_LOG_CHANNEL`, install the service unit, then
   `systemctl daemon-reload` and enable/start `sloperator-operations`.
3. Restart `sloperator`; confirm a new PID, start time and Socket Mode session.
4. Confirm an actual cron start/result, usage post, delivery record, and coverage
   inventory in the Slack channel. Check both service journals on failure.

To disable: stop/disable `sloperator-operations`. Restore the pre-install crontab
(or unwrap the current specs to preserve newer edits) before removing wrapper
code. Stopping only the collector leaves safe local cron spool logging active.
Do not delete pending events to clear a Slack outage. Raw evidence is private;
manage disk retention according to local operational requirements.
