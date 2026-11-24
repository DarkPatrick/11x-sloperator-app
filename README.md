# 11x Sloperator

A private Slack bot for one authorized user. It connects through Slack Socket Mode,
so it does not need a public HTTP endpoint.

## Requirements

- Python 3.13
- A Slack bot token (`xoxb-…`)
- A Socket Mode app token (`xapp-…`) with `connections:write`
- Slack event subscriptions for `message.im` and `message.channels`
- Bot scopes: `chat:write`, `files:write`, `im:history`, `channels:history`

## Local setup

```bash
python3.13 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
.venv/bin/sloperator
```

The liveness endpoint is available at `http://127.0.0.1:8080/healthz`.

## Private Slack archive

On startup, Sloperator maps every conversation visible to its bot token and performs a
bounded history backfill for conversations it can actually read. New delivered Slack
events are stored immediately. A bounded reconciliation runs every five minutes to
recover messages missed because of event configuration or temporary disconnects.
An event from a previously unknown DM, MPIM, or channel adds it to the map immediately.
Membership and channel metadata events also trigger an immediate metadata refresh.

The default path is `data/sloperator.sqlite3`. The entire `data/` directory and common
SQLite database/WAL extensions are ignored by Git. The directory is mode `0700` and
the database is mode `0600`. Message bodies are never written to process logs.

Inspect counts or the channel map locally:

```bash
.venv/bin/sloperator-inspect status
.venv/bin/sloperator-inspect channels --members-only
```

## Local admin UI over SSH

The operational UI is served at `/admin` on the same loopback-only HTTP listener as
`/healthz`. It is not exposed to the internet and has no public login surface. Open it from
your workstation through an SSH tunnel:

```bash
ssh -N -L 8080:127.0.0.1:8080 egor@SERVER
```

Then open <http://127.0.0.1:8080/admin> in the workstation browser. SSH authenticates the
operator; the UI additionally uses a per-process CSRF token for mutations.

The UI shows the current user crontab, cron launch records from the last seven days of the
system journal, and recent durable agent sessions. An agent turn can be stopped, a session
can be permanently closed, or a message can be sent into the existing session. Agent replies
continue to be delivered to the original Slack thread. The **Usage** tab keeps per-invocation
token accounting for every logical agent, with 30-day aggregates, daily dynamics, failures,
duration, provider/model, and recent calls. Input, cache creation, cache reads, and output stay
separate. Stable names such as `experiment-finalizer/worker`,
`experiment-finalizer/reviewer`, and `experiment-finalizer/slack` let each role be compared over
time. Claude uses provider JSON counters and falls back to the local transcript delta (including
child agents); Codex uses the App Server completion event.

The schema includes disabled-by-default trigger rules and an action-run ledger. No
actions execute until an explicit condition and action are configured.

Slack only sends activity for conversations the bot can access. Subscribe the app to
`message.channels`, `message.groups`, `message.im`, and `message.mpim` to archive new
activity from each supported conversation type.

## Commands

- `ping`
- `status`
- `help`
- `stop` / `cancel` / `стоп` / `отмена` — cancel the active agent turn in that thread
- `next: <request>` — queue a separate turn instead of steering the active one
- `vpn` / `vpn status` / `vpn stop` — manage the isolated corporate VPN

DMs from users other than `SLACK_USER_ID` are ignored. In agent-enabled monitoring threads,
users listed in `SLACK_ALLOWED_CONVERSATION_USERS` may continue the existing agent session.
Replies are posted into the same Slack thread so they remain visible in the active Chat.

## Analytics anomaly auto-replies

Sloperator reacts immediately when Analytics Bot mentions `SLACK_USER_ID` in
`#ug-analytics-monitoring`. It reconstructs the bot's split alert batch from recent channel
history, checks the alerted metrics against the same week-over-week rule used by the former
GitLab cron job, and replies in the mention message's thread. Messages from other bots and
mentions in other channels are ignored.

