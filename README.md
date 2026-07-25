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

## Commands

- `ping`
- `status`
- `help`

Messages from users other than `SLACK_USER_ID` are ignored.

## Production

The included `deploy/sloperator.service` applies restart and resource-limit policies.
Install it as a systemd unit only after reviewing its absolute paths:

```bash
sudo cp deploy/sloperator.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sloperator
```
