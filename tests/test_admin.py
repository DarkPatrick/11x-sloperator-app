from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from sloperator.admin import (
    ADMIN_HTML,
    _cron_agent_prompt_definitions,
    _cron_execution_history,
    _cron_history,
    _cron_jobs,
    _crontab,
    _label_cron_history,
    _merge_attached_scheduled_sessions,
    _set_cron_enabled,
    _slack_trigger_definitions,
    _systemd_scheduler_history,
    _systemd_scheduler_jobs,
)
from sloperator.config import Settings


def test_crontab_returns_current_user_schedule() -> None:
    with patch("sloperator.admin.subprocess.run") as run:
        run.return_value.returncode = 0
        run.return_value.stdout = "*/5 * * * * do-work\n"
        run.return_value.stderr = ""

        assert _crontab() == "*/5 * * * * do-work"


def test_cron_history_extracts_launches_newest_first() -> None:
    older = {
        "__REALTIME_TIMESTAMP": "1000000",
        "MESSAGE": "(egor) CMD (first-job)",
    }
    newer = {
        "__REALTIME_TIMESTAMP": "2000000",
        "MESSAGE": "(egor) CMD (second-job)",
    }
    with patch("sloperator.admin.subprocess.run") as run:
        run.return_value.stdout = "\n".join((json.dumps(older), json.dumps(newer)))

        rows = _cron_history()

    assert [row["command"] for row in rows] == ["second-job", "first-job"]
    args = run.call_args.args[0]
    assert "--since" in args
    assert "3 days ago" in args
    assert "--grep=^\\(egor\\) CMD \\(" in args
    assert "-n" not in args


def test_cron_jobs_extracts_managed_blocks() -> None:
    crontab = """
# >>> ug-ai-analyst:health >>>
SHELL=/bin/bash
*/30 * * * * cd /repo && run-health
# <<< ug-ai-analyst:health <<<
"""

    assert _cron_jobs(crontab) == [
        {
            "name": "health",
            "schedule": "*/30 * * * *",
            "command": "cd /repo && run-health",
            "enabled": True,
        }
    ]


def test_cron_jobs_include_stopped_managed_blocks() -> None:
    crontab = """# >>> ug-ai-analyst:health >>>
# sloperator-disabled: */30 * * * * run-health
# <<< ug-ai-analyst:health <<<"""
    assert _cron_jobs(crontab)[0]["enabled"] is False


def test_set_cron_enabled_preserves_block_and_comments_schedule() -> None:
    crontab = """# >>> ug-ai-analyst:health >>>
SHELL=/bin/bash
*/30 * * * * run-health
# <<< ug-ai-analyst:health <<<"""
    with (
        patch("sloperator.admin._crontab", return_value=crontab),
        patch("sloperator.admin.subprocess.run") as run,
    ):
        run.return_value.returncode = 0
        assert _set_cron_enabled("health", False)
    assert "# sloperator-disabled: */30 * * * * run-health" in run.call_args.kwargs["input"]


def test_cron_history_is_labelled_for_calendar() -> None:
    jobs = [
        {
            "name": "health",
            "schedule": "*/30 * * * *",
            "command": "cd /repo && run-health",
        }
    ]

    assert _label_cron_history(
        jobs,
        [{"time": "2026-07-28 08:00:00 UTC", "command": "cd /repo && run-health"}],
    ) == [
        {
            "time": "2026-07-28 08:00:00 UTC",
            "command": "cd /repo && run-health",
            "job": "health",
            "status": "launched",
        }
    ]