When the check confirms an anomaly for a metric in Airflow's pinned `ug_monetisation` group,
Sloperator starts a durable agent session in `/home/egor/projects/ug-ai-analyst`. The first turn
must use the `time-series-research` skill to investigate the movement and recommend an action.
Later replies from users in `SLACK_ALLOWED_CONVERSATION_USERS` in that same channel thread
continue the session; other users and unrelated channel threads cannot launch or steer it.
Channel-thread sessions post no Slack assistant status or heartbeat.

The responder requires `ANOMALY_*` and `CLICKHOUSE_*` settings documented in `.env.example`,
membership in the monitoring channel, the `message.channels` event subscription, and the
`channels:history` scope. Slack thread history provides durable deduplication, while an
in-process guard suppresses concurrent delivery retries.

## Subscription-flow incident investigations

When `subscription_flow_monitor.py` posts a `SERIOUS` alert, Sloperator starts a Claude
investigation in the alert thread with the exact detector output and an explanation of its
upstream/downstream baseline model. The agent runs in `/home/egor/projects/ug-ai-analyst`, uses
the known-incident check before `time-series-research`, and replies with an evidence-backed
likely cause and action. New platform components from one alert are claimed atomically and
combined into one investigation and one shared answer (at most 120 words). Incident deduplication
and recovery remain per platform; already active components are context only. Missing measurements
and expected baseline volumes must not be presented as measured lost purchases.
The same trusted-user thread continuation and no-channel-status rules apply.

## Mobile health critical-drop investigations

Sloperator watches the UG mobile monetisation health report in
`#ug-monetization-metrics-monitoring`. For each report it selects at most five Android/iOS
metrics that are both red (negative direction) and `critical`, then starts a durable Claude
investigation in the report thread. The prompt requires the analyst to use the detector
semantics and mobile-health datamart knowledge in `ug-ai-analyst`; replies from allowed users
in the same thread continue that session. Configure the source with
`MOBILE_HEALTH_ALERT_CHANNEL` and `MOBILE_HEALTH_BOT_ID`.

Before querying data, Mobile and Web health agents inspect completed Sloperator analyses in the
same channel from the previous five days. An exact match requires the same complete set of
platform/card/metric identities; changing values and diagnostics do not make the alert new. When
a completed match exists, the fresh alert thread receives a link to that analysis instead of a
duplicate investigation and ZIP artifact. Partial overlap, raw alerts, and progress replies are
never reused.

Confirmed Analytics Bot monetisation anomalies apply the same five-day preflight in
`#ug-analytics-monitoring`, matching the complete set of metric/platform/type identities. Its
existing 24-hour per-identity launch cooldown remains the first deduplication layer.

The same channel and monitor bot can also post `UG Monetisation: WEB health monitoring`
reports. Sloperator selects at most five red critical metrics from the Web section and starts
an equivalent investigation grounded in dashboard 104's card-level SQL and the repository's
`ug_web_health_monitoring.md` knowledge source.

## Daily red-alert dashboard

The monitor posts three reports per run into `#ug-monetization-metrics-monitoring` a few
minutes apart: `UG Monetisation: WEB health monitoring`, `UG Monetisation: Mobile Health
Monitoring Dashboard`, and `UG Monetisation: WEB Subscriptions Monitoring`. Sloperator waits
until all three of one run have arrived, including their continuation chunks, and then rebuilds
the "Daily red alerts" Metabase dashboard from their `:red_circle:` alerts.

The rebuild runs no agent. It executes the `ug-ai-analyst` pipeline directly — the metric
registry, the report parser, the per-alert chart and KPI query generator, and the publisher —
so every red alert becomes one chart plus two KPI tiles, grouped into Web / iOS / Android / Web
subscriptions tabs. Sloperator then posts one line into the channel — `:bar_chart: Свежий
дашборд за <date> по аномалиям собран`, with the dashboard link behind the opening phrase — and
a failed rebuild is reported privately to the owner instead.

Only the last report of a run starts the rebuild, and the run is recorded in
`data/alert-dashboard-state.json`, so a redelivered event or a service restart does not rebuild
it twice. Reports further apart than an hour belong to different runs and never complete a set.
Configure it with `ALERT_DASHBOARD_DETECTOR_SRC`, `ALERT_DASHBOARD_ID`,
`ALERT_DASHBOARD_COLLECTION`, and `ALERT_DASHBOARD_TIMEOUT_SECONDS`.

