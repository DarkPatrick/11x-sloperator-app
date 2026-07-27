"""Loopback-only operational UI for cron and agent sessions."""

# ruff: noqa: E501

from __future__ import annotations

import asyncio
import json
import secrets
import subprocess
import time
from html import escape

from aiohttp import web
from slack_sdk.web.async_client import AsyncWebClient

from sloperator.agents import AgentOrchestrator, SubmitResult
from sloperator.store import EventStore

ADMIN_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Sloperator admin</title>
<style>
:root{color-scheme:dark;--bg:#111318;--card:#1b1f27;--muted:#9aa4b2;--line:#303744;
--green:#47d18c;--red:#ff6b6b;--blue:#6ea8fe}*{box-sizing:border-box}body{margin:0;background:var(--bg);
color:#eef2f7;font:14px/1.45 system-ui,sans-serif}main{max-width:1180px;margin:auto;padding:28px}
h1{margin:0 0 4px;font-size:28px}h2{margin:30px 0 12px}.sub{color:var(--muted)}
.grid{display:grid;gap:12px}.card{background:var(--card);border:1px solid var(--line);
border-radius:12px;padding:16px}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.spread{justify-content:space-between}.badge{padding:3px 8px;border-radius:99px;background:#303744}
.running{background:#174b35;color:#8cf0be}.failed{background:#552526;color:#ffaaaa}
button{border:1px solid var(--line);background:#252b35;color:#fff;border-radius:7px;padding:7px 11px;
cursor:pointer}button:hover{border-color:var(--blue)}button.danger{color:#ffaaaa}textarea{width:100%;
min-height:72px;background:#111318;color:#fff;border:1px solid var(--line);border-radius:8px;padding:10px}
pre{white-space:pre-wrap;word-break:break-word;background:#12151b;padding:10px;border-radius:8px;
max-height:320px;overflow:auto}.messages{max-height:300px;overflow:auto;margin:10px 0}.msg{border-left:2px
solid var(--line);padding:5px 9px;margin:4px 0}.meta{font-size:12px;color:var(--muted)}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:8px;border-bottom:1px solid var(--line)}
</style></head><body><main><h1>Sloperator</h1><div class="sub">localhost admin · access via SSH tunnel</div>
<h2>Agent sessions</h2><div id="sessions" class="grid"></div>
<h2>Configured cron</h2><div id="cron" class="card"></div>
<h2>Cron launch history</h2><div id="history" class="card"></div></main>
<script>
const csrf="__CSRF__"; const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",
">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
async function api(path,opts={}){opts.headers={...(opts.headers||{}),"X-Admin-CSRF":csrf};
const r=await fetch("/admin/api"+path,opts);if(!r.ok)throw new Error(await r.text());return r.json()}
async function action(path,body){await api(path,{method:"POST",headers:{"Content-Type":"application/json"},
body:JSON.stringify(body||{})});await load()}
function sessionCard(s){const k=encodeURIComponent(s.channel_id)+"/"+encodeURIComponent(s.thread_ts);
const msgs=(s.messages||[]).map(m=>`<div class="msg"><div class="meta">${esc(m.message_ts)} ·
${esc(m.user_id||m.bot_id||"unknown")}</div>${esc(m.text)}</div>`).join("");
return `<section class="card"><div class="row spread"><div><b>${esc(s.channel_name)}</b>
<span class="meta">${esc(s.channel_id)} / ${esc(s.thread_ts)}</span></div><span class="badge
${esc(s.runtime_status)}">${esc(s.runtime_status)}</span></div><div class="meta">${esc(s.provider)}:
${esc(s.model)} · turns ${s.turn_count} · updated ${esc(s.updated_at)}</div>
${s.last_error?`<pre>${esc(s.last_error)}</pre>`:""}<details><summary>Thread messages</summary>
<div class="messages">${msgs||'<span class="sub">No archived messages</span>'}</div></details>
<textarea id="m-${esc(s.thread_ts)}" placeholder="Message or steer this agent"></textarea>
<div class="row"><button onclick="send('${k}','m-${esc(s.thread_ts)}')">Send</button>
<button onclick="action('/sessions/${k}/stop')" ${s.active?"":"disabled"}>Stop process</button>
<button class="danger" onclick="closeSession('${k}')">Close session</button></div></section>`}
async function send(k,id){const el=document.getElementById(id);if(!el.value.trim())return;
await action("/sessions/"+k+"/message",{text:el.value.trim()})}
async function closeSession(k){if(confirm("Permanently close this session?"))await action("/sessions/"+k+"/close")}
async function load(){const d=await api("/state");document.getElementById("sessions").innerHTML=
d.sessions.map(sessionCard).join("")||'<div class="card sub">No sessions</div>';
document.getElementById("cron").innerHTML=d.cron_jobs.length?`<table><thead><tr><th>Job</th>
<th>Schedule</th><th>Command</th></tr></thead><tbody>${d.cron_jobs.map(x=>`<tr><td>${esc(x.name)}
</td><td><code>${esc(x.schedule)}</code></td><td><code>${esc(x.command)}</code></td></tr>`).join("")}
</tbody></table><details><summary>Raw crontab</summary><pre>${esc(d.crontab)}</pre></details>`:
'<span class="sub">No user crontab</span>';
document.getElementById("history").innerHTML=`<table><thead><tr><th>Time</th><th>Command</th></tr></thead>
<tbody>${d.cron_history.map(x=>`<tr><td>${esc(x.time)}</td><td><code>${esc(x.command)}</code></td></tr>`).join("")}</tbody></table>`}
load();setInterval(load,5000);
</script></body></html>"""


def _crontab() -> str:
    result = subprocess.run(
        ["crontab", "-l"], capture_output=True, text=True, timeout=5, check=False
    )
    if result.returncode == 0:
        return result.stdout.strip()
    if "no crontab" in result.stderr.lower():
        return ""
    return f"Unable to read crontab: {result.stderr.strip()}"


def _cron_jobs(crontab: str) -> list[dict[str, str]]:
    """Extract repo-managed job blocks and their schedule lines."""
    jobs: list[dict[str, str]] = []
    current_name: str | None = None
    for raw_line in crontab.splitlines():
        line = raw_line.strip()
        if line.startswith("# >>> ug-ai-analyst:") and line.endswith(" >>>"):
            current_name = line.removeprefix("# >>> ug-ai-analyst:").removesuffix(" >>>")
            continue
        if line.startswith("# <<< ug-ai-analyst:"):
            current_name = None
            continue
        if current_name is None or not line or line.startswith("#") or "=" in line.split()[0]:
            continue
        fields = line.split(maxsplit=5)
        if len(fields) == 6:
            jobs.append(
                {"name": current_name, "schedule": " ".join(fields[:5]), "command": fields[5]}
            )
    return jobs


def _cron_history() -> list[dict[str, str]]:
    result = subprocess.run(
        [
            "journalctl", "--no-pager", "-o", "json", "-t", "CRON",
            "--since", "7 days ago", "-n", "200",
        ],
        capture_output=True, text=True, timeout=10, check=False,
    )
    rows: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = str(item.get("MESSAGE", ""))
        if not message.startswith("(egor) CMD ("):
            continue
        micros = int(item.get("__REALTIME_TIMESTAMP", 0))
        rows.append(
            {
                "time": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(micros / 1e6)),
                "command": message.split(" CMD (", 1)[1].removesuffix(")"),
            }
        )
    return list(reversed(rows))


def create_admin_routes(
    app: web.Application,
    store: EventStore,
    orchestrator: AgentOrchestrator,
    slack_client: AsyncWebClient,
) -> None:
    """Attach loopback admin routes to the existing HTTP application."""
    csrf = secrets.token_urlsafe(32)

    def require_local(request: web.Request) -> None:
        if request.remote not in {"127.0.0.1", "::1"}:
            raise web.HTTPForbidden(text="Admin UI is loopback-only")

    async def page(request: web.Request) -> web.Response:
        require_local(request)
        return web.Response(
            text=ADMIN_HTML.replace("__CSRF__", escape(csrf)),
            content_type="text/html",
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": (
                    "default-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"
                ),
                "Referrer-Policy": "no-referrer",
                "X-Frame-Options": "DENY",
            },
        )

    async def state(request: web.Request) -> web.Response:
        require_local(request)
        sessions = await asyncio.to_thread(store.list_agent_sessions)
        active = orchestrator.active_keys()
        for session in sessions:
            key = (session["channel_id"], session["thread_ts"])
            session["active"] = key in active
            session["runtime_status"] = "running" if key in active else session["status"]
            session["messages"] = await asyncio.to_thread(
                store.thread_messages, *key
            )
        crontab, history = await asyncio.gather(
            asyncio.to_thread(_crontab), asyncio.to_thread(_cron_history)
        )
        return web.json_response(
            {
                "sessions": sessions,
                "crontab": crontab,
                "cron_jobs": _cron_jobs(crontab),
                "cron_history": history,
            }
        )

    def require_csrf(request: web.Request) -> None:
        require_local(request)
        if not secrets.compare_digest(request.headers.get("X-Admin-CSRF", ""), csrf):
            raise web.HTTPForbidden(text="Invalid CSRF token")

    async def stop(request: web.Request) -> web.Response:
        require_csrf(request)
        stopped = await orchestrator.cancel(
            request.match_info["channel"], request.match_info["thread"]
        )
        return web.json_response({"stopped": stopped})

    async def close(request: web.Request) -> web.Response:
        require_csrf(request)
        key = (request.match_info["channel"], request.match_info["thread"])
        await orchestrator.cancel(*key)
        closed = await asyncio.to_thread(store.close_agent_session, *key)
        return web.json_response({"closed": closed})

    async def message(request: web.Request) -> web.Response:
        require_csrf(request)
        body = await request.json()
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise web.HTTPBadRequest(text="Message text is required")
        channel, thread = request.match_info["channel"], request.match_info["thread"]
        result = await orchestrator.submit(
            slack_client,
            channel_id=channel,
            message_ts=f"admin:{time.time_ns()}",
            thread_ts=thread,
            text=text.strip(),
            show_status=False,
        )
        if result is SubmitResult.EXPIRED:
            raise web.HTTPConflict(text="Session is closed or expired")
        await asyncio.to_thread(store.record_admin_agent_message, channel, thread, text.strip())
        return web.json_response({"result": result.value})

    app.router.add_get("/admin", page)
    app.router.add_get("/admin/api/state", state)
    app.router.add_post("/admin/api/sessions/{channel}/{thread}/stop", stop)
    app.router.add_post("/admin/api/sessions/{channel}/{thread}/close", close)
    app.router.add_post("/admin/api/sessions/{channel}/{thread}/message", message)