def test_cron_execution_history_uses_real_retry_child_results(tmp_path: Path) -> None:
    timezone = ZoneInfo("Asia/Nicosia")
    local_date = (dt.datetime.now(timezone) - dt.timedelta(days=1)).date()
    completed_at = dt.datetime.combine(local_date, dt.time(10, 5), timezone)
    failed_at = dt.datetime.combine(local_date, dt.time(14, 5), timezone)
    log = tmp_path / "health.log"
    log.write_text(
        "\n".join(
            (
                f"[{completed_at:%Y-%m-%d %H:%M:%S}] [cron_retry:health] child exited rc=0",
                f"[{local_date:%Y-%m-%d} 11:00:00] "
                "[cron_retry:health] not the scheduled fire: skipping",
                f"[{failed_at:%Y-%m-%d %H:%M:%S}] [cron_retry:health] child exited rc=1",
            )
        )
    )
    jobs = [
        {
            "name": "health",
            "schedule": "0 7,8 * * *",
            "command": (
                f"TZ=Asia/Nicosia python cron_retry.py --only-at 10:00 --log-out {log} -- child.py"
            ),
        }
    ]

    rows, authoritative = _cron_execution_history(jobs)

    assert authoritative == {"health"}
    assert [row["status"] for row in rows] == ["failed", "completed"]
    assert [row["time"] for row in rows] == [
        failed_at.astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
        completed_at.astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
    ]


def test_cron_execution_history_reads_jsonl_job_outcomes(tmp_path: Path) -> None:
    base_time = dt.datetime.now(dt.UTC).replace(microsecond=0) - dt.timedelta(days=1)
    script = tmp_path / "probe.py"
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "probe.jsonl").write_text(
        "\n".join(
            (
                json.dumps({"ts": base_time.isoformat(), "status": "ok"}),
                json.dumps(
                    {
                        "ts": (base_time + dt.timedelta(minutes=30)).isoformat(),
                        "status": "data_unavailable",
                    }
                ),
                json.dumps({"ts": (base_time + dt.timedelta(hours=1)).isoformat(), "findings": 2}),
            )
        )
    )
    jobs = [
        {
            "name": "probe",
            "schedule": "*/30 * * * *",
            "command": f"python {script}",
        }
    ]

    rows, authoritative = _cron_execution_history(jobs)

    assert authoritative == {"probe"}
    assert [row["status"] for row in rows] == ["completed", "failed", "completed"]
    assert "status=legacy_success" in rows[0]["command"]


def test_admin_contains_airflow_style_28_day_cron_grid() -> None:
    assert "Array.from({length:28}" in ADMIN_HTML
    assert 'class="cron-grid"' in ADMIN_HTML
    assert "function calendarRow(item,events,days,today,options)" in ADMIN_HTML
    assert "function renderRunCalendar(items,events,options)" in ADMIN_HTML
    assert "Last 28 days · UTC" in ADMIN_HTML
    assert "renderCronHistory(d.cron_jobs,d.cron_history)" in ADMIN_HTML
    assert "function plannedRuns(job,date)" in ADMIN_HTML
    assert 'class="run-segment ${esc(status)}"' in ADMIN_HTML
    assert "cronFieldValues(fields[0],0,59).size*cronFieldValues(fields[1],0,23).size" in ADMIN_HTML


def test_admin_contains_slack_trigger_calendar_and_session_links() -> None:
    assert 'id="tab-triggers"' in ADMIN_HTML
    assert 'id="panel-triggers"' in ADMIN_HTML
    assert "function renderTriggerHistory(triggers,events)" in ADMIN_HTML
    assert "function openAgentSession(channel,thread)" in ADMIN_HTML
    assert "Slack thread ↗" in ADMIN_HTML
    assert "Agent session" in ADMIN_HTML
    assert 'id="prompt-modal"' in ADMIN_HTML
    assert "function openPrompt(trigger)" in ADMIN_HTML
    assert "function renderMarkdown(markdown)" in ADMIN_HTML
    assert "Click to view prompt" in ADMIN_HTML
    assert "automationButton(kind,item)" in ADMIN_HTML
    assert "/automations/${kind}/" in ADMIN_HTML
    assert 'kind:"trigger",title:"Trigger calendar"' in ADMIN_HTML
    assert 'resetCalendarRuns("trigger")' in ADMIN_HTML


