"""CRM run names in the Jira policy job lists, planner task tracking and the store."""

import inspect
import re
import subprocess
import sys

import pytest

from sloperator import crm_finalizer, experiment_finalizer, jira_agent_policy, store
from sloperator.experiment_finalizer import NO_OP_NOTIFICATION
from sloperator.planner_task_tracking import completed_task
from sloperator.store import EventStore

PAGE_URL = "https://alice.mu.se/pages/viewpage.action?pageId=838613487"
STARTED_PROMPT = (
    "Verified start:\n"
    f"FINALIZATION_STARTED: 8225 | {PAGE_URL} | Iteration 1 | UMN-4321"
)
_HEADLINE = (
    f"[CRM Black Friday]({PAGE_URL}) — experiment "
    "[8225](https://www.ultimate-guitar.com/components/ab/experiment/view?id=8225), "
    "Iteration 1."
)
UGM_NOTIFICATION = (
    f"{_HEADLINE} Results calculated and published.\n"
    "- After Click: no significant difference in Revenue."
)
PA_NOTIFICATION = (
    f"{_HEADLINE} After Split post-analysis published.\n"
    "- After Split agrees with After Click."
)
CRM_PREPARERS = {"crm-finalizer-preparer", "crm-post-analysis-preparer"}
CRM_REVIEWERS = {"crm-finalizer-reviewer", "crm-post-analysis-reviewer"}
# BOARD_SELECTION_JOBS exactly as it is before the change.
BASELINE_BOARD_SELECTION_JOBS = frozenset({
    "experiment-design-preparer", "experiment-design-reviewer",
    "experiment-analytics-preparer", "experiment-analytics-reviewer",
})


def _started_task(prompt: str) -> str | None:
    match = experiment_finalizer.STARTED_RE.search(prompt)
    return match[4] if match else None


def test_c1_worker_jobs_hold_crm_preparers():
    jobs = jira_agent_policy.WORKER_JOBS
    assert CRM_PREPARERS | {"experiment-finalizer-preparer"} <= jobs
    assert not CRM_REVIEWERS & jobs


def test_c2_reviewer_jobs_hold_crm_reviewers():
    jobs = jira_agent_policy.REVIEWER_JOBS
    assert CRM_REVIEWERS | {"experiment-finalizer-reviewer"} <= jobs
    assert not CRM_PREPARERS & jobs


def test_c3_literal_names_match_crm_finalizer_constants():
    assert len(crm_finalizer.CRM_RUN_JOB_NAMES) == 4
    for name in crm_finalizer.CRM_RUN_JOB_NAMES:
        in_worker = name in jira_agent_policy.WORKER_JOBS
        in_reviewer = name in jira_agent_policy.REVIEWER_JOBS
        assert in_worker != in_reviewer, name
        assert in_worker == name.endswith("-preparer"), name


def test_c4_board_selection_jobs_unchanged():
    jobs = jira_agent_policy.BOARD_SELECTION_JOBS
    assert jobs == BASELINE_BOARD_SELECTION_JOBS
    assert not [name for name in jobs if name.startswith("crm-")]


def test_c5_crm_first_read_reviewer_mirrors_ugm_reviewer():
    assert _started_task(STARTED_PROMPT) == "UMN-4321"
    crm = completed_task("crm-finalizer-reviewer", UGM_NOTIFICATION, STARTED_PROMPT)
    ugm = completed_task("experiment-finalizer-reviewer", UGM_NOTIFICATION, STARTED_PROMPT)
    assert crm == ugm == "UMN-4321"


def test_c6_crm_post_analysis_reviewer_returns_started_task():
    assert _started_task(STARTED_PROMPT) == "UMN-4321"
    result = completed_task("crm-post-analysis-reviewer", PA_NOTIFICATION, STARTED_PROMPT)
    assert result == "UMN-4321"


def test_c7_post_analysis_reviewer_needs_post_analysis_heading():
    assert _started_task(STARTED_PROMPT) == "UMN-4321"
    result = completed_task("crm-post-analysis-reviewer", UGM_NOTIFICATION, STARTED_PROMPT)
    assert result is None


