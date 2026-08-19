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
from typing import Any
from zoneinfo import ZoneInfo

from aiohttp import web
from slack_sdk.web.async_client import AsyncWebClient

from sloperator.admin_codex import AdminCodexManager
from sloperator.admin_sql import AdminSqlManager
from sloperator.agents import AgentOrchestrator, SubmitResult
from sloperator.anomaly_alerts import Alert, AlertBatch, build_monetisation_agent_prompt
from sloperator.automation_controls import AutomationControls
from sloperator.codex_app_server import CodexAppServerError
from sloperator.config import Settings
from sloperator.mobile_health import MobileCriticalMetric, build_mobile_health_agent_prompt
from sloperator.payment_layer import build_payment_layer_agent_prompt
from sloperator.store import EventStore
from sloperator.subscription_flow import (
    SubscriptionFlowIncident,
    build_subscription_flow_agent_prompt,
)

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
.run-segment{min-width:1px;min-height:1px;border-radius:1px;background:var(--blue)}
.cron-empty-board{padding:18px;color:var(--muted)}.cron-config,.history-log{margin-top:14px}
.cron-config .table-wrap,.history-log .table-wrap{overflow:auto}summary{cursor:pointer}
.trigger-configs{grid-template-columns:repeat(auto-fit,minmax(260px,1fr));margin-bottom:14px}
.trigger-config{cursor:pointer;transition:border-color .12s,transform .12s}.trigger-config:hover{
border-color:var(--blue);transform:translateY(-1px)}.trigger-config h3{margin:0 0 7px}
.trigger-condition{font-size:12px;color:var(--muted);line-height:1.5}
.trigger-links{display:flex;gap:10px;flex-wrap:wrap}.trigger-links a{color:var(--blue);cursor:pointer}
.modal-backdrop{position:fixed;inset:0;z-index:20;background:#0009;display:flex;align-items:center;
justify-content:center;padding:24px}.modal-backdrop[hidden]{display:none}.prompt-modal{width:min(920px,100%);
max-height:min(860px,92vh);display:flex;flex-direction:column;padding:0;box-shadow:0 24px 80px #0008}
.prompt-modal-head{padding:14px 18px;border-bottom:1px solid var(--line)}.prompt-modal-head h2{
margin:0}.prompt-modal-body{padding:18px;overflow:auto}.prompt-markdown{font-size:14px;line-height:1.6}
.prompt-markdown p{margin:0 0 14px}.prompt-markdown ul{margin:0 0 14px;padding-left:24px}
.prompt-markdown pre{max-height:none}.prompt-markdown code{background:var(--surface);padding:2px 5px;
border-radius:4px}.prompt-markdown blockquote{margin:0 0 16px;padding:10px 14px;border-left:3px solid
var(--blue);background:var(--surface);color:var(--muted)}
.codex-shell{display:grid;grid-template-columns:280px minmax(0,1fr);height:min(720px,calc(100vh - 190px));
min-height:480px;padding:0;overflow:hidden}.codex-sidebar{border-right:1px solid var(--line);display:flex;
flex-direction:column;min-height:0}.codex-sidebar-head{padding:12px;border-bottom:1px solid var(--line)}
.codex-session-list{overflow:auto;padding:8px}.codex-session{display:block;width:100%;text-align:left;
padding:10px;margin-bottom:6px}.codex-session.active{border-color:var(--blue);background:var(--surface)}
.codex-session-title{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:650}
.codex-chat{display:flex;flex-direction:column;min-width:0;min-height:0}.codex-chat-head{padding:12px 14px;
border-bottom:1px solid var(--line)}.codex-messages{flex:1;overflow:auto;padding:16px}.codex-msg{
max-width:88%;padding:10px 12px;border-radius:10px;margin:0 0 10px;white-space:pre-wrap;
word-break:break-word}.codex-msg.user{margin-left:auto;background:#17355f}.codex-msg.assistant{
background:var(--surface);border:1px solid var(--line)}html[data-theme="light"] .codex-msg.user{
background:#dbeafe}.codex-compose{padding:12px;border-top:1px solid var(--line)}
.codex-compose textarea{min-height:74px}.codex-empty{margin:auto;color:var(--muted);text-align:center}
.sql-toolbar{margin-bottom:10px}.sql-workbench{display:grid;grid-template-columns:1fr 1fr;
height:min(720px,calc(100vh - 235px));min-height:480px;padding:0;overflow:hidden}.sql-pane{
display:flex;flex-direction:column;min-width:0;min-height:0}.sql-pane:first-child{
border-right:1px solid var(--line)}.sql-pane-head{padding:10px 12px;border-bottom:1px solid var(--line)}
.sql-code{position:relative;flex:1;min-height:0;background:var(--surface);overflow:hidden}
.sql-highlight,.sql-editor{position:absolute;inset:0;margin:0;border:0;border-radius:0;padding:16px;
font:13px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace;tab-size:2;white-space:pre-wrap;
overflow-wrap:normal}.sql-highlight{pointer-events:none;overflow:hidden;max-height:none;color:var(--text)}
.sql-editor{resize:none;outline:none;background:transparent;color:transparent;caret-color:var(--text);
-webkit-text-fill-color:transparent;overflow:auto}.sql-editor::placeholder{-webkit-text-fill-color:var(--muted);
color:var(--muted)}.sql-editor::selection{background:#6ea8fe55}.sql-code:focus-within{
box-shadow:inset 0 0 0 1px var(--blue)}.sql-output-wrap{background:var(--surface)}
.sql-kw{color:#c792ea;font-weight:650}.sql-fn{color:#82aaff}.sql-str{color:#c3e88d}
.sql-num{color:#f78c6c}.sql-comment{color:#7f8c98;font-style:italic}.sql-param{color:#ffcb6b}
html[data-theme="light"] .sql-kw{color:#7c3aed}html[data-theme="light"] .sql-fn{color:#005cc5}
html[data-theme="light"] .sql-str{color:#22863a}html[data-theme="light"] .sql-num{color:#b31d28}
html[data-theme="light"] .sql-comment{color:#6a737d}html[data-theme="light"] .sql-param{color:#9a6700}
.sql-status.busy{color:#f5a524}.sql-status.error{color:var(--red)}
.sql-actions{margin-left:auto}.sql-results{margin-top:14px}.sql-results-head{margin-bottom:10px}
.sql-table-wrap{max-height:480px;overflow:auto;border:1px solid var(--line);border-radius:8px}
.sql-table-wrap table{min-width:max-content;background:var(--card)}.sql-table-wrap th{position:sticky;
top:0;background:var(--button);z-index:1}.sql-table-wrap td{max-width:420px;white-space:pre-wrap;
word-break:break-word}.sql-empty{padding:28px;text-align:center;color:var(--muted)}
.sql-viz{margin-top:14px}.sql-viz summary{padding:12px 0;font-weight:650}.sql-viz-frame{
display:block;width:100%;height:620px;border:1px solid var(--line);border-radius:8px;background:#fff}
@media(max-width:700px){main{padding:18px}.cron-grid{grid-template-columns:170px repeat(28,24px);
min-width:998px}.cron-job-label{position:sticky;left:0;background:var(--card);z-index:3}
.codex-shell{grid-template-columns:130px minmax(0,1fr)}.sql-workbench{grid-template-columns:1fr;
height:auto}.sql-pane:first-child{border-right:0;border-bottom:1px solid var(--line)}
.sql-editor{min-height:42vh}}
</style></head><body><main><div class="row spread"><div><h1>Sloperator</h1>
<div class="sub">localhost admin · access via SSH tunnel</div></div>
<button id="theme-toggle" onclick="toggleTheme()" aria-label="Переключить тему"></button></div>
<nav class="tabs" aria-label="Admin sections">
<button id="tab-agents" onclick="setTab('agents')">Агенты</button>
<button id="tab-cron" onclick="setTab('cron')">Cron</button>
<button id="tab-triggers" onclick="setTab('triggers')">Slack-триггеры</button>
<button id="tab-codex" onclick="setTab('codex')">Codex</button>
<button id="tab-sql" onclick="setTab('sql')">SQL editor</button>
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
<div id="cron"></div></details></section>
<section id="panel-triggers" class="panel"><h2>Slack triggers</h2>
<div class="sub cron-toolbar">Configured event-driven launches · last 28 days · UTC</div>
<div id="trigger-configs" class="grid trigger-configs"></div>
<div id="trigger-history"></div></section>
<section id="panel-codex" class="panel"><h2>Codex sessions</h2><div class="card codex-shell">
<aside class="codex-sidebar"><div class="codex-sidebar-head"><button onclick="newCodexSession()">
+ Новая</button></div><div id="codex-sessions" class="codex-session-list"></div></aside>
<div id="codex-chat" class="codex-chat"><div class="codex-empty">Выберите или создайте сессию</div>
</div></div></section>
<section id="panel-sql" class="panel"><h2>SQL editor</h2>
<div class="row spread sql-toolbar"><div class="row"><label for="sql-provider">Агент</label>
<select id="sql-provider" onchange="changeSqlProvider()"><option value="claude">Claude</option>
<option value="codex">Codex</option></select></div>
<div class="row sql-actions"><button id="sql-run" onclick="executeSql()">▶ Выполнить</button>
<button id="sql-visualize" onclick="visualizeSql()" disabled>Визуализация</button></div>
<span id="sql-status" class="meta sql-status">Напишите SQL — подсказка появится после паузы</span></div>
<div class="card sql-workbench"><section class="sql-pane"><div class="sql-pane-head row spread">
<b>Ваш SQL</b><span class="meta">автосохранение · пауза 7 сек.</span></div>
<div class="sql-code"><pre id="sql-input-highlight" class="sql-highlight" aria-hidden="true"></pre>
<textarea id="sql-input" class="sql-editor" spellcheck="false" placeholder="-- Начните писать запрос…"></textarea></div>
</section><section class="sql-pane"><div class="sql-pane-head row spread"><b>Продолжение агента</b>
<button onclick="copySqlSuggestion()">Копировать</button></div>
<div class="sql-code sql-output-wrap"><pre id="sql-output-highlight" class="sql-highlight"
aria-hidden="true"></pre><textarea id="sql-output" class="sql-editor" readonly spellcheck="false"
placeholder="Здесь появится готовый SQL для копирования"></textarea></div></section></div>
<section class="card sql-results"><div class="row spread sql-results-head"><b>Результат запроса</b>
<span id="sql-result-meta" class="meta">Запрос ещё не выполнялся</span></div>
<div id="sql-result-table" class="sql-table-wrap"><div class="sql-empty">Нажмите «Выполнить»</div></div>
<details id="sql-viz" class="sql-viz" hidden><summary>Полученные визуализации</summary>
<iframe id="sql-viz-frame" class="sql-viz-frame" sandbox="allow-scripts"
title="SQL visualizations"></iframe></details></section></section></main>
<div id="prompt-modal" class="modal-backdrop" hidden onclick="if(event.target===this)closePrompt()">
<section class="card prompt-modal" role="dialog" aria-modal="true" aria-labelledby="prompt-modal-title">
<div class="row spread prompt-modal-head"><div><h2 id="prompt-modal-title">Agent prompt</h2>
<div class="sub">Current initialization prompt · representative dynamic values</div></div>
<button onclick="closePrompt()" aria-label="Close prompt">✕</button></div>
<div class="prompt-modal-body"><div id="prompt-markdown" class="prompt-markdown"></div></div>
</section></div>
<script>
const csrf="__CSRF__"; const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",
">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
function preferredTheme(){const saved=localStorage.getItem("sloperator-theme");
return saved|| (matchMedia("(prefers-color-scheme: light)").matches?"light":"dark")}
function applyTheme(theme){document.documentElement.dataset.theme=theme;
document.getElementById("theme-toggle").textContent=theme==="light"?"Тёмная тема":"Светлая тема"}
function toggleTheme(){const next=document.documentElement.dataset.theme==="light"?"dark":"light";
localStorage.setItem("sloperator-theme",next);applyTheme(next)}
function setTab(tab){if(!["agents","cron","triggers","codex","sql"].includes(tab))tab="agents";
for(const name of ["agents","cron","triggers","codex","sql"]){document.getElementById("panel-"+name).classList.toggle("active",name===tab);
document.getElementById("tab-"+name).classList.toggle("active",name===tab)}
if(location.hash!=="#"+tab)history.replaceState(null,"","#"+tab)}
async function api(path,opts={}){opts.headers={...(opts.headers||{}),"X-Admin-CSRF":csrf};
const r=await fetch("/admin/api"+path,opts);if(!r.ok)throw new Error(await r.text());return r.json()}
async function action(path,body){await api(path,{method:"POST",headers:{"Content-Type":"application/json"},
body:JSON.stringify(body||{})});await load()}
function sessionCard(s){const k=encodeURIComponent(s.channel_id)+"/"+encodeURIComponent(s.thread_ts);
const msgs=(s.messages||[]).map(m=>`<div class="msg"><div class="meta">${esc(m.message_ts)} ·
${esc(m.user_id||m.bot_id||"unknown")}</div>${esc(m.text)}</div>`).join("");
return `<section class="card" id="session-${esc(k)}" data-session="${esc(k)}"><div class="row spread"><div><b>${esc(s.channel_name)}</b>
<span class="meta">${esc(s.channel_id)} / ${esc(s.thread_ts)}</span></div><span class="badge
${esc(s.runtime_status)}">${esc(s.runtime_status)}</span></div><div class="meta">${esc(s.provider)}:
${esc(s.model)} · turns ${s.turn_count} · updated ${esc(s.updated_at)}
${s.process_id?` · PID ${esc(s.process_id)} + subprocess tree`:""}</div>
${s.last_error?`<pre>${esc(s.last_error)}</pre>`:""}<details><summary>${s.headless?"Prompt and result":"Thread messages"}</summary>
<div class="messages">${msgs||'<span class="sub">No archived messages</span>'}</div></details>${s.headless?"":`
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
function openAgentSession(channel,thread){setTab("agents");const key=encodeURIComponent(channel)+"/"+
encodeURIComponent(thread);requestAnimationFrame(()=>{const card=document.getElementById("session-"+key);
if(card){card.scrollIntoView({behavior:"smooth",block:"start"});card.style.outline="2px solid var(--blue)";
setTimeout(()=>card.style.outline="",1800)}});return false}
function inlineMarkdown(text){return esc(text).replace(/`([^`]+)`/g,"<code>$1</code>")
.replace(/\\*\\*([^*]+)\\*\\*/g,"<strong>$1</strong>")}
function renderMarkdown(markdown){const lines=String(markdown||"").split("\\n"),parts=[];let list=[];
const flushList=()=>{if(list.length){parts.push("<ul>"+list.map(x=>"<li>"+inlineMarkdown(x)+
"</li>").join("")+"</ul>");list=[]}};for(let i=0;i<lines.length;i++){const line=lines[i];
if(line.startsWith("```")){flushList();const code=[];for(i++;i<lines.length&&!lines[i].startsWith(
"```");i++)code.push(lines[i]);parts.push("<pre><code>"+esc(code.join("\\n"))+"</code></pre>");
continue}if(line.startsWith("- ")){list.push(line.slice(2));continue}flushList();
if(!line.trim())continue;if(line.startsWith("> "))parts.push("<blockquote>"+inlineMarkdown(
line.slice(2))+"</blockquote>");else if(line.startsWith("### "))parts.push("<h3>"+
inlineMarkdown(line.slice(4))+"</h3>");else if(line.startsWith("## "))parts.push("<h2>"+
inlineMarkdown(line.slice(3))+"</h2>");else parts.push("<p>"+inlineMarkdown(line)+"</p>")}
flushList();return parts.join("")}
function openPrompt(trigger){document.getElementById("prompt-modal-title").textContent=trigger.name;
document.getElementById("prompt-markdown").innerHTML=renderMarkdown(trigger.prompt);
const modal=document.getElementById("prompt-modal");modal.hidden=false;document.body.style.overflow="hidden";
modal.querySelector("button").focus()}
function closePrompt(){document.getElementById("prompt-modal").hidden=true;document.body.style.overflow=""}
addEventListener("keydown",event=>{if(event.key==="Escape")closePrompt()});
let cronSignature="",triggerSignature="";
let codexSignature="",selectedCodex=localStorage.getItem("sloperator-codex-session")||"",
codexDrafts={},codexDetail=null;
async function selectCodex(id){if(selectedCodex)codexDrafts[selectedCodex]=
document.getElementById("codex-input")?.value||"";selectedCodex=id;
localStorage.setItem("sloperator-codex-session",id);
codexDetail=null;codexSignature="";await loadCodexDetail(id)}
async function loadCodexDetail(id){if(!id)return;try{codexDetail=await api(
"/codex/sessions/"+encodeURIComponent(id));renderCodex(window.codexSessions||[])}
catch(error){codexDetail=null}}
async function newCodexSession(){const title=prompt("Название сессии (необязательно)")||"";
const result=await api("/codex/sessions",{method:"POST",headers:{"Content-Type":"application/json"},
body:JSON.stringify({title})});selectCodex(result.session.session_id)}
async function deleteCodex(id){if(!confirm("Удалить эту Codex-сессию и её историю?"))return;
await api("/codex/sessions/"+encodeURIComponent(id)+"/delete",{method:"POST"});
if(selectedCodex===id){selectedCodex="";localStorage.removeItem("sloperator-codex-session")}
codexSignature="";await load()}
async function sendCodex(){const input=document.getElementById("codex-input");const text=input?.value.trim();
if(!text||!selectedCodex)return;input.value="";codexDrafts[selectedCodex]="";await api("/codex/sessions/"+
encodeURIComponent(selectedCodex)+"/message",{method:"POST",headers:{"Content-Type":"application/json"},
body:JSON.stringify({text})});codexSignature="";await load()}
function renderCodex(sessions){if(selectedCodex)codexDrafts[selectedCodex]=
document.getElementById("codex-input")?.value||codexDrafts[selectedCodex]||"";
window.codexSessions=sessions;if(selectedCodex&&!sessions.some(s=>s.session_id===selectedCodex))
selectedCodex="";if(!selectedCodex&&sessions.length)selectedCodex=sessions[0].session_id;
const list=document.getElementById("codex-sessions");list.innerHTML=sessions.map(s=>
`<button class="codex-session ${s.session_id===selectedCodex?"active":""}"
onclick="selectCodex('${esc(s.session_id)}')"><span class="codex-session-title">${esc(s.title)}</span>
<span class="meta">${esc(s.status)} · ${esc(s.updated_at)}</span></button>`).join("")||
'<div class="sub">Нет сессий</div>';const summary=sessions.find(s=>s.session_id===selectedCodex);
const session=codexDetail?.session_id===selectedCodex?{...summary,...codexDetail}:summary;
const chat=document.getElementById("codex-chat");if(!session){chat.innerHTML=
'<div class="codex-empty">Выберите или создайте сессию</div>';return}
const messages=(session.messages||[]).map(m=>`<div class="codex-msg ${esc(m.role)}">
<div class="meta">${esc(m.role)} · ${esc(m.created_at)}</div>${esc(m.content)}</div>`).join("");
chat.innerHTML=`<div class="codex-chat-head row spread"><div><b>${esc(session.title)}</b>
<span class="badge ${esc(session.status)}">${esc(session.status)}</span></div>
<button class="danger" onclick="deleteCodex('${esc(session.session_id)}')">Удалить</button></div>
<div class="codex-messages">${messages||'<div class="codex-empty">Напишите первое сообщение</div>'}
${session.last_error?`<pre>${esc(session.last_error)}</pre>`:""}</div>
<div class="codex-compose"><textarea id="codex-input" placeholder="Сообщение Codex"
onkeydown="if((event.metaKey||event.ctrlKey)&&event.key==='Enter')sendCodex()"></textarea>
<div class="row spread"><span class="meta">Ctrl/⌘ + Enter — отправить</span>
<button onclick="sendCodex()">Отправить</button></div></div>`;
const input=document.getElementById("codex-input");if(input)input.value=codexDrafts[selectedCodex]||"";
const box=chat.querySelector(".codex-messages");if(box)box.scrollTop=box.scrollHeight}
const sqlSession=sessionStorage.getItem("sloperator-sql-session")||crypto.randomUUID();
sessionStorage.setItem("sloperator-sql-session",sqlSession);
let sqlTimer=null,sqlRequest=0,sqlLastSent="",sqlPastingSuggestion=false;
let sqlResult=null,sqlResultQuery="";
function sqlStatus(text,kind=""){const el=document.getElementById("sql-status");el.textContent=text;
el.className="meta sql-status "+kind}
const sqlKeywords=new Set(("SELECT FROM WHERE WITH AS JOIN LEFT RIGHT FULL INNER OUTER CROSS ON "+
"AND OR NOT IN IS NULL GROUP BY ORDER HAVING LIMIT OFFSET UNION ALL DISTINCT CASE WHEN THEN ELSE END "+
"OVER PARTITION ROWS RANGE BETWEEN PRECEDING FOLLOWING CURRENT ASC DESC INSERT INTO UPDATE DELETE CREATE "+
"TABLE VIEW MATERIALIZED DROP ALTER ARRAY JOIN GLOBAL PREWHERE SAMPLE SETTINGS FORMAT QUALIFY").split(" "));
const sqlFunctions=new Set(("count countIf countDistinct uniq uniqExact sum sumIf avg avgIf min max argMin "+
"argMax if multiIf coalesce nullIf toDate toDateTime toStartOfDay toStartOfWeek toStartOfMonth dateDiff "+
"dateAdd dateSub formatDateTime lower upper trim replaceRegexpAll match extract has hasAny arrayMap arrayFilter "+
"arrayJoin groupArray quantile median round floor ceil cast assumeNotNull").toLowerCase().split(" "));
function highlightSql(sql){let out="",i=0;const add=(kind,value)=>out+=kind?`<span class="${kind}">${esc(value)}</span>`:esc(value);
while(i<sql.length){const rest=sql.slice(i),line=rest.match(/^--[^\\n]*/),block=rest.match(/^\\/\\*[\\s\\S]*?\\*\\//);
if(line){add("sql-comment",line[0]);i+=line[0].length;continue}if(block){add("sql-comment",block[0]);
i+=block[0].length;continue}const quote=sql[i];if(quote==="'"||quote==='"'||quote==="`"){let j=i+1;
while(j<sql.length){if(sql[j]===quote){if(sql[j+1]===quote){j+=2;continue}j++;break}j++}
add("sql-str",sql.slice(i,j));i=j;continue}const param=rest.match(/^\\{\\{[^}]+\\}\\}|^\\{[A-Za-z_]\\w*:[^}]+\\}/);
if(param){add("sql-param",param[0]);i+=param[0].length;continue}const number=rest.match(/^\\b\\d+(?:\\.\\d+)?\\b/);
if(number){add("sql-num",number[0]);i+=number[0].length;continue}const word=rest.match(/^[A-Za-z_]\\w*/);
if(word){const upper=word[0].toUpperCase(),lower=word[0].toLowerCase();
add(sqlKeywords.has(upper)?"sql-kw":sqlFunctions.has(lower)?"sql-fn":"",word[0]);i+=word[0].length;
continue}add("",sql[i]);i++}return out+(sql.endsWith("\\n")?" ":"\\n")}
function paintSql(id){const editor=document.getElementById(id),highlight=document.getElementById(id+"-highlight");
highlight.innerHTML=highlightSql(editor.value);highlight.scrollTop=editor.scrollTop;
highlight.scrollLeft=editor.scrollLeft}
function changeSqlProvider(){localStorage.setItem("sloperator-sql-provider",
document.getElementById("sql-provider").value);sqlLastSent="";scheduleSqlCompletion()}
function scheduleSqlCompletion(){clearTimeout(sqlTimer);const input=document.getElementById("sql-input");
localStorage.setItem("sloperator-sql-draft",input.value);if(sqlPastingSuggestion){
sqlPastingSuggestion=false;sqlLastSent=input.value;sqlStatus("Вставлено предложение агента");return}
if(!input.value.trim()){sqlStatus("Напишите SQL — подсказка появится после паузы");return}
if(input.value===sqlLastSent)return;sqlStatus("Жду паузу во вводе…");
sqlTimer=setTimeout(requestSqlCompletion,7000)}
async function requestSqlCompletion(){const input=document.getElementById("sql-input");
const sql=input.value;if(!sql.trim()||sql===sqlLastSent)return;sqlLastSent=sql;
const requestId=++sqlRequest;sqlStatus("Агент дописывает SQL…","busy");
try{const result=await api("/sql/complete",{method:"POST",headers:{"Content-Type":"application/json"},
body:JSON.stringify({session_id:sqlSession,provider:document.getElementById("sql-provider").value,sql})});
if(requestId!==sqlRequest)return;if(input.value===sql){document.getElementById("sql-output").value=result.sql;
paintSql("sql-output");
sqlStatus("Готово · измените запрос для новой подсказки")}else{sqlStatus("SQL изменился · жду новую паузу");
scheduleSqlCompletion()}}catch(error){if(requestId===sqlRequest)sqlStatus("Ошибка агента: "+error.message,"error")}}
async function copySqlSuggestion(){const value=document.getElementById("sql-output").value;if(!value)return;
await navigator.clipboard.writeText(value);sqlStatus("SQL скопирован")}
function setSqlButtons(running){document.getElementById("sql-run").disabled=running;
document.getElementById("sql-visualize").disabled=running||!sqlResult||
document.getElementById("sql-input").value!==sqlResultQuery}
function renderSqlResult(result){const root=document.getElementById("sql-result-table");
const meta=document.getElementById("sql-result-meta"),columns=result.columns||[],rows=result.rows||[];
meta.textContent=`${rows.length} строк${result.truncated?" · лимит 1000":""} · ${columns.length} столбцов`;
if(!columns.length){root.innerHTML='<div class="sql-empty">Запрос не вернул столбцов</div>';return}
const head=columns.map(column=>`<th>${esc(column)}</th>`).join("");
const body=rows.map(row=>`<tr>${columns.map((_,index)=>`<td>${esc(row[index]??"NULL")}</td>`).join("")}</tr>`).join("");
root.innerHTML=`<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`}
async function executeSql(){const sql=document.getElementById("sql-input").value;if(!sql.trim())return;
sqlStatus("Выполняю запрос…","busy");setSqlButtons(true);document.getElementById("sql-viz").hidden=true;
try{const result=await api("/sql/execute",{method:"POST",headers:{"Content-Type":"application/json"},
body:JSON.stringify({sql})});sqlResult=result;sqlResultQuery=sql;renderSqlResult(result);
sqlStatus("Запрос выполнен")}catch(error){sqlResult=null;document.getElementById("sql-result-meta").textContent="Ошибка";
document.getElementById("sql-result-table").innerHTML=`<div class="sql-empty">${esc(error.message)}</div>`;
sqlStatus("Ошибка запроса: "+error.message,"error")}finally{setSqlButtons(false)}}
async function visualizeSql(){if(!sqlResult)return;sqlStatus("Агент строит визуализации…","busy");
setSqlButtons(true);try{const result=await api("/sql/visualize",{method:"POST",
headers:{"Content-Type":"application/json"},body:JSON.stringify({session_id:sqlSession,
provider:document.getElementById("sql-provider").value,sql:sqlResultQuery,columns:sqlResult.columns,
sample_rows:sqlResult.rows.slice(0,20)})});const data=JSON.stringify(
{columns:sqlResult.columns,rows:sqlResult.rows}).replaceAll("<","\\\\u003c");
document.getElementById("sql-viz-frame").srcdoc=result.html.replace("__SLOPERATOR_DATA__",data);
const details=document.getElementById("sql-viz");details.hidden=false;details.open=true;
sqlStatus("Визуализации готовы")}catch(error){sqlStatus("Ошибка визуализации: "+error.message,"error")}
finally{setSqlButtons(false)}}
function initSqlEditor(){const input=document.getElementById("sql-input");
input.value=localStorage.getItem("sloperator-sql-draft")||"";
document.getElementById("sql-provider").value=localStorage.getItem("sloperator-sql-provider")||"claude";
paintSql("sql-input");paintSql("sql-output");input.addEventListener("input",()=>{paintSql("sql-input");
scheduleSqlCompletion();setSqlButtons(false)});for(const id of ["sql-input","sql-output"]){const editor=document.getElementById(id);
editor.addEventListener("scroll",()=>paintSql(id))}
input.addEventListener("paste",event=>{
const suggestion=document.getElementById("sql-output").value;
sqlPastingSuggestion=Boolean(suggestion&&event.clipboardData?.getData("text")===suggestion)});
input.addEventListener("keydown",event=>{if(event.key==="Tab"){event.preventDefault();const start=input.selectionStart;
input.setRangeText("  ",start,input.selectionEnd,"end");input.dispatchEvent(new Event("input"))}})}
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
function automationButton(kind,item){const verb=item.enabled?"stop":"start",label=item.enabled?"Stop":"Start";
return `<button class="${item.enabled?"danger":""}" onclick="event.stopPropagation();action('/automations/${kind}/${encodeURIComponent(item.name||item.key)}/${verb}')">${label}</button>`}
function cronRow(job,events,days,today){const firstEvent=events.length?
[...events].sort((a,b)=>a.time.localeCompare(b.time))[0].time.slice(0,10):today;
const cells=days.map(date=>{const key=dayKey(date);
const runs=events.filter(event=>dayKey(utcDate(event.time))===key).sort((a,b)=>a.time.localeCompare(b.time));
const executionRuns=runs.filter(event=>statusLabel(event.status)!=="scheduled");
const planned=key>=firstEvent?plannedRuns(job,date):0;const displayedRuns=planned===1&&executionRuns.length?
[executionRuns[executionRuns.length-1]]:executionRuns.slice(0,planned);
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
return `<div class="cron-job-label" title="${esc(job.name)} · ${esc(job.schedule)}"><div class="row spread"><b>${esc(job.name)}</b>${automationButton("crons",job)}</div>
<div class="cron-job-stats">${job.enabled?"active":"stopped"} · ${esc(job.schedule)} · ${events.length} events${completed?` · ${completed} done`:""}</div>
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
function triggerRunStatus(event){return event.session_status==="running"?"running":event.status||"queued"}
function renderTriggerHistory(triggers,events){const configs=document.getElementById("trigger-configs");
window.slackTriggers=triggers;configs.innerHTML=triggers.map((trigger,index)=>`<section
class="card trigger-config" tabindex="0" role="button" onclick="openPrompt(window.slackTriggers[${index}])"
onkeydown="if(event.key==='Enter'||event.key===' ')openPrompt(window.slackTriggers[${index}])">
<div class="row spread">
<h3>${esc(trigger.name)}</h3><div class="row"><span class="badge">${trigger.enabled?"active":"stopped"}</span>${automationButton("triggers",trigger)}</div>
</div><div class="trigger-condition">${esc(trigger.channel_name)} · <code>${esc(trigger.channel_id)}</code><br>
Source: <code>${esc(trigger.source)}</code><br>${esc(trigger.condition)}<br>
Limit: ${esc(trigger.limit||"one session per matched incident")}<br><b>Click to view prompt</b>
</div></section>`).join("")||
'<div class="card sub">No configured Slack triggers</div>';
const root=document.getElementById("trigger-history"),days=calendarDays(),today=dayKey(days[days.length-1]);
const axis=days.map(date=>{const monday=date.getUTCDay()===1;return `<div class="cron-axis-day
${monday?"week-start":""}" title="${dayKey(date)}">${monday?date.toLocaleString("en",
{month:"short",day:"numeric",timeZone:"UTC"}):date.getUTCDate()}</div>`}).join("");
const rows=triggers.map(trigger=>{const triggerEvents=events.filter(e=>e.trigger===trigger.key);
const cells=days.map(date=>{const key=dayKey(date),runs=triggerEvents.filter(e=>e.created_at.slice(0,10)===key);
const segments=runs.map(run=>`<i class="run-segment ${esc(triggerRunStatus(run))}"
title="${esc(run.created_at+" — "+triggerRunStatus(run))}"></i>`).join("");
const cols=Math.max(1,Math.ceil(Math.sqrt(runs.length)));return `<div class="cron-day-slot"><div
class="cron-day ${runs.length?"has-runs":""} ${key===today?"today":""}" style="--segment-cols:${cols}"
title="${esc(key+" · "+runs.length+" trigger(s)")}" aria-label="${esc(key+" · "+runs.length+
" trigger(s)")}">${segments}</div></div>`}).join("");
return `<div class="cron-job-label"><b>${esc(trigger.name)}</b><div class="cron-job-stats">
event-driven · ${triggerEvents.length} launches</div></div>${cells}`}).join("");
const eventRows=events.map(event=>`<tr><td>${esc(event.created_at)} UTC</td><td>${esc(
triggers.find(t=>t.key===event.trigger)?.name||event.trigger)}</td><td><span class="badge ${esc(
triggerRunStatus(event))}">${esc(triggerRunStatus(event))}</span></td><td>${esc(event.channel_name)}</td>
<td><div class="trigger-links">${event.session_exists?`<a href="#agents" onclick="return openAgentSession(
'${esc(event.channel_id)}','${esc(event.thread_ts)}')">Agent session</a>`:"<span class='sub'>No session</span>"}
<a href="${esc(event.slack_url)}" target="_blank" rel="noreferrer">Slack thread ↗</a></div></td></tr>`).join("");
root.innerHTML=`<section class="card cron-board"><div class="cron-board-head row spread">
<b>Trigger calendar</b><span class="badge">${events.length} launches</span></div><div class="cron-scroll">
<div class="cron-grid"><div class="cron-axis-label">Trigger</div>${axis}${rows}</div></div></section>
<details class="card history-log" open><summary>Trigger log</summary><div class="table-wrap"><table>
<thead><tr><th>Time</th><th>Trigger</th><th>Status</th><th>Channel</th><th>Links</th></tr></thead>
<tbody>${eventRows||'<tr><td colspan="5" class="sub">No launches in the last 28 days</td></tr>'}
</tbody></table></div></details>`}
async function load(){const d=await api("/state");const signature=JSON.stringify(d.sessions);
if(signature!==sessionsSignature){renderSessions(d.sessions);sessionsSignature=signature}
const nextCodexSignature=JSON.stringify([d.codex_sessions,selectedCodex]);
if(nextCodexSignature!==codexSignature){renderCodex(d.codex_sessions);codexSignature=nextCodexSignature;
if(selectedCodex)await loadCodexDetail(selectedCodex)}
const nextCronSignature=JSON.stringify([d.cron_jobs,d.cron_history,d.crontab]);
if(nextCronSignature!==cronSignature){const config=document.querySelector(".cron-config");
const history=document.querySelector(".history-log");const scroll=document.querySelector(".cron-scroll");
const ui={configOpen:config?.open||false,historyOpen:history?.open||false,scrollLeft:scroll?.scrollLeft||0};
document.getElementById("cron").innerHTML=d.cron_jobs.length?`<table><thead><tr><th>Job</th>
<th>Schedule</th><th>Command</th><th>Control</th></tr></thead><tbody>${d.cron_jobs.map(x=>`<tr><td>${esc(x.name)}
</td><td><code>${esc(x.schedule)}</code></td><td><code>${esc(x.command)}</code></td><td>${automationButton("crons",x)}</td></tr>`).join("")}
</tbody></table><details><summary>Raw crontab</summary><pre>${esc(d.crontab)}</pre></details>`:
'<span class="sub">No user crontab</span>';
renderCronHistory(d.cron_jobs,d.cron_history);document.querySelector(".cron-config").open=ui.configOpen;
const nextHistory=document.querySelector(".history-log");if(nextHistory)nextHistory.open=ui.historyOpen;
const nextScroll=document.querySelector(".cron-scroll");if(nextScroll)nextScroll.scrollLeft=ui.scrollLeft;
cronSignature=nextCronSignature}
const nextTriggerSignature=JSON.stringify([d.slack_triggers,d.slack_trigger_runs]);
if(nextTriggerSignature!==triggerSignature){renderTriggerHistory(d.slack_triggers,d.slack_trigger_runs);
triggerSignature=nextTriggerSignature}}
applyTheme(preferredTheme());initSqlEditor();setTab(location.hash.slice(1));
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


def _cron_jobs(crontab: str) -> list[dict[str, Any]]:
    """Extract repo-managed job blocks and their schedule lines."""
    jobs: list[dict[str, Any]] = []
    current_name: str | None = None
    for raw_line in crontab.splitlines():
        line = raw_line.strip()
        if line.startswith("# >>> ug-ai-analyst:") and line.endswith(" >>>"):
            current_name = line.removeprefix("# >>> ug-ai-analyst:").removesuffix(" >>>")
            continue
        if line.startswith("# <<< ug-ai-analyst:"):
            current_name = None
            continue
        enabled = True
        if line.startswith("# sloperator-disabled: "):
            line = line.removeprefix("# sloperator-disabled: ")
            enabled = False
        if current_name is None or not line or line.startswith("#") or "=" in line.split()[0]:
            continue
        fields = line.split(maxsplit=5)
        if len(fields) == 6:
            jobs.append(
                {
                    "name": current_name,
                    "schedule": " ".join(fields[:5]),
                    "command": fields[5],
                    "enabled": enabled,
                }
            )
    return jobs


def _set_cron_enabled(name: str, enabled: bool) -> bool:
    """Comment or uncomment the schedule line in one named managed block."""
    current = _crontab()
    if current.startswith("Unable to read crontab:"):
        raise RuntimeError(current)
    lines = current.splitlines()
    in_target = False
    changed = False
    found = False
    for index, raw in enumerate(lines):
        line = raw.strip()
        if line == f"# >>> ug-ai-analyst:{name} >>>":
            in_target = True
            found = True
            continue
        if in_target and line.startswith("# <<< ug-ai-analyst:"):
            in_target = False
        if not in_target:
            continue
        if enabled and line.startswith("# sloperator-disabled: "):
            lines[index] = raw.replace("# sloperator-disabled: ", "", 1)
            changed = True
        elif not enabled and line and not line.startswith("#") and "=" not in line.split()[0]:
            lines[index] = f"# sloperator-disabled: {raw}"
            changed = True
    if not found:
        return False
    if changed:
        result = subprocess.run(
            ["crontab", "-"],
            input="\n".join(lines) + "\n",
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "crontab update failed")
    return True


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
                timestamp = dt.datetime.strptime(match.group("time"), "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=timezone
                )
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


def _systemd_scheduler_job(settings: Settings) -> dict[str, Any]:
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
            "--grep=sloperator\\.experiment_finalizer:",
            "--case-sensitive=yes",
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


def _slack_trigger_definitions(settings: Settings) -> list[dict[str, str]]:
    """Describe the event-driven Slack automations shown in the admin UI."""
    analytics_prompt = build_monetisation_agent_prompt(
        AlertBatch("{{ alert timestamp }}", "{{ Slack message timestamp }}"),
        [
            (
                Alert(
                    "{{ metric }}",
                    "{{ platform }}",
                    "{{ events or uniques }}",
                    "{{ prophet delta }}",
                    0,
                    0,
                    "{{ p-value }}",
                ),
                {
                    "value": 120,
                    "last_week": 100,
                    "wow": 0.20,
                    "peak_wow": 0.25,
                },
            )
        ],
    )
    subscription_prompt = build_subscription_flow_agent_prompt(
        SubscriptionFlowIncident(
            "{{ incident nature key }}",
            frozenset({"{{ platform:flow-kind }}"}),
            "{{ exact SERIOUS Slack alert }}",
        )
    )
    mobile_prompt = build_mobile_health_agent_prompt(
        "{{ source report header and summary }}",
        [
            MobileCriticalMetric(
                "{{ Android or iOS }}",
                "{{ Metabase card title }}",
                "https://metabase.mu.se/question/{{ card id }}",
                ":red_circle: {{ critical metric line with detector evidence }}",
                ("{{ optional segment or numerator/denominator diagnostics }}",),
            )
        ],
    )
    payment_prompt = build_payment_layer_agent_prompt(
        ":rotating_light: *Payment path collapsed* — {{ source build `path` }}\n"
        "{{ current rate vs baseline · estimated loss }}\n"
        "{{ incident responders }} · investigation in thread"
    )
    preview_note = (
        "> Preview: values in `{{ double braces }}` are filled from the triggering Slack "
        "report. The surrounding instructions are the current production prompt.\n\n"
    )
    return [
        {
            "key": "analytics-anomaly",
            "name": "Analytics anomaly analysis",
            "channel_id": settings.anomaly_alert_channel,
            "channel_name": "ug-analytics-monitoring",
            "source": settings.anomaly_bot_id,
            "condition": ("Analytics Bot mentions the operator; confirmed UG monetisation anomaly"),
            "limit": "24h cooldown per metric/platform/type",
            "prompt": preview_note + analytics_prompt,
        },
        {
            "key": "subscription-flow",
            "name": "Subscription flow incident",
            "channel_id": settings.subscription_flow_alert_channel,
            "channel_name": "ug-analytics-monitoring",
            "source": "Sloperator subscription_flow_monitor",
            "condition": "New SERIOUS incident nature; recovery closes affected components",
            "limit": "one session per active incident nature",
            "prompt": preview_note + subscription_prompt,
        },
        {
            "key": "mobile-health",
            "name": "Mobile health critical drops",
            "channel_id": settings.mobile_health_alert_channel,
            "channel_name": "ug-monetization-metrics-monitoring",
            "source": settings.mobile_health_bot_id,
            "condition": "Red critical metrics in Android/iOS report sections",
            "limit": "at most 5 metrics per report",
            "prompt": preview_note + mobile_prompt,
        },
        {
            "key": "payment-layer",
            "name": "Payment layer incident",
            "channel_id": settings.payment_layer_alert_channel,
            "channel_name": "ug-analytics-monitoring",
            "source": "Sloperator payment monitors",
            "condition": "New payment error signature or catastrophic path collapse",
            "limit": "one open incident per source/path nature; recovery in the source thread",
            "prompt": preview_note + payment_prompt,
        },
    ]


def create_admin_routes(
    app: web.Application,
    store: EventStore,
    orchestrator: AgentOrchestrator,
    slack_client: AsyncWebClient,
    automation_controls: AutomationControls | None = None,
) -> None:
    """Attach loopback admin routes to the existing HTTP application."""
    csrf = secrets.token_urlsafe(32)
    codex_manager = AdminCodexManager(orchestrator.settings, store)
    sql_manager = AdminSqlManager(orchestrator.settings, store)
    automation_controls = automation_controls or AutomationControls(
        orchestrator.settings.database_path.parent / "automation-controls.json"
    )

    async def close_admin_managers(_: web.Application) -> None:
        await codex_manager.close()
        await sql_manager.close()

    app.on_cleanup.append(close_admin_managers)

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
        runtime_headless = orchestrator.headless_sessions()
        runtime_keys = {
            (session["channel_id"], session["thread_ts"]) for session in runtime_headless
        }
        persisted_headless = await asyncio.to_thread(store.list_scheduled_agent_runs)
        sessions = [
            *runtime_headless,
            *(
                session
                for session in persisted_headless
                if (session["channel_id"], session["thread_ts"]) not in runtime_keys
            ),
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
        service_job["enabled"] = not automation_controls.disabled("crons", service_job["name"])
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
                "codex_sessions": await codex_manager.list_threads(),
                "slack_triggers": [
                    {
                        **trigger,
                        "enabled": not automation_controls.disabled("triggers", trigger["key"]),
                    }
                    for trigger in _slack_trigger_definitions(orchestrator.settings)
                ],
                "slack_trigger_runs": await asyncio.to_thread(store.list_slack_trigger_runs),
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

    async def set_automation(request: web.Request) -> web.Response:
        require_csrf(request)
        kind = request.match_info["kind"]
        name = request.match_info["name"]
        enabled = request.match_info["action"] == "start"
        if kind == "crons" and name != "experiment-finalizer (sloperator.service)":
            if not await asyncio.to_thread(_set_cron_enabled, name, enabled):
                raise web.HTTPNotFound(text="Unknown managed cron")
        elif kind == "triggers":
            valid = {item["key"] for item in _slack_trigger_definitions(orchestrator.settings)}
            if name not in valid:
                raise web.HTTPNotFound(text="Unknown Slack trigger")
        elif kind != "crons":
            raise web.HTTPBadRequest(text="Unknown automation kind")
        automation_controls.set_enabled(kind, name, enabled)
        return web.json_response({"enabled": enabled})

    async def close(request: web.Request) -> web.Response:
        require_csrf(request)
        key = (request.match_info["channel"], request.match_info["thread"])
        await orchestrator.cancel(*key)
        closed = orchestrator.dismiss_headless(*key)
        if key[0] == "scheduled":
            deleted = await asyncio.to_thread(store.delete_scheduled_agent_run, key[1])
            closed = closed or deleted
        elif not closed:
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

    async def create_codex_session(request: web.Request) -> web.Response:
        require_csrf(request)
        body = await request.json()
        session = await codex_manager.create(str(body.get("title", "")))
        return web.json_response({"session": session})

    async def read_codex_session(request: web.Request) -> web.Response:
        require_local(request)
        try:
            session = await codex_manager.read(request.match_info["session_id"])
        except CodexAppServerError as error:
            raise web.HTTPNotFound(text=str(error)) from error
        return web.json_response(session)

    async def send_codex_message(request: web.Request) -> web.Response:
        require_csrf(request)
        body = await request.json()
        try:
            result = await codex_manager.submit(
                request.match_info["session_id"], str(body.get("text", ""))
            )
        except KeyError:
            raise web.HTTPNotFound(text="Unknown Codex session") from None
        except (ValueError, RuntimeError) as error:
            raise web.HTTPConflict(text=str(error)) from error
        return web.json_response({"result": result})

    async def delete_codex_session(request: web.Request) -> web.Response:
        require_csrf(request)
        deleted = await codex_manager.delete(request.match_info["session_id"])
        return web.json_response({"deleted": deleted})

    async def complete_sql(request: web.Request) -> web.Response:
        require_csrf(request)
        body = await request.json()
        session_id = str(body.get("session_id", "")).strip()
        provider = str(body.get("provider", "claude")).strip().lower()
        sql = str(body.get("sql", ""))
        if not session_id or len(session_id) > 100:
            raise web.HTTPBadRequest(text="Valid SQL session ID is required")
        try:
            result = await sql_manager.complete(session_id, provider, sql)
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error)) from error
        except RuntimeError as error:
            raise web.HTTPBadGateway(text=str(error)) from error
        return web.json_response({"sql": result})

    async def execute_sql(request: web.Request) -> web.Response:
        require_csrf(request)
        body = await request.json()
        sql = str(body.get("sql", ""))
        try:
            result = await sql_manager.execute(sql)
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error)) from error
        except RuntimeError as error:
            raise web.HTTPBadGateway(text=str(error)) from error
        return web.json_response(result)

    async def visualize_sql(request: web.Request) -> web.Response:
        require_csrf(request)
        body = await request.json()
        session_id = str(body.get("session_id", "")).strip()
        provider = str(body.get("provider", "claude")).strip().lower()
        sql = str(body.get("sql", ""))
        columns = body.get("columns", [])
        sample_rows = body.get("sample_rows", [])
        if not session_id or len(session_id) > 100:
            raise web.HTTPBadRequest(text="Valid SQL session ID is required")
        if not isinstance(columns, list) or not isinstance(sample_rows, list):
            raise web.HTTPBadRequest(text="Visualization sample must be tabular")
        try:
            html = await sql_manager.visualize(
                session_id,
                provider,
                sql,
                columns[:100],
                sample_rows[:20],
            )
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error)) from error
        except RuntimeError as error:
            raise web.HTTPBadGateway(text=str(error)) from error
        return web.json_response({"html": html})

    app.router.add_get("/admin", page)
    app.router.add_get("/admin/api/state", state)
    app.router.add_post("/admin/api/sessions/{channel}/{thread}/stop", stop)
    app.router.add_post("/admin/api/automations/{kind}/{name}/{action:start|stop}", set_automation)
    app.router.add_post("/admin/api/sessions/{channel}/{thread}/close", close)
    app.router.add_post("/admin/api/sessions/{channel}/{thread}/message", message)
    app.router.add_post("/admin/api/codex/sessions", create_codex_session)
    app.router.add_get("/admin/api/codex/sessions/{session_id}", read_codex_session)
    app.router.add_post("/admin/api/codex/sessions/{session_id}/message", send_codex_message)
    app.router.add_post("/admin/api/codex/sessions/{session_id}/delete", delete_codex_session)
    app.router.add_post("/admin/api/sql/complete", complete_sql)
    app.router.add_post("/admin/api/sql/execute", execute_sql)
    app.router.add_post("/admin/api/sql/visualize", visualize_sql)