def test_slack_trigger_definitions_include_all_automatic_investigations() -> None:
    settings = Settings(
        slack_user_id="UOWNER",
        bot_token="xoxb-test",
        app_token="xapp-test",
    )

    definitions = _slack_trigger_definitions(settings)

    assert [item["key"] for item in definitions] == [
        "analytics-anomaly",
        "subscription-flow",
        "mobile-health",
        "web-health",
        "payment-layer",
    ]
    assert definitions[2]["channel_id"] == settings.mobile_health_alert_channel
    assert definitions[2]["limit"] == "at most 5 metrics per report"
    assert "`time-series-research`" in definitions[0]["prompt"]
    assert "{{ exact SERIOUS Slack alert }}" in definitions[1]["prompt"]
    assert "context/data-warehouse/anomaly-detection.md" in definitions[2]["prompt"]
    assert definitions[3]["channel_id"] == settings.mobile_health_alert_channel
    assert "ug_web_health_monitoring.md" in definitions[3]["prompt"]
    assert definitions[4]["channel_id"] == settings.payment_layer_alert_channel
    assert "no more than five short lines" in definitions[4]["prompt"]


def test_cron_refresh_preserves_expanded_sections_and_scroll() -> None:
    assert "let cronSignature=" in ADMIN_HTML
    assert "configOpen:config?.open" in ADMIN_HTML
    assert "historyOpen:history?.open" in ADMIN_HTML
    assert "scrollLeft:scroll?.scrollLeft" in ADMIN_HTML


def test_cron_calendar_does_not_let_next_schedule_hide_completed_run() -> None:
    assert 'executionRuns=runs.filter(event=>event.status!=="scheduled")' in ADMIN_HTML
    assert "segmentRuns=executionRuns.length?executionRuns" in ADMIN_HTML


def test_cron_calendar_has_clickable_execution_details_and_multi_run_hover() -> None:
    assert 'id="run-modal"' in ADMIN_HTML
    assert "function openCalendarRuns(index,runIndex=null)" in ADMIN_HTML
    assert "function resetCalendarRuns(kind)" in ADMIN_HTML
    assert "openCalendarRuns('${esc(groupKey)}')" in ADMIN_HTML
    assert ".cron-day.multiple:hover" in ADMIN_HTML
    assert "--run-count:${Math.max(1,segmentRuns.length)}" in ADMIN_HTML
    assert "Started, awaiting result" in ADMIN_HTML


def test_cron_agent_prompt_cards_share_the_slack_trigger_component() -> None:
    jobs = [
        {
            "name": "experiment-finalizer (sloperator.service)",
            "schedule": "weekdays Mon-Fri 12:00 Asia/Nicosia",
            "command": "embedded scheduler",
            "enabled": True,
        },
        {
            "name": "automation-error-audit (sloperator.service)",
            "schedule": "daily 14:00 Asia/Nicosia",
            "command": "embedded scheduler",
            "enabled": True,
        },
    ]

    prompts = _cron_agent_prompt_definitions(jobs)

    assert 'id="cron-prompts"' in ADMIN_HTML
    assert "function renderPromptCards(rootId,items,kind)" in ADMIN_HTML
    assert "encodeURIComponent(item.key||item.name)" in ADMIN_HTML
    assert 'renderPromptCards("cron-prompts",d.cron_agent_prompts,"crons")' in ADMIN_HTML
    assert ".trigger-condition code{white-space:normal;overflow-wrap:anywhere" in ADMIN_HTML
    assert prompts[0]["name"] == jobs[0]["name"]
    assert prompts[1]["name"] == jobs[1]["name"]
    assert "daily read-only audit" in prompts[1]["prompt"]
    assert "AUTOMATED RESPONSE STYLE" in prompts[0]["prompt"]


