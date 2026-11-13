"""Single source of truth for schedulers embedded in sloperator.service."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sloperator.automation_error_audit import AUDIT_PROMPT, HOUR, TIMEZONE
from sloperator.config import Settings
from sloperator.crm_finalizer import (
    CRM_FIRST_READ_START_PROMPT,
    CRM_POST_ANALYSIS_START_PROMPT,
    CRM_RUN_JOB_NAMES,
)
from sloperator.daily_activity_digest import (
    CRM_FINALIZATION_FORECAST_JOB_NAME,
    CRM_FINALIZATION_FORECAST_PROMPT,
    DIGEST_PROMPT,
    FINALIZATION_FORECAST_PROMPT,
)
from sloperator.daily_activity_digest import HOUR as DIGEST_HOUR
from sloperator.daily_activity_digest import TIMEZONE as DIGEST_TIMEZONE
from sloperator.experiment_analytics_planner import PREPARATION_PROMPT as ANALYTICS_PROMPT
from sloperator.experiment_design_planner import PREPARATION_PROMPT
from sloperator.experiment_finalizer import START_PROMPT as FINALIZATION_PROMPT
from sloperator.jira_task_automation import WORKER_PROMPT
from sloperator.mysql_docs_sync import HOUR as MYSQL_DOCS_HOUR
from sloperator.mysql_docs_sync import PROMPT as MYSQL_DOCS_PROMPT
from sloperator.mysql_docs_sync import PROMPT_SOURCE as MYSQL_DOCS_PROMPT_SOURCE
from sloperator.mysql_docs_sync import TIMEZONE_NAME as MYSQL_DOCS_TIMEZONE
from sloperator.skill_docs_sync import HOUR as SKILL_DOCS_HOUR
from sloperator.skill_docs_sync import PROMPT as SKILL_DOCS_PROMPT
from sloperator.skill_docs_sync import PROMPT_SOURCE as SKILL_DOCS_PROMPT_SOURCE
from sloperator.skill_docs_sync import TIMEZONE_NAME as SKILL_DOCS_TIMEZONE


@dataclass(frozen=True)
class EmbeddedScheduledJob:
    """Metadata shared by the runtime controls and the admin UI."""

    job_name: str
    run_job_names: tuple[str, ...]
    display_name: str
    schedule: Callable[[Settings], str]
    logger_name: str
    scheduled_prefix: str
    started_message: str
    completed_message: str
    failed_message: str
    prompt_source: str
    condition: str
    prompt: str


EMBEDDED_SCHEDULED_JOBS = (
    EmbeddedScheduledJob(
        job_name="jira-task-automation",
        run_job_names=("jira-task-worker", "jira-task-reviewer"),
        display_name="jira-task-automation (sloperator.service)",
        schedule=lambda _settings: "hourly; quota-gated UMN Jira tasks",
        logger_name="sloperator.jira_task_automation",
        scheduled_prefix="Next Jira task automation run scheduled for ",
        started_message="Starting Jira task automation run",
        completed_message="Jira task automation run completed",
        failed_message="Could not complete Jira task automation run",
        prompt_source="sloperator.jira_task_automation.WORKER_PROMPT",
        condition="Queued UMN tasks assigned to ug-ai-analyst when Claude quota permits",
        prompt=WORKER_PROMPT,
    ),
    EmbeddedScheduledJob(
        job_name="experiment-finalizer",
        run_job_names=("experiment-finalizer-preparer", "experiment-finalizer-reviewer"),
        display_name="experiment-finalizer (sloperator.service)",
        schedule=lambda settings: (
            f"weekdays Mon-Fri {settings.experiment_finalizer_hour:02d}:00 "
            f"{settings.experiment_finalizer_timezone}"
        ),
        logger_name="sloperator.experiment_finalizer",
        scheduled_prefix="Next experiment finalizer run scheduled for ",
        started_message="Starting scheduled experiment finalizer run",
        completed_message="Experiment finalizer run completed",
        failed_message="Could not start the daily experiment finalizer",
        prompt_source="sloperator.experiment_finalizer.START_PROMPT",
        condition="One autonomous experiment-finalisation agent run per weekday",
        prompt=FINALIZATION_PROMPT,
    ),
    EmbeddedScheduledJob(
        job_name="crm-finalizer",
        run_job_names=CRM_RUN_JOB_NAMES,
        display_name="crm-finalizer (sloperator.service)",
        schedule=lambda settings: (
            f"weekdays Mon-Fri {settings.crm_finalizer_hour:02d}:00 "
            f"{settings.experiment_finalizer_timezone}"
        ),
        logger_name="sloperator.crm_finalizer",
        scheduled_prefix="Next CRM finalizer run scheduled for ",
        started_message="Starting scheduled CRM finalizer run",
        completed_message="CRM finalizer run completed",
        failed_message="Could not complete the CRM finalizer run",
        prompt_source=(
            "sloperator.crm_finalizer.CRM_FIRST_READ_START_PROMPT + CRM_POST_ANALYSIS_START_PROMPT"
        ),
        condition=(
            "One CRM first read and one After Split post-analysis per weekday; silent when none"
        ),
        prompt=CRM_FIRST_READ_START_PROMPT + "\n\n" + CRM_POST_ANALYSIS_START_PROMPT,
    ),
    EmbeddedScheduledJob(
        job_name="experiment-design-planner",
        run_job_names=("experiment-design-preparer", "experiment-design-reviewer"),
        display_name="experiment-design-planner (sloperator.service)",
        schedule=lambda settings: (
            f"weekdays Mon-Fri {settings.experiment_design_hour:02d}:00 "
            f"{settings.experiment_design_timezone}"
        ),
        logger_name="sloperator.experiment_design_planner",
        scheduled_prefix="Next experiment design run scheduled for ",
        started_message="Starting scheduled experiment design run",
        completed_message="Experiment design run completed",
        failed_message="Could not complete the daily experiment design run",
        prompt_source="sloperator.experiment_design_planner.PREPARATION_PROMPT",
        condition=(
            "One oldest eligible Reach & Impact / Experiment design task per weekday; "
            "silent when none"
        ),
        prompt=PREPARATION_PROMPT,
    ),
    EmbeddedScheduledJob(
        job_name="experiment-analytics-planner",
        run_job_names=("experiment-analytics-preparer", "experiment-analytics-reviewer"),
        display_name="experiment-analytics-planner (sloperator.service)",
        schedule=lambda settings: (
            f"weekdays Mon-Fri {settings.experiment_analytics_hour:02d}:00 "
            f"{settings.experiment_analytics_timezone}"
        ),
        logger_name="sloperator.experiment_analytics_planner",
        scheduled_prefix="Next experiment analytics run scheduled for ",
        started_message="Starting scheduled experiment analytics run",
        completed_message="Experiment analytics run completed",
        failed_message="Could not complete the daily experiment analytics run",
        prompt_source="sloperator.experiment_analytics_planner.PREPARATION_PROMPT",
        condition="One oldest eligible Analytics task per weekday; silent when none",
        prompt=ANALYTICS_PROMPT,
    ),
    EmbeddedScheduledJob(
        job_name="daily-activity-digest",
        run_job_names=(
            "daily-activity-digest",
            "daily-activity-finalization-forecast",
            CRM_FINALIZATION_FORECAST_JOB_NAME,
        ),
        display_name="daily-activity-digest (sloperator.service)",
        schedule=lambda _settings: (
            f"weekdays Mon-Fri {DIGEST_HOUR:02d}:00 {DIGEST_TIMEZONE}"
        ),
        logger_name="sloperator.daily_activity_digest",
        scheduled_prefix="Next daily activity digest scheduled for ",
        started_message="Starting scheduled daily activity digest",
        completed_message="Daily activity digest completed",
        failed_message="Could not complete the daily activity digest",
        prompt_source=(
            "sloperator.daily_activity_digest.DIGEST_PROMPT + FINALIZATION_FORECAST_PROMPT"
            " + CRM_FINALIZATION_FORECAST_PROMPT"
        ),
        condition=(
            "Weekday message to #owl-ugmonetization; deterministic fallback when the agent "
            "cannot run"
        ),
        prompt=(
            DIGEST_PROMPT
            + "\n\n"
            + FINALIZATION_FORECAST_PROMPT
            + "\n\n"
            + CRM_FINALIZATION_FORECAST_PROMPT
        ),
    ),
    EmbeddedScheduledJob(
        job_name="automation-error-audit",
        run_job_names=("automation-error-audit",),
        display_name="automation-error-audit (sloperator.service)",
        schedule=lambda _settings: f"daily {HOUR:02d}:00 {TIMEZONE}",
        logger_name="sloperator.automation_error_audit",
        scheduled_prefix="Next automation error audit scheduled for ",
        started_message="Starting scheduled automation error audit",
        completed_message="Automation error audit completed",
        failed_message="Could not complete the daily automation error audit",
        prompt_source="sloperator.automation_error_audit.AUDIT_PROMPT",
        condition="Daily read-only audit; sends a DM only when failures are found",
        prompt=AUDIT_PROMPT,
    ),
    EmbeddedScheduledJob(
        job_name="skill-docs-sync",
        run_job_names=("skill-docs-sync",),
        display_name="skill-docs-sync (sloperator.service)",
        schedule=lambda _settings: (
            f"daily {SKILL_DOCS_HOUR:02d}:00 {SKILL_DOCS_TIMEZONE}"
        ),
        logger_name="sloperator.skill_docs_sync",
        scheduled_prefix="Next skill docs sync scheduled for ",
        started_message="Starting scheduled skill docs sync",
        completed_message="Skill docs sync completed",
        failed_message="Daily skill docs sync failed",
        prompt_source=SKILL_DOCS_PROMPT_SOURCE,
        condition=(
            "Daily fast-forward of ug-ai-analyst main and Confluence regeneration for stale skills"
        ),
        prompt=SKILL_DOCS_PROMPT,
    ),
    EmbeddedScheduledJob(
        job_name="mysql-docs-sync",
        run_job_names=("mysql-docs-sync",),
        display_name="mysql-docs-sync (sloperator.service)",
        schedule=lambda _settings: f"weekly Sun {MYSQL_DOCS_HOUR:02d}:00 {MYSQL_DOCS_TIMEZONE}",
        logger_name="sloperator.mysql_docs_sync",
        scheduled_prefix="Next MySQL docs sync scheduled for ",
        started_message="Starting scheduled MySQL docs sync",
        completed_message="MySQL docs sync completed",
        failed_message="Weekly MySQL docs sync failed",
        prompt_source=MYSQL_DOCS_PROMPT_SOURCE,
        condition=(
            "Weekly check of the Confluence MySQL table docs; Claude only for tables with drift"
        ),
        prompt=MYSQL_DOCS_PROMPT,
    ),
)


EMBEDDED_SCHEDULED_JOBS_BY_NAME = {
    job.display_name: job for job in EMBEDDED_SCHEDULED_JOBS
}
EMBEDDED_SCHEDULED_JOBS_BY_JOB_NAME = {
    job.job_name: job for job in EMBEDDED_SCHEDULED_JOBS
}