Confirmed Analytics Bot anomalies are deduplicated per metric, platform, and metric type.
Once an agent investigation is launched for a combination, repeated alerts for that same
combination are excluded from agent analysis for 24 hours; new combinations in a mixed alert
are still investigated.

Repeated alerts are grouped persistently by their failure shape: upstream severity, downstream
severity, and ingestion-probe state. Platform, flow kind, evaluated hour, and changing values do
not make a continuing incident look new. Renewals use the monitor's pooled same-hour baseline with
day-of-month correction. Acquisitions use the same-hour/same-weekday baseline with a three-sample
minimum, pooled fallback, no day-of-month correction, and a reference-spread gate before a
non-catastrophic single-hour SERIOUS verdict.

Sloperator tracks the affected platform-and-flow components and suppresses another agent launch
until every component has received one of the monitor's terminal threaded events: `Recovered`
means cumulative volume caught up, while `Back to normal` means the dynamic stabilized but the
missing volume never returned. Closure is tied to the latest alert timestamp for that component,
so a delayed reply from an older alert cannot close a newer recurrence. A different failure shape
or the same shape after full closure launches a new investigation.

## Claude and Codex sessions

Any authorized DM that is not one of the commands above starts or resumes an agent
session in `/home/egor/projects/ug-ai-analyst`.

One Slack thread maps to exactly one durable CLI session:

- a new top-level Chat starts a new session;
- replies in that thread resume the same session;
- messages in one thread execute serially;
- different threads are bounded by `SLOPERATOR_AGENT_MAX_CONCURRENCY`;
- Slack delivery retries are deduplicated before a paid agent turn starts.
- after 24 hours without a user message, the thread session expires permanently;
  later replies cannot resume it or create another agent session in that thread;
- a stop command in the thread terminates that turn's entire process group.

While an agent is working, another ordinary message in the same Slack thread steers
the active turn. Codex uses the native App Server `turn/steer` protocol. Claude is
interrupted and immediately resumed in the same durable session with the additional
guidance, because its print-mode protocol does not guarantee same-turn steering.
Sloperator acknowledges accepted guidance in the thread. Prefix a message with
`next:` when it should wait and run as a distinct follow-up turn.

Authorized messages can include images and files, including file-only messages. Sloperator
downloads originals (up to 50 MiB per file) into the agent workspace under
`output/slack_inputs/` and passes local paths to Claude or Codex. An unavailable attachment
is explicitly identified in the worker prompt. Slack's `message` and `app_mention` deliveries
share the same request deduplication; file IDs are persisted before downloading so an
interrupted download can recover after restart.

Follow-up answers can be text-only. Agents should attach an analysis ZIP only when the current
request needs new or materially updated evidence. Unchanged archives from earlier turns are
suppressed, and delivered archive content is tracked per thread in SQLite. Repackaging the same
files with different ZIP timestamps, compression, or archive names does not upload them again.

## Isolated corporate VPN

When LDAP credentials and a VPN profile are configured, Sloperator starts a
resource-limited OpenVPN container only after the owner replies `vpn ready` or
`готов` to its DM. It then completes the LDAP form itself and asks for the OTP;
a bare 6-8 digit reply completes that pending login. OTP messages are redacted
before local event archival.

The container publishes an HTTP CONNECT proxy on localhost only. When VPN is
connected, Claude and Codex inherit that proxy, so their HTTP/HTTPS tools can reach
corporate services without changing host routes or risking the server's SSH session.
Build the pinned local image after cloning or whenever its definition changes:

```bash
sudo docker build -t local/openvpn-agent:24.04 deploy/openvpn
```

On Ubuntu hosts that restrict unprivileged user namespaces through AppArmor,
install the included Codex profile so its bubblewrap-based `workspace-write`
sandbox can start without disabling the host restriction globally:

```bash
sudo cp deploy/apparmor.codex /etc/apparmor.d/codex
sudo apparmor_parser -r /etc/apparmor.d/codex
```

Claude Opus 5.5 is the default. Select the provider and model in the first message:

