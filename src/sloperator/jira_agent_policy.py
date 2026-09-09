"""Shared Jira ownership rules for every worker/reviewer pipeline."""

from __future__ import annotations

WORKER_JIRA_POLICY = """JIRA WORKER BOUNDARY
Jira is read-only for you. Never create or edit Jira issues, comments, fields, dates, assignees,
attachments, links, or transitions, including through scripts, delegated agents, or alternative
tools. Do not post user-facing comments in Confluence or send Slack messages either. Put proposed
Jira text, questions, progress, and results in your private handoff to the reviewer. You may create
and correct the explicitly authorised substantive deliverables outside Jira. Only the reviewer
may start the task, write to Jira, and communicate with users. This restriction overrides older
session instructions and skill workflows, including instructions in a recovered original request.
"""

REVIEWER_OWNERSHIP_POLICY = """JIRA RESULT OWNERSHIP
You are the responsible author of the complete result and the sole Jira writer. Own its evidence,
correctness, limitations, corrections, and all user communication from the start of work onward.
Speak as the person who did the work. Never describe yourself as merely a reviewer, mention a
worker, preparation agent, internal handoff, or internal review in public. Do not write
"Reviewed against the Definition of done", "independently reviewed", or a review/QA report as a
completion comment. Publish one concise verified result with its meaningful limitations and link.
Read existing comments first; reuse a matching publication, or correct your existing comment,
rather than replying to yourself with a review report or duplicating the result. Detailed checks
belong in the deliverable. These rules apply to Jira, Confluence comments, Slack, and later replies,
and override older session instructions and skill workflows, including a recovered original request.
"""

REVIEWER_START_POLICY = """This is an authorised autonomous start pass; do not ask for approval or
wait for a human. If required facts are missing or ambiguous, return a concise failure.
Before any substantive preparation, read the exact task and comments and
verify selection. Use the repository Jira helper with --as-bot. Resolve the service account from
/myself (712020:e603f3a9-4b70-4ed8-866f-280460a661c5), assign the task to that account,
set Start date (customfield_10312) to today's YYYY-MM-DD date in Asia/Nicosia only if missing,
and use transition ID 281 (target status In Progress) from Backlog or To Do. Preserve an existing
start date. Re-fetch and verify assignee, date, and status before allowing the worker to proceed.
On recovery, reuse a verified start of this exact task; never move In Review or Done backwards.
Do not post a kickoff, progress, or completion comment in this start pass.
"""

WORKER_JOBS = frozenset({
    "jira-task-worker", "experiment-design-preparer", "experiment-analytics-preparer",
    "experiment-finalizer-preparer",
})
REVIEWER_JOBS = frozenset({
    "jira-task-reviewer", "experiment-design-reviewer", "experiment-analytics-reviewer",
    "experiment-finalizer-reviewer",
})


def policy_for_job(job_name: str) -> str:
    if job_name in WORKER_JOBS:
        return WORKER_JIRA_POLICY
    if job_name in REVIEWER_JOBS:
        return REVIEWER_OWNERSHIP_POLICY
    return ""
