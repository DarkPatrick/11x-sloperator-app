"""Loopback-only operational UI for cron and agent sessions."""

# ruff: noqa: E501

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import secrets
import shlex
import subprocess
import time
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from aiohttp import web
from slack_sdk.web.async_client import AsyncWebClient

from sloperator.agents import AgentOrchestrator, SubmitResult
from sloperator.config import Settings
from sloperator.store import EventStore

ADMIN_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Sloperator admin</title>
<style>
:root{color-scheme:dark;--bg:#111318;--card:#1b1f27;--surface:#12151b;--text:#eef2f7;
--muted:#9aa4b2;--line:#303744;--button:#252b35;--green:#47d18c;--red:#ff6b6b;
--blue:#6ea8fe}html[data-theme="light"]{color-scheme:light;--bg:#f5f7fa;--card:#fff;
--surface:#f0f3f7;--text:#17202b;--muted:#667085;--line:#d9dee7;--button:#eef1f5;
--green:#08783f;--red:#b42318;--blue:#3976d3}*{box-sizing:border-box}body{margin:0;
background:var(--bg);color:var(--text);font:14px/1.45 system-ui,sans-serif}
main{max-width:1180px;margin:auto;padding:28px}
h1{margin:0 0 4px;font-size:28px}h2{margin:30px 0 12px}.sub{color:var(--muted)}
.grid{display:grid;gap:12px}.card{background:var(--card);border:1px solid var(--line);
border-radius:12px;padding:16px}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.spread{justify-content:space-between}.badge{padding:3px 8px;border-radius:99px;
background:var(--surface);color:var(--text);border:1px solid var(--line)}
.running{background:#174b35;color:#8cf0be}.failed{background:#552526;color:#ffaaaa}
html[data-theme="light"] .running{background:#d7f4e5;color:#08783f}
html[data-theme="light"] .failed{background:#fee4e2;color:#b42318}
.tabs{display:flex;gap:8px;margin:24px 0}.tabs button{font-weight:650;padding:9px 16px}
.tabs button.active{background:var(--blue);border-color:var(--blue);color:#0c1a2d}
.panel{display:none}.panel.active{display:block}
button{border:1px solid var(--line);background:var(--button);color:var(--text);border-radius:7px;
padding:7px 11px;cursor:pointer}button:hover{border-color:var(--blue)}button.danger{color:var(--red)}
textarea{width:100%;min-height:72px;background:var(--surface);color:var(--text);border:1px solid var(--line);
border-radius:8px;padding:10px}pre{white-space:pre-wrap;word-break:break-word;background:var(--surface);
padding:10px;border-radius:8px;
max-height:320px;overflow:auto}.messages{max-height:300px;overflow:auto;margin:10px 0}.msg{border-left:2px
solid var(--line);padding:5px 9px;margin:4px 0}.meta{font-size:12px;color:var(--muted)}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:8px;border-bottom:1px solid var(--line)}
.cron-toolbar{margin-bottom:12px}.cron-legend{display:flex;gap:14px;flex-wrap:wrap;color:var(--muted);
font-size:12px}.legend-item{display:flex;align-items:center;gap:6px}.run-dot{width:10px;height:10px;
border-radius:3px;background:var(--blue);box-shadow:inset 0 0 0 1px #ffffff24}.run-dot.success,
.run-segment.completed{background:var(--green)}.run-dot.running,.run-segment.running{background:#f5a524}
.run-segment.launched{background:var(--blue)}.run-dot.scheduled,.run-segment.scheduled{background:#697386}
.run-dot.failed,.run-segment.failed{background:var(--red)}.run-dot.missed,.run-segment.missed{
background:var(--surface);border:1px solid var(--line)}
.cron-board{padding:0;overflow:hidden}.cron-board-head{padding:13px 16px;border-bottom:1px solid var(--line)}
.cron-scroll{overflow-x:auto}.cron-grid{display:grid;grid-template-columns:220px repeat(28,24px);
column-gap:5px;row-gap:0;min-width:1048px;padding:12px 16px 16px;align-items:center}
.cron-grid-head{display:contents}.cron-axis-label{font-size:11px;color:var(--muted);padding-bottom:7px}
.cron-axis-day{text-align:center;font-size:9px;color:var(--muted);padding-bottom:7px;line-height:1.1}
.cron-axis-day.week-start{color:var(--text)}.cron-job-label{min-width:0;padding:10px 14px 10px 0;
border-top:1px solid var(--line)}.cron-job-label b{display:block;white-space:nowrap;overflow:hidden;
text-overflow:ellipsis}.cron-job-stats{font-size:11px;color:var(--muted);margin-top:2px}
.cron-day-slot{height:38px;border-top:1px solid var(--line);display:flex;align-items:center;justify-content:center}
.cron-day{position:relative;width:22px;height:22px;border-radius:5px;background:var(--surface);
border:1px solid var(--line);transition:transform .12s,border-color .12s;display:grid;
grid-template-columns:repeat(var(--segment-cols,1),1fr);grid-auto-rows:1fr;gap:1px;padding:2px;
overflow:hidden}.cron-day.has-runs{border-color:#ffffff20}
.cron-day:hover{transform:scale(1.18);z-index:2;border-color:var(--text)}.cron-day.today{
outline:2px solid var(--blue);outline-offset:2px}.cron-day.future{opacity:.32}
.run-segment{min-width:1px;min-height:1px;border-radius:1px}
.cron-empty-board{padding:18px;color:var(--muted)}.cron-config,.history-log{margin-top:14px}
.cron-config .table-wrap,.history-log .table-wrap{overflow:auto}summary{cursor:pointer}
@media(max-width:700px){main{padding:18px}.cron-grid{grid-template-columns:170px repeat(28,24px);
min-width:998px}.cron-job-label{position:sticky;left:0;background:var(--card);z-index:3}}
</style></head><body><main><div class="row spread"><div><h1>Sloperator</h1>
<div class="sub">localhost admin · access via SSH tunnel</div></div>
<button id="theme-toggle" onclick="toggleTheme()" aria-label="Переключить тему"></button></div>
<nav class="tabs" aria-label="Admin sections">
<button id="tab-agents" onclick="setTab('agents')">Агенты</button>
<button id="tab-cron" onclick="setTab('cron')">Cron</button>
</nav>
<section id="panel-agents" class="panel"><h2>Agent sessions</h2>
<div id="sessions" class="grid"></div></section>
<section id="panel-cron" class="panel"><h2>Cron runs</h2><div class="cron-toolbar row spread">
<span class="sub">Last 28 days · UTC</span><div class="cron-legend">
<span class="legend-item"><i class="run-dot success"></i>Completed</span>
<span class="legend-item"><i class="run-dot running"></i>Running</span>
<span class="legend-item"><i class="run-dot failed"></i>Failed</span>
<span class="legend-item"><i class="run-dot scheduled"></i>Scheduled</span>
<span class="legend-item"><i class="run-dot missed"></i>No record</span>
</div></div><div id="history"></div><details class="card cron-config"><summary>Schedules and commands</summary>
<div id="cron"></div></details></section></main>
<script>
const csrf="__CSRF__"; const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",
">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
function preferredTheme(){const saved=localStorage.getItem("sloperator-theme");
return saved|| (matchMedia("(prefers-color-scheme: light)").matches?"light":"dark")}
function applyTheme(theme){document.documentElement.dataset.theme=theme;
document.getElementById("theme-toggle").textContent=theme==="light"?"Тёмная тема":"Светлая тема"}
function toggleTheme(){const next=document.documentElement.dataset.theme==="light"?"dark":"light";
localStorage.setItem("sloperator-theme",next);applyTheme(next)}
function setTab(tab){if(!["agents","cron"].includes(tab))tab="agents";
for(const name of ["agents","cron"]){document.getElementById("panel-"+name).classList.toggle("active",name===tab);
document.getElementById("tab-"+name).classList.toggle("active",name===tab)}
if(location.hash!=="#"+tab)history.replaceState(null,"","#"+tab)}
async function api(path,opts={}){opts.headers={...(opts.headers||{}),"X-Admin-CSRF":csrf};
const r=await fetch("/admin/api"+path,opts);if(!r.ok)throw new Error(await r.text());return r.json()}
async function action(path,body){await api(path,{method:"POST",headers:{"Content-Type":"application/json"},
body:JSON.stringify(body||{})});await load()}
function sessionCard(s){const k=encodeURIComponent(s.channel_id)+"/"+encodeURIComponent(s.thread_ts);
const msgs=(s.messages||[]).map(m=>`<div class="msg"><div class="meta">${esc(m.message_ts)} ·
${esc(m.user_id||m.bot_id||"unknown")}</div>${esc(m.text)}</div>`).join("");
return `<section class="card" data-session="${esc(k)}"><div class="row spread"><div><b>${esc(s.channel_name)}</b>
<span class="meta">${esc(s.channel_id)} / ${esc(s.thread_ts)}</span></div><span class="badge
${esc(s.runtime_status)}">${esc(s.runtime_status)}</span></div><div class="meta">${esc(s.provider)}:
${esc(s.model)} · turns ${s.turn_count} · updated ${esc(s.updated_at)}
${s.process_id?` · PID ${esc(s.process_id)} + subprocess tree`:""}</div>
${s.last_error?`<pre>${esc(s.last_error)}</pre>`:""}${s.headless?"":`<details><summary>Thread messages</summary>
<div class="messages">${msgs||'<span class="sub">No archived messages</span>'}</div></details>
<textarea id="m-${esc(s.thread_ts)}" placeholder="Message or steer this agent"></textarea>
`}<div class="row">${s.headless?"":`<button onclick="send('${k}','m-${esc(s.thread_ts)}')">Send</button>`}
<button onclick="action('/sessions/${k}/stop')" ${s.active?"":"disabled"}>Stop process</button>
<button class="danger" onclick="closeSession('${k}')">Close session</button></div></section>`}
async function send(k,id){const el=document.getElementById(id);if(!el.value.trim())return;
await action("/sessions/"+k+"/message",{text:el.value.trim()})}
async function closeSession(k){if(confirm("Permanently close this session?"))await action("/sessions/"+k+"/close")}
let sessionsSignature="";
function renderSessions(sessions){const root=document.getElementById("sessions");const previous=new Map();
for(const card of root.querySelectorAll("[data-session]")){const details=card.querySelector("details");
const messages=card.querySelector(".messages");const draft=card.querySelector("textarea");
previous.set(card.dataset.session,{open:details?.open,scroll:messages?.scrollTop||0,draft:draft?.value||""})}
root.innerHTML=sessions.map(sessionCard).join("")||'<div class="card sub">No sessions</div>';
for(const card of root.querySelectorAll("[data-session]")){const state=previous.get(card.dataset.session);
if(!state)continue;const details=card.querySelector("details");const messages=card.querySelector(".messages");
const draft=card.querySelector("textarea");if(details)details.open=state.open;if(messages)messages.scrollTop=state.scroll;
if(draft)draft.value=state.draft}}
let cronSignature="";
function utcDate(value){return new Date(value.replace(" UTC","Z").replace(" ","T"))}
function calendarDays(){const today=new Date();const end=new Date(Date.UTC(today.getUTCFullYear(),
today.getUTCMonth(),today.getUTCDate()));return Array.from({length:28},(_,index)=>{
const date=new Date(end);date.setUTCDate(end.getUTCDate()-27+index);return date})}
function dayKey(date){return date.toISOString().slice(0,10)}
function statusLabel(status){return {completed:"completed",started:"running",launched:"launched",
scheduled:"scheduled"}[status]||status}
function cronFieldValues(field,min,max){const values=new Set();for(const part of field.split(",")){
const [base,stepRaw]=part.split("/"),step=Math.max(1,Number(stepRaw)||1);let start=min,end=max;
if(base!=="*"){if(base.includes("-"))[start,end]=base.split("-").map(Number);else start=end=Number(base)}
for(let value=start;value<=end;value+=step)if(value>=min&&value<=max)values.add(value)}return values}
function plannedRuns(job,date){if(job.schedule.startsWith("weekdays "))return date.getUTCDay()>=1&&
date.getUTCDay()<=5?1:0;const fields=job.schedule.trim().split(/\\s+/);if(fields.length!==5)return 0;
const [, ,dom,month,dow]=fields;if(!cronFieldValues(month,1,12).has(date.getUTCMonth()+1))return 0;
if(dom!=="*"&&!cronFieldValues(dom,1,31).has(date.getUTCDate()))return 0;
if(dow!=="*"&&!cronFieldValues(dow,0,7).has(date.getUTCDay())&&
!(date.getUTCDay()===0&&cronFieldValues(dow,0,7).has(7)))return 0;
if(job.command.includes("--only-at"))return 1;
return cronFieldValues(fields[0],0,59).size*cronFieldValues(fields[1],0,23).size}
function cronRow(job,events,days,today){const firstEvent=events.length?
[...events].sort((a,b)=>a.time.localeCompare(b.time))[0].time.slice(0,10):today;
const cells=days.map(date=>{const key=dayKey(date);
const runs=events.filter(event=>dayKey(utcDate(event.time))===key).sort((a,b)=>a.time.localeCompare(b.time));
const planned=key>=firstEvent?plannedRuns(job,date):0;const displayedRuns=planned===1&&runs.length?
[runs[runs.length-1]]:runs.slice(0,planned);
const segments=Array.from({length:planned},(_,index)=>{const status=index<displayedRuns.length?
statusLabel(displayedRuns[index].status):(key===today?"scheduled":"missed");
return `<i class="run-segment ${esc(status)}" title="${esc(index<displayedRuns.length?
displayedRuns[index].time+" — "+status:"planned — "+status)}"></i>`}).join("");
const details=`${key} · planned ${planned} · recorded ${runs.length}`+
(runs.length?"\\n"+runs.map(event=>`${event.time} — ${statusLabel(event.status)}`).join("\\n"):"");
const cols=Math.max(1,Math.ceil(Math.sqrt(planned)));return `<div class="cron-day-slot"><div
class="cron-day ${runs.length?"has-runs":""} ${key===today?"today":""}"
style="--segment-cols:${cols}" title="${esc(details)}" aria-label="${esc(details)}">${segments}</div></div>`}).join("");
const completed=events.filter(event=>statusLabel(event.status)==="completed").length;
return `<div class="cron-job-label" title="${esc(job.name)} · ${esc(job.schedule)}"><b>${esc(job.name)}</b>
<div class="cron-job-stats">${esc(job.schedule)} · ${events.length} events${completed?` · ${completed} done`:""}</div>
</div>${cells}`}
function renderCronHistory(jobs,events){const root=document.getElementById("history");
const days=calendarDays(),today=dayKey(days[days.length-1]);const axis=days.map(date=>{
const monday=date.getUTCDay()===1;return `<div class="cron-axis-day ${monday?"week-start":""}"
title="${dayKey(date)}">${monday?date.toLocaleString("en",{month:"short",day:"numeric",timeZone:"UTC"}):
date.getUTCDate()}</div>`}).join("");const gridRows=jobs.map(job=>
cronRow(job,events.filter(event=>event.job===job.name),days,today)).join("");
const eventRows=events.map(event=>`<tr><td>${esc(event.time)}</td><td>${esc(event.job||"unknown")}</td>
<td><span class="badge ${esc(statusLabel(event.status))}">${esc(statusLabel(event.status))}</span></td>
<td><code>${esc(event.command)}</code></td></tr>`).join("");
root.innerHTML=jobs.length?`<section class="card cron-board"><div class="cron-board-head row spread">
<b>Run calendar</b><span class="badge">${jobs.length} jobs</span></div><div class="cron-scroll">
<div class="cron-grid"><div class="cron-axis-label">Job / schedule</div>${axis}${gridRows}</div></div></section>`:
'<div class="card cron-empty-board">No scheduled jobs</div>';
root.innerHTML+=`<details class="card history-log"><summary>Event log</summary><div class="table-wrap"><table><thead>
<tr><th>Time</th><th>Job</th><th>Status</th><th>Command</th></tr></thead><tbody>${eventRows||
'<tr><td colspan="4" class="sub">No events in the last 28 days</td></tr>'}</tbody></table></div></details>`}
async function load(){const d=await api("/state");const signature=JSON.stringify(d.sessions);
if(signature!==sessionsSignature){renderSessions(d.sessions);sessionsSignature=signature}
const nextCronSignature=JSON.stringify([d.cron_jobs,d.cron_history,d.crontab]);
if(nextCronSignature!==cronSignature){const config=document.querySelector(".cron-config");
const history=document.querySelector(".history-log");const scroll=document.querySelector(".cron-scroll");
const ui={configOpen:config?.open||false,historyOpen:history?.open||false,scrollLeft:scroll?.scrollLeft||0};
document.getElementById("cron").innerHTML=d.cron_jobs.length?`<table><thead><tr><th>Job</th>
<th>Schedule</th><th>Command</th></tr></thead><tbody>${d.cron_jobs.map(x=>`<tr><td>${esc(x.name)}
</td><td><code>${esc(x.schedule)}</code></td><td><code>${esc(x.command)}</code></td></tr>`).join("")}
</tbody></table><details><summary>Raw crontab</summary><pre>${esc(d.crontab)}</pre></details>`:
'<span class="sub">No user crontab</span>';
renderCronHistory(d.cron_jobs,d.cron_history);document.querySelector(".cron-config").open=ui.configOpen;
const nextHistory=document.querySelector(".history-log");if(nextHistory)nextHistory.open=ui.historyOpen;
const nextScroll=document.querySelector(".cron-scroll");if(nextScroll)nextScroll.scrollLeft=ui.scrollLeft;
cronSignature=nextCronSignature}}
applyTheme(preferredTheme());setTab(location.hash.slice(1));
addEventListener("hashchange",()=>setTab(location.hash.slice(1)));
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
            "journalctl",
            "--no-pager",
            "-o",
            "json",
            "-t",
            "CRON",
            "--since",
            "28 days ago",
            "--grep=^\\(egor\\) CMD \\(",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
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


def _label_cron_history(
    jobs: list[dict[str, str]],
    rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Attach configured job names and launch status to CRON journal events."""
    labelled: list[dict[str, str]] = []
    for row in rows:
        command = row["command"]
        job = next(
            (
                item["name"]
                for item in jobs
                if command == item["command"] or item["command"] in command
            ),
            command.split()[0] if command else "unknown",
        )
        labelled.append({**row, "job": job, "status": "launched"})
    return labelled


_RETRY_RESULT = re.compile(
    r"^\[(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] "
    r"\[cron_retry:[^\]]+\] child exited rc=(?P<rc>\d+)$"
)


def _utc_string(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _cron_execution_history(
    jobs: list[dict[str, str]],
) -> tuple[list[dict[str, str]], set[str]]:
    """Read actual child outcomes from each managed job's own execution logs.

    The system CRON journal records scheduler invocations, including the second
    DST-candidate invocation rejected by ``--only-at``.  ``cron_retry`` logs the
    child exit exactly once per real attempt.  Non-wrapper jobs use the shared
    ``scripts/logs/<script-stem>.jsonl`` convention.
    """
    rows: list[dict[str, str]] = []
    authoritative: set[str] = set()
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=28)

    for job in jobs:
        command = job["command"]
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = command.split()

        timezone_match = re.search(r"(?:^|\s)TZ=([A-Za-z0-9_+./-]+)", command)
        timezone = ZoneInfo(timezone_match.group(1)) if timezone_match else dt.UTC
        if "--log-out" in tokens:
            authoritative.add(job["name"])
            try:
                log_path = Path(tokens[tokens.index("--log-out") + 1])
                lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            except (IndexError, OSError):
                lines = []
            for line in lines:
                match = _RETRY_RESULT.match(line)
                if not match:
                    continue
                timestamp = dt.datetime.strptime(
                    match.group("time"), "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=timezone)
                if timestamp.astimezone(dt.UTC) < cutoff:
                    continue
                rc = int(match.group("rc"))
                rows.append(
                    {
                        "time": _utc_string(timestamp),
                        "command": f"execution log · child rc={rc}",
                        "job": job["name"],
                        "status": "completed" if rc == 0 else "failed",
                    }
                )
            continue

        script_paths = [
            Path(token)
            for token in tokens
            if token.endswith(".py") and Path(token).name != "cron_retry.py"
        ]
        if not script_paths:
            continue
        script_path = script_paths[-1]
        jsonl_path = script_path.parent / "logs" / f"{script_path.stem}.jsonl"
        if not jsonl_path.is_file():
            continue
        authoritative.add(job["name"])
        try:
            lines = jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                item = json.loads(line)
                timestamp = dt.datetime.fromisoformat(str(item["ts"]).replace("Z", "+00:00"))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if timestamp.astimezone(dt.UTC) < cutoff:
                continue
            status = str(item.get("status", "")).lower()
            rows.append(
                {
                    "time": _utc_string(timestamp),
                    "command": f"execution log · status={status or 'unknown'}",
                    "job": job["name"],
                    "status": "completed" if status in {"ok", "completed", "success"} else "failed",
                }
            )

    rows.sort(key=lambda row: row["time"], reverse=True)
    return rows, authoritative


def _systemd_scheduler_job(settings: Settings) -> dict[str, str]:
    """Describe the experiment scheduler embedded in sloperator.service."""
    result = subprocess.run(
        [
            "systemctl",
            "show",
            "sloperator",
            "--property=ActiveState,MainPID",
            "--no-pager",
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    state = properties.get("ActiveState", "unknown")
    pid = properties.get("MainPID", "unknown")
    return {
        "name": "experiment-finalizer (sloperator.service)",
        "schedule": (
            f"weekdays Mon-Fri {settings.experiment_finalizer_hour:02d}:00 "
            f"{settings.experiment_finalizer_timezone}"
        ),
        "command": f"embedded asyncio scheduler · {state} · PID {pid}",
    }


def _systemd_scheduler_history() -> list[dict[str, str]]:
    """Return recent schedule/start/completion events from the service journal."""
    result = subprocess.run(
        [
            "journalctl",
            "--no-pager",
            "-o",
            "json",
            "-u",
            "sloperator",
            "--since",
            "28 days ago",
            "-n",
            "300",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    prefixes = {
        "Next experiment finalizer run scheduled for ": ("scheduled: ", "scheduled"),
        "Starting scheduled experiment finalizer run": ("started", "started"),
        "Experiment finalizer run completed": ("completed", "completed"),
    }
    rows: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = str(item.get("MESSAGE", ""))
        event: str | None = None
        status: str | None = None
        for prefix, (label, event_status) in prefixes.items():
            if message.endswith(prefix.rstrip()):
                event = label
                status = event_status
                break
            if prefix in message:
                event = f"{label}{message.split(prefix, 1)[1]}"
                status = event_status
                break
        if event is None or status is None:
            continue
        micros = int(item.get("__REALTIME_TIMESTAMP", 0))
        rows.append(
            {
                "time": time.strftime(
                    "%Y-%m-%d %H:%M:%S UTC",
                    time.gmtime(micros / 1e6),
                ),
                "command": f"sloperator.service · experiment-finalizer · {event}",
                "job": "experiment-finalizer (sloperator.service)",
                "status": status,
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
        sessions = [
            *orchestrator.headless_sessions(),
            *await asyncio.to_thread(store.list_agent_sessions),
        ]
        active = orchestrator.active_keys()
        for session in sessions:
            key = (session["channel_id"], session["thread_ts"])
            session["active"] = key in active
            session["runtime_status"] = "running" if key in active else session["status"]
            if not session.get("headless"):
                session["messages"] = await asyncio.to_thread(store.thread_messages, *key)
        sessions.sort(key=lambda session: session["updated_at"], reverse=True)
        crontab, history, service_job, service_history = await asyncio.gather(
            asyncio.to_thread(_crontab),
            asyncio.to_thread(_cron_history),
            asyncio.to_thread(_systemd_scheduler_job, orchestrator.settings),
            asyncio.to_thread(_systemd_scheduler_history),
        )
        cron_jobs = _cron_jobs(crontab)
        execution_history, authoritative_jobs = await asyncio.to_thread(
            _cron_execution_history, cron_jobs
        )
        journal_history = [
            row
            for row in _label_cron_history(cron_jobs, history)
            if row["job"] not in authoritative_jobs
        ]
        return web.json_response(
            {
                "sessions": sessions,
                "crontab": crontab,
                "cron_jobs": [service_job, *cron_jobs],
                "cron_history": sorted(
                    [*execution_history, *journal_history, *service_history],
                    key=lambda row: row["time"],
                    reverse=True,
                ),
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
        closed = orchestrator.dismiss_headless(*key)
        if not closed:
            closed = await asyncio.to_thread(store.close_agent_session, *key)
        return web.json_response({"closed": closed})

    async def message(request: web.Request) -> web.Response:
        require_csrf(request)
        body = await request.json()
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise web.HTTPBadRequest(text="Message text is required")
        channel, thread = request.match_info["channel"], request.match_info["thread"]
        if channel == "scheduled":
            raise web.HTTPConflict(text="Headless runs do not accept messages")
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
