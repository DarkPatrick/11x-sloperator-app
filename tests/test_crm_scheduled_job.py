"""Registry entry of the CRM finalizer cron and the digest CRM forecast run name."""

from __future__ import annotations

import subprocess
import sys

from sloperator import crm_finalizer, daily_activity_digest
from sloperator.automated_session_policy import AUTOMATED_RESPONSE_STYLE
from sloperator.config import Settings
from sloperator.scheduled_jobs import (
    EMBEDDED_SCHEDULED_JOBS,
    EMBEDDED_SCHEDULED_JOBS_BY_JOB_NAME,
    EMBEDDED_SCHEDULED_JOBS_BY_NAME,
    EmbeddedScheduledJob,
)

CRM_DISPLAY_NAME = "crm-finalizer (sloperator.service)"
CRM_RUN_NAMES = (
    "crm-finalizer-preparer",
    "crm-finalizer-reviewer",
    "crm-post-analysis-preparer",
    "crm-post-analysis-reviewer",
)


def make_settings(**overrides: object) -> Settings:
    """Settings from keyword overrides only; the environment is not read."""
    return Settings(
        slack_user_id="UOWNER",
        bot_token="xoxb-test",
        app_token="xapp-test",
        **overrides,  # type: ignore[arg-type]
    )


def crm_job() -> EmbeddedScheduledJob:
    return EMBEDDED_SCHEDULED_JOBS_BY_JOB_NAME["crm-finalizer"]


def test_c1_crm_entry_is_registered_with_its_own_display_name() -> None:
    job = crm_job()
    assert job.job_name == "crm-finalizer"
    assert job.display_name == CRM_DISPLAY_NAME
    assert EMBEDDED_SCHEDULED_JOBS_BY_NAME[CRM_DISPLAY_NAME] is job


def test_c2_crm_entry_follows_experiment_finalizer_once() -> None:
    names = [job.job_name for job in EMBEDDED_SCHEDULED_JOBS]
    assert names.index("crm-finalizer") == names.index("experiment-finalizer") + 1
    assert names.count("crm-finalizer") == 1
    assert names.count("experiment-finalizer") == 1


def test_c3_crm_run_job_names_are_the_four_crm_run_names() -> None:
    assert crm_job().run_job_names == crm_finalizer.CRM_RUN_JOB_NAMES == CRM_RUN_NAMES


def test_c4_default_schedule_is_14_nicosia() -> None:
    settings = make_settings()
    assert settings.crm_finalizer_hour == 14
    assert crm_job().schedule(settings) == "weekdays Mon-Fri 14:00 Asia/Nicosia"


def test_c5_crm_schedule_uses_its_own_hour_and_finalizer_timezone() -> None:
    settings = make_settings(
        crm_finalizer_hour=9,
        experiment_finalizer_hour=12,
        experiment_finalizer_timezone="Europe/London",
    )
    assert crm_job().schedule(settings) == "weekdays Mon-Fri 09:00 Europe/London"
    ugm_text = EMBEDDED_SCHEDULED_JOBS_BY_JOB_NAME["experiment-finalizer"].schedule(settings)
    assert "12" in ugm_text and "09:00" not in ugm_text


def test_c6_logger_and_log_messages() -> None:
    job = crm_job()
    assert (
        job.logger_name,
        job.scheduled_prefix,
        job.started_message,
        job.completed_message,
        job.failed_message,
    ) == (
        "sloperator.crm_finalizer",
        "Next CRM finalizer run scheduled for ",
        "Starting scheduled CRM finalizer run",
        "CRM finalizer run completed",
        "Could not complete the CRM finalizer run",
    )
    assert job.logger_name == crm_finalizer.LOGGER.name
    line = "Next CRM finalizer run scheduled for %s" % "2026-11-16 14:00:00+02:00"  # noqa: UP031
    assert line.startswith(job.scheduled_prefix)


def test_c7_prompt_source_and_condition() -> None:
    job = crm_job()
    # R2: the admin shows the stored templates, <CRM_RUN_TIME> unrendered.
    assert job.prompt == (
        crm_finalizer.CRM_FIRST_READ_START_PROMPT
        + "\n\n"
        + crm_finalizer.CRM_POST_ANALYSIS_START_PROMPT
    )
    assert job.prompt_source == (
        "sloperator.crm_finalizer.CRM_FIRST_READ_START_PROMPT + CRM_POST_ANALYSIS_START_PROMPT"
    )
    assert job.condition == (
        "One CRM first read and one After Split post-analysis per weekday; silent when none"
    )


