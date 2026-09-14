"""Recover task ownership from validated, completed planner reviews."""

import re


def completed_task(job_name: str, result: str, prompt: str) -> str | None:
    # Import lazily: the planners themselves depend on Jira automation.
    from sloperator import experiment_analytics_planner as analytics
    from sloperator import experiment_design_planner as design
    from sloperator import experiment_finalizer as finalizer

    try:
        if job_name == "experiment-design-reviewer":
            return design.task_key_from_review_result(result)
        if job_name == "experiment-analytics-reviewer":
            return analytics.task_key_from_review_result(result)
        if job_name == "experiment-finalizer-reviewer":
            notification = finalizer.normalize_finalization_notification(result)
            if "Results calculated and published." not in notification:
                return None
            # The verified start identifies the task; other Jira links may be epics.
            match = re.search(finalizer.STARTED_RE.pattern, prompt)
            return match[4] if match else None
    except ValueError:
        pass
    return None
