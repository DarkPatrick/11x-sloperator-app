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
the `time-series-research` skill, and replies with an evidence-backed likely cause and action.
The same trusted-user thread continuation and no-channel-status rules apply.

Repeated alerts are grouped persistently by their failure shape: upstream severity, downstream
severity, and ingestion-probe state. Platform, flow kind, evaluated hour, and changing values do
not make a continuing incident look new. Sloperator tracks the affected platform-and-flow
components and suppresses another agent launch until every component has received the monitor's
threaded `Recovered` event. Recovery is tied to the latest alert timestamp for that component,
so a delayed reply from an older alert cannot close a newer recurrence. A different failure shape
or the same shape after full recovery launches a new investigation.

## Claude and Codex sessions

Any authorized DM that is not one of the commands above starts or resumes an agent
session in `/home/egor/projects/ug-ai-analyst`.

One Slack thread maps to exactly one durable CLI session:

- a new top-level Chat starts a new session;
- replies in that thread resume the same session;
- messages in one thread execute serially;
- different threads are bounded by `SLOPERATOR_AGENT_MAX_CONCURRENCY`;
- Slack delivery retries are deduplicated before a paid agent turn starts.
- a stop command in the thread terminates that turn's entire process group.

While an agent is working, another ordinary message in the same Slack thread steers
the active turn. Codex uses the native App Server `turn/steer` protocol. Claude is
interrupted and immediately resumed in the same durable session with the additional
guidance, because its print-mode protocol does not guarantee same-turn steering.
Sloperator acknowledges accepted guidance in the thread. Prefix a message with
`next:` when it should wait and run as a distinct follow-up turn.

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

Claude Opus is the default. Select the provider and model in the first message:

```text
[claude] Analyze experiment 1234
[claude:opus] Analyze experiment 1234
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

## Production

The included `deploy/sloperator.service` applies restart and resource-limit policies.
Install it as a systemd unit only after reviewing its absolute paths:

```bash
sudo cp deploy/sloperator.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sloperator
```