def test_admin_supports_headless_agent_runs() -> None:
    assert "s.headless" in ADMIN_HTML
    assert "PID ${esc(s.process_id)} + subprocess tree" in ADMIN_HTML
    assert 's.headless?"Prompt and result":"Thread messages"' in ADMIN_HTML


def test_admin_lists_both_experiment_design_agent_prompts() -> None:
    job_name = "experiment-design-planner (sloperator.service)"
    prompts = _cron_agent_prompt_definitions([
        {
            "name": job_name,
            "schedule": "weekdays Mon-Fri 15:00 Asia/Nicosia",
            "command": "embedded scheduler",
            "enabled": True,
        }
    ])

    assert [prompt["name"] for prompt in prompts] == [
        f"{job_name} · preparation",
        f"{job_name} · independent review",
    ]
    assert {prompt["key"] for prompt in prompts} == {job_name}
    assert "No eligible experiment-design task was found." in prompts[0]["prompt"]
    assert "{{ calculation task key }}" in prompts[1]["prompt"]
    assert "add one short English comment" in prompts[1]["prompt"]
    assert "Do not send Slack messages yourself" in prompts[1]["prompt"]


def test_admin_merges_published_scheduled_run_with_its_slack_session() -> None:
    scheduled = {
        "channel_id": "scheduled",
        "channel_name": "experiment-finalizer",
        "thread_ts": "run-1",
        "external_session_id": "agent-1",
        "status": "completed",
        "runtime_status": "completed",
        "active": False,
        "turn_count": 1,
        "headless": True,
        "created_at": "2026-08-25 09:00:00",
        "updated_at": "2026-08-25 09:54:37",
        "last_activity_at": "2026-08-25 09:54:37",
        "messages": [{"text": "scheduled prompt"}],
    }
    slack = {
        "channel_id": "C123",
        "channel_name": "ug-monetization-pvt",
        "thread_ts": "123.456",
        "external_session_id": "agent-1",
        "status": "idle",
        "runtime_status": "idle",
        "active": False,
        "turn_count": 1,
        "created_at": "2026-08-25 09:54:38",
        "updated_at": "2026-08-25 09:54:38",
        "last_activity_at": "2026-08-25 09:54:38",
        "messages": [{"text": "published result"}],
    }

    assert _merge_attached_scheduled_sessions([scheduled, slack]) == [
        {
            **slack,
            "channel_name": "experiment-finalizer → ug-monetization-pvt",
            "created_at": scheduled["created_at"],
            "status": "completed",
            "runtime_status": "completed",
            "messages": [*scheduled["messages"], *slack["messages"]],
            "scheduled_run_id": "run-1",
        }
    ]


def test_admin_does_not_merge_a_running_scheduled_run() -> None:
    scheduled = {
        "headless": True,
        "external_session_id": "agent-running",
    }

    assert _merge_attached_scheduled_sessions([scheduled]) == [scheduled]


def test_admin_contains_codex_session_chat_ui() -> None:
    assert 'id="tab-codex"' in ADMIN_HTML
    assert 'id="panel-codex"' in ADMIN_HTML
    assert "newCodexSession()" in ADMIN_HTML
    assert "sendCodex()" in ADMIN_HTML
    assert "deleteCodex(" in ADMIN_HTML
    assert 'localStorage.getItem("sloperator-codex-session")' in ADMIN_HTML
    assert 'document.getElementById("codex-input")?.value' in ADMIN_HTML


