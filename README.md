# 11x Sloperator

A private Slack bot for one authorized user. It connects through Slack Socket Mode,
so it does not need a public HTTP endpoint.

## Requirements

- Python 3.13
- A Slack bot token (`xoxb-…`)
- A Socket Mode app token (`xapp-…`) with `connections:write`
- Slack event subscription for `message.im`
- Bot scopes: `chat:write`, `im:history`

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

Messages from users other than `SLACK_USER_ID` are ignored.
Replies are posted into the same Slack thread so they remain visible in the active Chat.

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