def test_c8_ugm_reviewer_behaviour_unchanged():
    # Regression guard: values recorded from baseline/ for the same inputs.
    job = "experiment-finalizer-reviewer"
    assert completed_task(job, UGM_NOTIFICATION, STARTED_PROMPT) == "UMN-4321"
    assert completed_task(job, PA_NOTIFICATION, STARTED_PROMPT) is None


def _record_run(event_store: EventStore, run_id: str, job_name: str) -> None:
    event_store.create_scheduled_agent_run(
        run_id,
        job_name=job_name,
        provider="claude",
        model="opus",
        external_session_id=f"session-{run_id}",
        prompt=STARTED_PROMPT,
    )
    event_store.finish_scheduled_agent_run(
        run_id, status="completed", result_text=UGM_NOTIFICATION
    )


def test_c9_store_reviewer_query_selects_crm_reviewers(tmp_path):
    event_store = EventStore(tmp_path / "state.sqlite3")
    event_store.initialize()
    jobs = {
        "ugm": "experiment-finalizer-reviewer",
        "crm-first": "crm-finalizer-reviewer",
        "crm-pa": "crm-post-analysis-reviewer",
        "crm-prep": "crm-finalizer-preparer",
    }
    for run_id, job_name in jobs.items():
        _record_run(event_store, run_id, job_name)
    event_store.sync_completed_planner_tasks()
    with event_store._connect() as connection:
        tracked = {
            row[0].removeprefix("planner-task-tracked:")
            for row in connection.execute(
                "SELECT key FROM metadata WHERE key LIKE 'planner-task-tracked:%'"
            )
        }
    assert tracked == {"ugm", "crm-first", "crm-pa"}


def test_c10_store_reviewer_query_lists_five_names():
    source = inspect.getsource(store.EventStore.sync_completed_planner_tasks)
    in_list = re.search(r"job_name IN \(([^)]*)\)", source)
    assert in_list is not None
    assert re.findall(r"'([^']+)'", in_list[1]) == [
        "experiment-design-reviewer",
        "experiment-analytics-reviewer",
        "experiment-finalizer-reviewer",
        "crm-finalizer-reviewer",
        "crm-post-analysis-reviewer",
    ]


def test_e1_post_analysis_without_start_marker_returns_none():
    prompt = "Recovered run, no start marker"
    assert completed_task("crm-post-analysis-reviewer", PA_NOTIFICATION, prompt) is None


FAILURE_TEXT = "Experiment finalisation failed: pending trials on UG_CRM / CRM purchases"


@pytest.mark.parametrize("text", [NO_OP_NOTIFICATION, FAILURE_TEXT])
def test_e2_no_op_and_failure_texts_return_none(text):
    assert completed_task("crm-post-analysis-reviewer", text, STARTED_PROMPT) is None
    crm = completed_task("crm-finalizer-reviewer", text, STARTED_PROMPT)
    ugm = completed_task("experiment-finalizer-reviewer", text, STARTED_PROMPT)
    assert crm is None and crm == ugm


def test_e3_empty_post_analysis_result_returns_none():
    assert completed_task("crm-post-analysis-reviewer", "", STARTED_PROMPT) is None


def _modules_after_import(module: str) -> str:
    code = (
        f"import sys, {module}; "
        "print(sorted(m for m in sys.modules if m in "
        "('sloperator.crm_finalizer', 'sloperator.experiment_finalizer')))"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def test_e4_jira_policy_import_does_not_load_crm_finalizer():
    assert "sloperator.crm_finalizer" not in _modules_after_import(
        "sloperator.jira_agent_policy"
    )


def test_e5_planner_task_tracking_imports_stay_lazy():
    assert _modules_after_import("sloperator.planner_task_tracking") == "[]"


def test_e6_completed_task_is_pure():
    first = completed_task("crm-post-analysis-reviewer", PA_NOTIFICATION, STARTED_PROMPT)
    second = completed_task("crm-post-analysis-reviewer", PA_NOTIFICATION, STARTED_PROMPT)
    assert first == second == "UMN-4321"


def test_e7_only_reviewer_run_names_are_tracked():
    for job in ("crm-finalizer", "crm-post-analysis-preparer"):
        assert completed_task(job, PA_NOTIFICATION, STARTED_PROMPT) is None