```text
[claude] Analyze experiment 1234
[claude:claude-opus-5-5] Analyze experiment 1234
[codex:gpt-5.6-sol] Review the analysis scripts
```

Provider and model are fixed for the lifetime of a thread. Start a new Chat to choose
different values. Defaults, CLI paths, timeout, workspace, and concurrency are
configurable through the variables documented in `.env.example`.

Each new agent session is explicitly instructed to run the repository
`scripts/freshness_preflight.sh` before substantive work. Claude uses resumable JSON
sessions; Codex uses resumable JSONL sessions with a `workspace-write` sandbox and
sandboxed network access. Agent processes never bypass approval or sandbox controls.
The automatic repository updater and agent processes share an exclusive Git lock so
they cannot modify the working tree concurrently.

## Scheduled jobs across restarts

The embedded scheduler loops start right after Slack Socket Mode connects, before the startup
recovery of interrupted agent runs. Each loop logs its usual `Next ... scheduled for ...` line
when it starts. The loops are the experiment finalizer (UGM, 12:00), the CRM finalizer (14:00),
the experiment design planner and the
experiment analytics planner. They also include the Jira task automation hourly launcher and
ten-minute poll, the automation error audit, the daily activity digest, the skill docs sync and
the MySQL docs sync. The `*_ENABLED` settings still decide whether a loop is created. The Slack
archive sync loop and the VPN monitor still start after the recovery and the initial archive
sync, and `Sloperator started` is still logged last.

Recovery gate. At startup the service notes, per job, which of the job's scheduled agent runs
are unfinished. It uses the job's `run_job_names` from `scheduled_jobs.py`, and for the finalizers
also their overdue sweeps: `results-overdue-sweep` (UGM) and `crm-results-overdue-sweep` (CRM). While the recovery is still running, a job that is due waits until
those runs are finished. It re-checks about once a minute and logs `Scheduled <job> is waiting
for the recovery of its interrupted run(s): ...` once. The gate opens for every job when the
recovery finishes. Runs started after startup never hold it.

Slots and catch-up. Eight jobs run on a daily or weekly schedule: the two finalizers (UGM and
CRM), the two planners, the audit, the digest and the two docs syncs. Each of their scheduled times (a slot)
fires at most once. A slot missed while the service was down, still starting, or held by the
gate fires once after startup, provided it is still inside the catch-up window. The window is at
most 6 hours old and the same calendar day in the job's own timezone. Only the most recent
missed slot is considered, so a multi-day outage gives at most one run. A catch-up logs
`Catching up the missed <job> slot ...`. The digest also waits for the initial archive sync. The
Jira launcher and poll have no slots: they already check once as soon as they start.

Slot record. Before a slot runs, the slot is recorded in the SQLite `metadata` table under the
key `scheduler-slot:<job>`, with the slot time in ISO format. A slot skipped because the job is
disabled from the admin is recorded too. So a restart in the middle of a run does not repeat the
slot (the existing recovery resumes the interrupted run). A slot disabled from the admin also
does not run later. On the first start of a job with no record, its most recent past slot is
recorded without running it. If the record cannot be read or written, the job logs an error,
skips that slot, and waits for the next one. Inspect the records with:

```bash
sqlite3 data/sloperator.sqlite3 "SELECT key, value FROM metadata WHERE key LIKE 'scheduler-slot:%'"
```

## CRM finalizer

CRM (Bloomreach) email experiments have their own finalizer, `crm_finalizer.py`, separate from the
UGM finalizer, which skips CRM. It runs on weekdays at `CRM_FINALIZER_HOUR` (default `14`)
Asia/Nicosia and is switched by `CRM_FINALIZER_ENABLED` (default `true`) and by its admin row
`crm-finalizer`. Each run has up to three steps:

- First read: a send whose 3-day attribution window has closed gets its results block without
  After Split. There is no trial-maturity wait.
- After Split post-analysis: from day 7, once pending trials on `UG_CRM` / `CRM purchases` pass
  the gate, the post-analysis block is added after the send's Insights. At most one per run.
- Overdue sweep of CRM `Experiment results` tasks.

