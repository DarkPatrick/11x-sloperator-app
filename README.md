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

Messages from users other than `SLACK_USER_ID` are ignored.
Replies are posted into the same Slack thread so they remain visible in the active Chat.

## Production

The included `deploy/sloperator.service` applies restart and resource-limit policies.
Install it as a systemd unit only after reviewing its absolute paths:

```bash
sudo cp deploy/sloperator.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sloperator
```