def test_c8_digest_recognises_crm_forecast_run_name() -> None:
    digest = EMBEDDED_SCHEDULED_JOBS_BY_JOB_NAME["daily-activity-digest"]
    assert digest.run_job_names == (
        "daily-activity-digest",
        "daily-activity-finalization-forecast",
        "daily-activity-crm-forecast",
    )
    assert digest.run_job_names[2] == daily_activity_digest.CRM_FINALIZATION_FORECAST_JOB_NAME


def test_c9_digest_prompt_appends_crm_forecast_prompt() -> None:
    digest = EMBEDDED_SCHEDULED_JOBS_BY_JOB_NAME["daily-activity-digest"]
    suffix = "\n\n" + daily_activity_digest.CRM_FINALIZATION_FORECAST_PROMPT
    assert digest.prompt.endswith(suffix)
    assert digest.prompt[: -len(suffix)] == (
        daily_activity_digest.DIGEST_PROMPT
        + "\n\n"
        + daily_activity_digest.FINALIZATION_FORECAST_PROMPT
    )
    assert digest.prompt_source.endswith(" + CRM_FINALIZATION_FORECAST_PROMPT")


def test_c10_experiment_finalizer_entry_stays_separate() -> None:
    job = EMBEDDED_SCHEDULED_JOBS_BY_JOB_NAME["experiment-finalizer"]
    assert job.job_name == "experiment-finalizer"
    assert job.run_job_names == ("experiment-finalizer-preparer", "experiment-finalizer-reviewer")
    assert not set(job.run_job_names) & set(CRM_RUN_NAMES)
    assert job.display_name != CRM_DISPLAY_NAME
    text = job.schedule(make_settings())
    assert "12:00" in text and "14:00" not in text


def test_e1_schedule_hour_boundaries() -> None:
    texts = [
        crm_job().schedule(
            make_settings(crm_finalizer_hour=hour, experiment_finalizer_timezone="Asia/Nicosia")
        )
        for hour in (0, 23)
    ]
    assert texts == [
        "weekdays Mon-Fri 00:00 Asia/Nicosia",
        "weekdays Mon-Fri 23:00 Asia/Nicosia",
    ]


def test_e2_no_run_name_is_shared_between_entries() -> None:
    owners: dict[str, list[str]] = {}
    for job in EMBEDDED_SCHEDULED_JOBS:
        for run_name in job.run_job_names:
            owners.setdefault(run_name, []).append(job.job_name)
    assert {name: jobs for name, jobs in owners.items() if len(jobs) > 1} == {}
    assert all(owners[name] == ["crm-finalizer"] for name in CRM_RUN_NAMES)
    assert owners["daily-activity-crm-forecast"] == ["daily-activity-digest"]


def test_e3_display_and_job_names_are_unique() -> None:
    display_names = [job.display_name for job in EMBEDDED_SCHEDULED_JOBS]
    job_names = [job.job_name for job in EMBEDDED_SCHEDULED_JOBS]
    assert len(display_names) == len(set(display_names))
    assert len(job_names) == len(set(job_names))


def test_e4_registry_imports_in_a_fresh_interpreter_in_both_orders() -> None:
    for code in (
        "import sloperator.scheduled_jobs as m; "
        "m.EMBEDDED_SCHEDULED_JOBS_BY_JOB_NAME['crm-finalizer']",
        "import sloperator.crm_finalizer; import sloperator.scheduled_jobs as m; "
        "m.EMBEDDED_SCHEDULED_JOBS_BY_JOB_NAME['crm-finalizer']",
    ):
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr


def test_e5_schedule_is_pure() -> None:
    settings = make_settings(crm_finalizer_hour=14, experiment_finalizer_timezone="Asia/Nicosia")
    assert crm_job().schedule(settings) == crm_job().schedule(settings)


def test_e6_crm_prompt_embeds_automated_response_style() -> None:
    assert AUTOMATED_RESPONSE_STYLE in crm_job().prompt