def test_admin_contains_debounced_two_pane_sql_editor() -> None:
    assert 'id="tab-sql"' in ADMIN_HTML
    assert 'class="card sql-workbench"' in ADMIN_HTML
    assert 'id="sql-input"' in ADMIN_HTML
    assert 'id="sql-output"' in ADMIN_HTML
    assert 'id="sql-input-highlight"' in ADMIN_HTML
    assert 'id="sql-output-highlight"' in ADMIN_HTML
    assert "function highlightSql(sql)" in ADMIN_HTML
    assert ".sql-comment{" in ADMIN_HTML
    assert "setTimeout(requestSqlCompletion,7000)" in ADMIN_HTML
    assert "if(input.value===sqlLastSent)return" in ADMIN_HTML
    assert 'event.clipboardData?.getData("text")===suggestion' in ADMIN_HTML
    assert '<option value="claude">Claude</option>' in ADMIN_HTML
    assert '<option value="codex">Codex</option>' in ADMIN_HTML
    assert 'id="sql-run"' in ADMIN_HTML
    assert 'id="sql-visualize"' in ADMIN_HTML
    assert 'id="sql-result-table"' in ADMIN_HTML
    assert 'id="sql-viz-frame"' in ADMIN_HTML
    assert "sample_rows:sqlResult.rows.slice(0,20)" in ADMIN_HTML
    assert 'sandbox="allow-scripts"' in ADMIN_HTML


def test_admin_refreshes_stale_csrf_token_after_service_restart() -> None:
    assert "async function refreshCsrf()" in ADMIN_HTML
    assert 'cache:"no-store"' in ADMIN_HTML
    assert 'r.status===403&&error==="Invalid CSRF token"' in ADMIN_HTML
    assert "return api(path,opts,false)" in ADMIN_HTML


def test_systemd_scheduler_jobs_include_every_registered_schedule_and_runtime_state() -> None:
    settings = Settings(
        slack_user_id="UOWNER",
        bot_token="xoxb-test",
        app_token="xapp-test",
    )
    with patch("sloperator.admin.subprocess.run") as run:
        run.return_value.stdout = "MainPID=123\nActiveState=active\n"

        jobs = _systemd_scheduler_jobs(settings)

    assert jobs == [
        {
            "name": "experiment-finalizer (sloperator.service)",
            "schedule": "weekdays Mon-Fri 12:00 Asia/Nicosia",
            "command": "embedded asyncio scheduler · active · PID 123",
        },
        {
            "name": "experiment-design-planner (sloperator.service)",
            "schedule": "weekdays Mon-Fri 15:00 Asia/Nicosia",
            "command": "embedded asyncio scheduler · active · PID 123",
        },
        {
            "name": "automation-error-audit (sloperator.service)",
            "schedule": "daily 14:00 Asia/Nicosia",
            "command": "embedded asyncio scheduler · active · PID 123",
        },
    ]


def test_systemd_scheduler_history_extracts_scheduler_events() -> None:
    scheduled = {
        "__REALTIME_TIMESTAMP": "1000000",
        "MESSAGE": (
            "2026 INFO sloperator.experiment_finalizer: "
            "Next experiment finalizer run scheduled for 2026-07-28T12:00:00+03:00"
        ),
    }
    started = {
        "__REALTIME_TIMESTAMP": "2000000",
        "MESSAGE": (
            "2026 INFO sloperator.experiment_finalizer: Starting scheduled experiment finalizer run"
        ),
    }
    completed = {
        "__REALTIME_TIMESTAMP": "3000000",
        "MESSAGE": (
            "2026 INFO sloperator.experiment_finalizer: Experiment finalizer run completed"
        ),
    }
    with patch("sloperator.admin.subprocess.run") as run:
        run.return_value.stdout = "\n".join(
            (json.dumps(scheduled), json.dumps(started), json.dumps(completed))
        )

        rows = _systemd_scheduler_history()

    assert [row["command"] for row in rows] == [
        "sloperator.service · experiment-finalizer · completed",
        ("sloperator.service · experiment-finalizer · scheduled: 2026-07-28T12:00:00+03:00"),
    ]
    assert [row["status"] for row in rows] == ["completed", "scheduled"]
    assert {row["job"] for row in rows} == {"experiment-finalizer (sloperator.service)"}
    args = run.call_args_list[0].args[0]
    assert "--grep=sloperator\\.experiment_finalizer:" in args
    assert "--case-sensitive=yes" in args
    assert "-n" not in args