A run with nothing to publish posts nothing to Slack. The 19:00 daily digest carries a separate
forecast line for the CRM queue (`daily-activity-crm-forecast`).

## Jira code tasks (`claude-pr`)

Sloperator can turn a Jira issue into a draft GitHub PR in a muse-tech repository. An
orchestrator Claude session in ug-ai-analyst (job `jira-code-orchestrator`) drives a nested
session in the claude-workspace-ug umbrella repo. It reviews the nested result against the
issue, sends rework (at most 3 rounds per turn), has a draft PR opened, and reports in Jira.
The feature is off by default (`CODE_TASK_ENABLED=false`).

Creating a task:

- Any Jira project. Assign the issue to the ug-ai-analyst service account, add the label
  `claude-pr` (`CODE_TASK_LABEL`) and leave it in a status of the "To Do" category.
- The reporter must be trusted. The trusted set is the people in
  `SLACK_ALLOWED_CONVERSATION_USERS` plus the owner `SLACK_USER_ID`, mapped to Jira by a unique,
  exact (case-insensitive) match of their Slack real name to an active Jira display name, plus
  the Jira account ids in `CODE_TASK_ALLOWED_REPORTERS`. Unmatched or ambiguous people are left
  out with a warning. The set is cached for an hour. If resolution fails and there is no cache,
  only `CODE_TASK_ALLOWED_REPORTERS` is trusted. Issues from untrusted reporters are skipped
  without a Jira write.
- Write the request in the description. Name the repository and the acceptance criteria. If the
  request is unclear, the agent asks one question in Jira and waits.

What happens:

- Launcher, every 10 minutes: takes the oldest eligible issue (one per cycle, none while another
  code task is in work). It runs the abuse pre-check on summary, description and comments, then
  starts the orchestrator. The issue moves to In Progress with a start comment. Once the draft PR
  is open, the agent posts the PR link, a summary, and the checks it ran and skipped, then moves
  the issue to In Review. Only headless checks such as `make be-unit` run. There are no
  stand, browser or VPN checks.
- Poll, every 10 minutes: resumes a task left unfinished by a timeout, failure or restart. A
  new comment by a trusted person on a waiting or in-review task goes through the reply
  decision. A comment that asks for a change is forwarded to the same nested session, the fix is
  pushed to the same PR branch, and the agent replies in the comment thread. Comments by the
  service account or untrusted people never wake the agent. A status change without a comment
  does nothing.
- An issue moved to Done, reassigned away or unlabelled stops being tracked. Its record is
  deleted a day later.
- Three turns in a row without a result verdict park the task and post one Jira comment asking
  a person to look. Suspected abuse posts one Jira comment, alerts the owner in Slack and turns
  the feature off in the admin (`jira-code-tasks (sloperator.service)`).
- The agent never merges a PR, marks it ready, pushes to a default branch or force-pushes a
  branch it did not create. No token budget applies. Cost is bounded by the Claude quota gate,
  one task in work, the timeouts and the parking rule. An orchestrator turn holds the
  ug-ai-analyst workspace lock for its whole length, so Slack and other agent turns in that
  workspace wait.

Progress is stored in the SQLite table `jira_code_task_links`, separate from the analytics
Jira automation. That automation ignores issues with the label even when this feature is off.
Nested session state (session id, process, result) is stored in `CODE_TASK_STATE_DIR`. The
orchestrator calls `python -m sloperator.workspace_session send|wait --task KEY` itself.
People do not need to run it.

