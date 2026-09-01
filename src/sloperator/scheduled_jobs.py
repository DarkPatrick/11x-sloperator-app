"""Single source of truth for schedulers embedded in sloperator.service."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sloperator.automation_error_audit import AUDIT_PROMPT, HOUR, TIMEZONE
from sloperator.config import Settings
from sloperator.experiment_design_planner import PREPARATION_PROMPT
from sloperator.experiment_finalizer import FINALIZATION_PROMPT


@dataclass(frozen=True)
class EmbeddedScheduledJob:
    """Metadata shared by the runtime controls and the admin UI."""

    job_name: str
    display_name: str
    schedule: Callable[[Settings], str]
    logger_name: str
    scheduled_prefix: str
    started_message: str
    completed_message: str
    prompt_source: str
    condition: str
    prompt: str


EMBEDDED_SCHEDULED_JOBS = (
    EmbeddedScheduledJob(
        job_name="experiment-finalizer",
        display_name="experiment-finalizer (sloperator.service)",
        schedule=lambda settings: (
            f"weekdays Mon-Fri {settings.experiment_finalizer_hour:02d}:00 "
            f"{settings.experiment_finalizer_timezone}"
        ),
        logger_name="sloperator.experiment_finalizer",
        scheduled_prefix="Next experiment finalizer run scheduled for ",
        started_message="Starting scheduled experiment finalizer run",
        completed_message="Experiment finalizer run completed",
        prompt_source="sloperator.experiment_finalizer.FINALIZATION_PROMPT",
        condition="One autonomous experiment-finalisation agent run per weekday",
        prompt=FINALIZATION_PROMPT,
    ),
    EmbeddedScheduledJob(
        job_name="experiment-design-planner",
        display_name="experiment-design-planner (sloperator.service)",
        schedule=lambda settings: (
            f"daily {settings.experiment_design_hour:02d}:00 "
            f"{settings.experiment_design_timezone}"
        ),
        logger_name="sloperator.experiment_design_planner",
        scheduled_prefix="Next experiment design run scheduled for ",
        started_message="Starting scheduled experiment design run",
        completed_message="Experiment design run completed",
        prompt_source="sloperator.experiment_design_planner.PREPARATION_PROMPT",
        condition=(
            "One oldest eligible Reach & Impact / Experiment design task per day; "
            "silent when none"
        ),
        prompt=PREPARATION_PROMPT,
    ),
    EmbeddedScheduledJob(
        job_name="automation-error-audit",
        display_name="automation-error-audit (sloperator.service)",
        schedule=lambda _settings: f"daily {HOUR:02d}:00 {TIMEZONE}",
        logger_name="sloperator.automation_error_audit",
        scheduled_prefix="Next automation error audit scheduled for ",
        started_message="Starting scheduled automation error audit",
        completed_message="Automation error audit completed",
        prompt_source="sloperator.automation_error_audit.AUDIT_PROMPT",
        condition="Daily read-only audit; sends a DM only when failures are found",
        prompt=AUDIT_PROMPT,
    ),
)


EMBEDDED_SCHEDULED_JOBS_BY_NAME = {
    job.display_name: job for job in EMBEDDED_SCHEDULED_JOBS
}
EMBEDDED_SCHEDULED_JOBS_BY_JOB_NAME = {
    job.job_name: job for job in EMBEDDED_SCHEDULED_JOBS
}