def test_systemd_scheduler_history_extracts_automation_audit_events() -> None:
    started = {
        "__REALTIME_TIMESTAMP": "1000000",
        "MESSAGE": (
            "INFO sloperator.automation_error_audit: "
            "Starting scheduled automation error audit"
        ),
    }
    completed = {
        "__REALTIME_TIMESTAMP": "2000000",
        "MESSAGE": "INFO sloperator.automation_error_audit: Automation error audit completed",
    }
    with patch("sloperator.admin.subprocess.run") as run:
        run.return_value.stdout = "\n".join((json.dumps(started), json.dumps(completed)))

        rows = _systemd_scheduler_history()

    assert rows == [{
        "time": "1970-01-01 00:00:01 UTC",
        "command": "sloperator.service · automation-error-audit · completed",
        "job": "automation-error-audit (sloperator.service)",
        "status": "completed",
    }]


def test_systemd_scheduler_history_closes_superseded_start() -> None:
    first = {
        "__REALTIME_TIMESTAMP": "1000000",
        "MESSAGE": (
            "INFO sloperator.experiment_finalizer: "
            "Starting scheduled experiment finalizer run"
        ),
    }
    second = {**first, "__REALTIME_TIMESTAMP": "2000000"}
    with patch("sloperator.admin.subprocess.run") as run:
        run.return_value.stdout = "\n".join((json.dumps(first), json.dumps(second)))

        rows = _systemd_scheduler_history()

    assert [row["status"] for row in rows] == ["started", "interrupted"]


def test_systemd_scheduler_history_uses_durable_recovery_status() -> None:
    started = {
        "__REALTIME_TIMESTAMP": "1000000",
        "MESSAGE": (
            "INFO sloperator.experiment_finalizer: "
            "Starting scheduled experiment finalizer run"
        ),
    }
    durable = [{
        "channel_name": "experiment-finalizer",
        "created_at": "1970-01-01 00:00:01",
        "status": "completed",
    }]
    with patch("sloperator.admin.subprocess.run") as run:
        run.return_value.stdout = json.dumps(started)

        rows = _systemd_scheduler_history(durable)

    assert [row["status"] for row in rows] == ["completed"]


def test_design_scheduler_history_uses_preparer_durable_status() -> None:
    started = {
        "__REALTIME_TIMESTAMP": "1000000",
        "MESSAGE": (
            "INFO sloperator.experiment_design_planner: "
            "Starting scheduled experiment design run"
        ),
    }
    durable = [{
        "channel_name": "experiment-design-preparer",
        "created_at": "1970-01-01 00:00:01",
        "status": "failed",
    }]
    with patch("sloperator.admin.subprocess.run") as run:
        run.return_value.stdout = json.dumps(started)

        rows = _systemd_scheduler_history(durable)

    design_rows = [row for row in rows if "experiment-design-planner" in row["command"]]
    assert [row["status"] for row in design_rows] == ["failed"]


def test_systemd_scheduler_history_closes_start_on_failure() -> None:
    started = {
        "__REALTIME_TIMESTAMP": "1000000",
        "MESSAGE": (
            "INFO sloperator.experiment_design_planner: "
            "Starting scheduled experiment design run"
        ),
    }
    failed = {
        "__REALTIME_TIMESTAMP": "2000000",
        "MESSAGE": (
            "ERROR sloperator.experiment_design_planner: "
            "Could not complete the daily experiment design run"
        ),
    }
    with patch("sloperator.admin.subprocess.run") as run:
        run.return_value.stdout = "\n".join((json.dumps(started), json.dumps(failed)))

        rows = _systemd_scheduler_history()

    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert rows[0]["command"] == "sloperator.service · experiment-design-planner · failed"