| Variable | Default | Meaning |
|---|---|---|
| `CODE_TASK_ENABLED` | `false` | starts both loops |
| `CODE_TASK_LABEL` | `claude-pr` | trigger label; must not be empty |
| `CODE_TASK_WORKSPACE` | see `config.py` | claude-workspace-ug checkout for nested sessions |
| `CODE_TASK_ORCHESTRATOR_TIMEOUT_SECONDS` | `7200` | one orchestrator turn; 300..86400 |
| `CODE_TASK_NESTED_TIMEOUT_SECONDS` | `3600` | one nested turn; 300..86400 |
| `CODE_TASK_MODEL` | `claude-opus-5-5` | nested session model |
| `CODE_TASK_CLAUDE_CLI` | see `config.py` | Claude CLI of nested sessions; set it to the server's CLI path |
| `CODE_TASK_STATE_DIR` | `data/code-tasks` | nested session state files |
| `CODE_TASK_GH_CONFIG_DIR` | unset | `GH_CONFIG_DIR` of the nested session |
| `CODE_TASK_GIT_SSH_COMMAND` | unset | `GIT_SSH_COMMAND` of the nested session |
| `CODE_TASK_GIT_NAME` | unset | git author and committer name |
| `CODE_TASK_GIT_EMAIL` | unset | git author and committer email |
| `CODE_TASK_ALLOWED_REPORTERS` | empty | extra trusted Jira account ids, comma-separated |

A bad timeout or an empty label stops service start with a `ConfigurationError`. The
`workspace_session` tool itself accepts any positive nested timeout. The `CODE_TASK_` prefix is
deliberate: agent child processes inherit it, while `SLOPERATOR_*` variables are stripped.

GitHub identity. With all identity variables unset, nested sessions use the service user's own
`gh` login and ssh key. To switch to a bot account, change configuration only. Point
`CODE_TASK_GH_CONFIG_DIR` at a `gh` config directory logged in as the bot. Set
`CODE_TASK_GIT_SSH_COMMAND` to an ssh command with the bot's key, for example
`ssh -i <bot key> -o IdentitiesOnly=yes`. Set `CODE_TASK_GIT_NAME` and `CODE_TASK_GIT_EMAIL` to
the bot's values. Then restart the service. A raw `GH_TOKEN` is not supported: every agent the
service starts would inherit it.

Server deployment (manual, not in `deploy/`):

- systemd unit: add `ReadWritePaths` for the claude-workspace-ug checkout and the product
  worktree base (`~/orca/worktrees`, or set `WT_BASE` for the service). Raise `MemoryMax`
  (suggested 4096M) and `TasksMax` (suggested 512), because two Claude processes, git and docker
  clients run at once. Set `GH_NO_UPDATE_NOTIFIER=1`, because `gh` cannot write its state file
  under `ProtectHome=read-only`. `~/.config/gh` and `~/.ssh` stay readable. `known_hosts` must
  already contain github.com.
- `.env`: `CODE_TASK_ENABLED=true` and `CODE_TASK_CLAUDE_CLI` set to the server's Claude CLI.
  Add the identity variables only when switching to a bot.
- Keep the server's claude-workspace-ug checkout on its default branch and up to date, with the
  docker images for `make be-unit` available.
- Branch protection on muse-tech default branches is the real guard against pushes there. Check
  it for every repository the agent may touch.
- Restart and confirm the service as described in `AGENTS.md`.

Known limits (v1):

- PR review comments on GitHub are ignored. Corrections come only from Jira comments.
- The nested session's `--disallowedTools` entries match command prefixes, so other spellings
  (`git push origin +branch`, `git -C dir push -f`, `gh api` merge or ready calls) are not
  blocked. The orchestrator session has no deny-list and relies on its prompt. Branch protection
  covers default branches only.
- The abuse pre-check skips the model call when every comment is by the service account. A
  requeued issue with only bot comments therefore starts without its description being checked.
- Untrusted comments never wake the agent, but the orchestrator still reads their text when it
  re-reads the issue.
- When several trusted comments arrive in one poll window, only the newest is the reply target.
  The others reach the agent only through its re-read of the issue.
- If a nested runner dies and its process id is reused, `wait` reports `running` instead of
  `interrupted` until the turn times out.
- The daily activity digest's queue section excludes the default label `claude-pr`, not a
  custom `CODE_TASK_LABEL`.

## Production

The included `deploy/sloperator.service` applies restart and resource-limit policies.
Install it as a systemd unit only after reviewing its absolute paths:

```bash
sudo cp deploy/sloperator.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sloperator
```

## Operations log

See [operations logging](docs/operations-log.md) for the dedicated Slack channel, full coverage
matrix, independent collector service, durable delivery queue, and half-hour Claude usage.
