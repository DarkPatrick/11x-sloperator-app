from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from sloperator.admin import (
    ADMIN_HTML,
    _cron_execution_history,
    _cron_history,
    _cron_jobs,
    _crontab,
    _label_cron_history,
    _systemd_scheduler_history,
    _systemd_scheduler_job,
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
    assert "28 days ago" in args
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
        }
    ]


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
    log = tmp_path / "health.log"
    log.write_text(
        "\n".join(
            (
                "[2026-07-27 10:05:00] [cron_retry:health] child exited rc=0",
                "[2026-07-27 11:00:00] [cron_retry:health] not the scheduled fire: skipping",
                "[2026-07-28 10:05:00] [cron_retry:health] child exited rc=1",
            )
        )
    )
    jobs = [
        {
            "name": "health",
            "schedule": "0 7,8 * * *",
            "command": (
                f"TZ=Asia/Nicosia python cron_retry.py --only-at 10:00 "
                f"--log-out {log} -- child.py"
            ),
        }
    ]

    rows, authoritative = _cron_execution_history(jobs)

    assert authoritative == {"health"}
    assert [row["status"] for row in rows] == ["failed", "completed"]
    assert [row["time"] for row in rows] == [
        "2026-07-28 07:05:00 UTC",
        "2026-07-27 07:05:00 UTC",
    ]


def test_cron_execution_history_reads_jsonl_job_outcomes(tmp_path: Path) -> None:
    script = tmp_path / "probe.py"
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "probe.jsonl").write_text(
        "\n".join(
            (
                '{"ts":"2026-07-28T08:00:00+00:00","status":"ok"}',
                '{"ts":"2026-07-28T08:30:00+00:00","status":"data_unavailable"}',
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
    assert [row["status"] for row in rows] == ["failed", "completed"]


def test_admin_contains_airflow_style_28_day_cron_grid() -> None:
    assert "Array.from({length:28}" in ADMIN_HTML
    assert 'class="cron-grid"' in ADMIN_HTML
    assert "function cronRow(job,events,days,today)" in ADMIN_HTML
    assert "Last 28 days · UTC" in ADMIN_HTML
    assert "renderCronHistory(d.cron_jobs,d.cron_history)" in ADMIN_HTML
    assert "function plannedRuns(job,date)" in ADMIN_HTML
    assert 'class="run-segment ${esc(status)}"' in ADMIN_HTML
    assert "cronFieldValues(fields[0],0,59).size*cronFieldValues(fields[1],0,23).size" in ADMIN_HTML


def test_cron_refresh_preserves_expanded_sections_and_scroll() -> None:
    assert "let cronSignature=" in ADMIN_HTML
    assert "configOpen:config?.open" in ADMIN_HTML
    assert "historyOpen:history?.open" in ADMIN_HTML
    assert "scrollLeft:scroll?.scrollLeft" in ADMIN_HTML


def test_admin_supports_headless_agent_runs() -> None:
    assert "s.headless" in ADMIN_HTML
    assert "PID ${esc(s.process_id)} + subprocess tree" in ADMIN_HTML
    assert 's.headless?"Prompt and result":"Thread messages"' in ADMIN_HTML


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
    assert "setTimeout(requestSqlCompletion,7000)" in ADMIN_HTML
    assert "if(input.value===sqlLastSent)return" in ADMIN_HTML
    assert 'event.clipboardData?.getData("text")===suggestion' in ADMIN_HTML
    assert '<option value="claude">Claude</option>' in ADMIN_HTML
    assert '<option value="codex">Codex</option>' in ADMIN_HTML


def test_systemd_scheduler_job_includes_schedule_and_runtime_state() -> None:
    settings = Settings(
        slack_user_id="UOWNER",
        bot_token="xoxb-test",
        app_token="xapp-test",
    )
    with patch("sloperator.admin.subprocess.run") as run:
        run.return_value.stdout = "MainPID=123\nActiveState=active\n"

        job = _systemd_scheduler_job(settings)

    assert job == {
        "name": "experiment-finalizer (sloperator.service)",
        "schedule": "weekdays Mon-Fri 12:00 Asia/Nicosia",
        "command": "embedded asyncio scheduler · active · PID 123",
    }


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
    with patch("sloperator.admin.subprocess.run") as run:
        run.return_value.stdout = "\n".join((json.dumps(scheduled), json.dumps(started)))

        rows = _systemd_scheduler_history()

    assert [row["command"] for row in rows] == [
        "sloperator.service · experiment-finalizer · started",
        ("sloperator.service · experiment-finalizer · scheduled: 2026-07-28T12:00:00+03:00"),
    ]
    assert [row["status"] for row in rows] == ["started", "scheduled"]
    assert {row["job"] for row in rows} == {"experiment-finalizer (sloperator.service)"}
