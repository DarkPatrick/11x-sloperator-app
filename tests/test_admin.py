from __future__ import annotations

import json
from unittest.mock import patch

from sloperator.admin import _cron_history, _cron_jobs, _crontab


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
