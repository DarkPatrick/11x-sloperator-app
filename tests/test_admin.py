from __future__ import annotations

import json
from unittest.mock import patch

from sloperator.admin import (
    _cron_history,
    _cron_jobs,
    _crontab,
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
        "schedule": "daily 12:00 Asia/Nicosia",
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
            "2026 INFO sloperator.experiment_finalizer: "
            "Starting scheduled experiment finalizer run"
        ),
    }
    with patch("sloperator.admin.subprocess.run") as run:
        run.return_value.stdout = "\n".join((json.dumps(scheduled), json.dumps(started)))

        rows = _systemd_scheduler_history()

    assert [row["command"] for row in rows] == [
        "sloperator.service · experiment-finalizer · started",
        (
            "sloperator.service · experiment-finalizer · scheduled: "
            "2026-07-28T12:00:00+03:00"
        ),
    ]
