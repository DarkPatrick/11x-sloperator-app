"""Daily CRM (Bloomreach) finalizer: a first read per CRM send unit, then the After Split
post-analysis once its trials have matured, then a CRM-only overdue Results sweep.

The start -> preparation -> review -> publish machinery is the experiment finalizer's; this
module supplies the CRM prompts and run names through `FinalizerFlow`. Shared functions are
called through the `experiment_finalizer` module object so tests can patch them in one place.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass

from slack_sdk.web.async_client import AsyncWebClient

from sloperator import experiment_finalizer
from sloperator.agents import AgentOrchestrator
from sloperator.automated_session_policy import (
    AUTOMATED_ATLASSIAN_IDENTITY,
    AUTOMATED_RESPONSE_STYLE,
    AUTOMATED_SESSION_REPOSITORY_POLICY,
    LOCAL_CONFLUENCE_USER_DIRECTORY,
)
from sloperator.config import Settings
from sloperator.experiment_design_selector import SelectionError
from sloperator.experiment_finalizer import (
    CALC_API_COMMAND,
    FINAL_NOTIFICATION_POLICY,
    FINALIZER_CALC_MAX_HOURS,
    FINALIZER_CALC_PRIORITY,
    FINALIZER_CALC_SOURCE,
    JIRA_HELPER,
    MATURITY_FORECAST_COMMENT_PREFIX,
    MATURITY_UNKNOWN_COMMENT_PREFIX,
    NO_OP_NOTIFICATION,
    NO_OP_PREFIX,
    PENDING_TRIALS_SMALL_COUNT_LIMIT,
    QUEUE_FORECAST_RESULT_PREFIX,
    SERVICE_ACCOUNT_ID,
    STARTED_RE,
    SUCCESS_HEADING,
    SWEEP_DONE_PREFIX,
    SWEEP_FAILED_PREFIX,
    SWEEP_RESULT_PREFIX,
    WAITING_COMMENT_PREFIX,
    AgentSubmitter,
    FinalizerFlow,
    InvalidFinalizationNotification,
    is_preparation_result,
    is_reviewer_result,
    next_run_at,
    split_maturity_forecasts,
)
from sloperator.jira_agent_policy import (
    REVIEWER_OWNERSHIP_POLICY,
    REVIEWER_START_POLICY,
    WORKER_JIRA_POLICY,
)
from sloperator.scheduler_catchup import SlotRunner
from sloperator.store import EventStore

LOGGER = logging.getLogger(__name__)

CRM_JOB_NAME = "crm-finalizer"
CRM_FIRST_READ_REVIEWER_JOB = "crm-finalizer-reviewer"
CRM_FIRST_READ_PREPARER_JOB = "crm-finalizer-preparer"
CRM_POST_ANALYSIS_REVIEWER_JOB = "crm-post-analysis-reviewer"
CRM_POST_ANALYSIS_PREPARER_JOB = "crm-post-analysis-preparer"
CRM_RUN_JOB_NAMES = (
    CRM_FIRST_READ_PREPARER_JOB,
    CRM_FIRST_READ_REVIEWER_JOB,
    CRM_POST_ANALYSIS_PREPARER_JOB,
    CRM_POST_ANALYSIS_REVIEWER_JOB,
)
CRM_SWEEP_JOB_NAME = "crm-results-overdue-sweep"
CRM_POST_ANALYSIS_HEADING = "After Split post-analysis published."
CRM_RUN_TIME_TOKEN = "<CRM_RUN_TIME>"


def render_crm_prompt(template: str, settings: Settings) -> str:
    """Put the CRM run's wall-clock time into a prompt template.

    Plain replacement, never `str.format`: the prompts carry literal ADF JSON braces.
    """
    run_time = f"{settings.crm_finalizer_hour:02d}:00 {settings.experiment_finalizer_timezone}"
    return template.replace(CRM_RUN_TIME_TOKEN, run_time)


_CALC_ENQUEUE = (
    f"{CALC_API_COMMAND} calculate <exp_id> --priority {FINALIZER_CALC_PRIORITY} "
    f"--source {FINALIZER_CALC_SOURCE} --wait --timeout 3300"
)

CRM_CALC_PROCEDURE = f"""\
Fresh calculation procedure (calculator API queue):
- Use the `ug-experiment-calculator` skill and calculate through the calculator API queue. Never
  call `calculate_exp_info(...)` in-process in this job: a direct run bypasses the queue and can
  read the shared subscription source tables while a queued calculation is rebuilding them.
  Enqueue with
  `{_CALC_ENQUEUE}`;
  on this server it reaches the API through the corporate bridge. A higher priority puts the job
  ahead of queued ones, but a running calculation is never pre-empted, so it may wait first.
  Exit 0 means the calculation succeeded; exit 3 means it is still queued or running — continue
  with `{CALC_API_COMMAND} wait <request_id> --timeout 3300` until it ends, for at most
  {FINALIZER_CALC_MAX_HOURS} hours in total. If the enqueue answer says `"duplicate": true`, the
  experiment is already queued or running: wait for that request id, do not enqueue another.
- Before importing clickhouse-worker for verification reads, disable clickhouse-connect server
  sessions with `clickhouse_connect.common.set_setting("autogenerate_session_id", False)`, and use
  the standard `ExperimentCalculatorConfig.from_env()` configuration with the
  `ug_monetization_sloperator_` table prefix. This queued calculation is explicitly authorised for
  this scheduled job, including its documented writes and subscription-source refresh.
- Calculate every send of the unit (one queued calculation per send id). Wait for each calculation
  to finish and verify fresh successful rows in every expected result/stat table before
  continuing. Do not silently use stale results. If a calculation fails, is lost, runs past the
  limit, or the API or the bridge is unreachable, stop without publishing partial results and
  report the exact failure.
"""

_CRM_IDENTITY_RULES = """\
   a. Allowlist: start exclusively from `ug_experiment_calculator.get_crm_exps_list(config=cfg)`.
      Treat the returned ids as authoritative; if the list cannot be read or verified, fail closed
      with no eligible unit. CRM sends are imported admin rows
      `Imported experiment [BLOOMREACH/<hex>]`; require `provider_id = 1` on the admin row.
   b. Identity: the admin `jira_task` must name a Jira epic (a key or a browse URL) whose summary
      starts with `[UG][CRM]`. An empty or non-epic `jira_task`, or another summary prefix, makes
      the send ineligible: never guess the epic from the campaign name. All CRM sends whose
      `jira_task` names the same epic (the main send, a last-chance resend) form one CRM send
      unit; the unit takes the place of "one experiment" everywhere in these rules, and its
      experiment id in every terminal line is the id of its latest send.
"""

_CRM_RESULTS_TASK_RULE = """\
   d. Results task: the unit's Results task is the epic's single child task whose summary is
      `Experiment results`. None, or more than one, makes the unit ineligible.\
"""

_CRM_PAGE_RULE = """\
   e. Project page and iteration: CRM rows carry no project link, so the project page is the epic's
      unique Confluence remote link. Request `/rest/api/3/issue/<EPIC-KEY>/remotelink` explicitly:
      the Jira UI's `Confluence content` / `mentioned in` block is a remote link and is not returned
      by the normal issue fields, issue links, description, or comments. Accept the unique
      Confluence `object.url` whose application type is `com.atlassian.confluence`; its `globalId`
      `pageId` is an equivalent fallback. Fetch only that page; never search or enumerate
      Confluence. If the unit has a Results task but no unique project page, fail with the
      experiment id, the Results task key and the epic key. A CRM unit is always `Iteration 1`.\
"""

CRM_SELECTION_RULES = f"""\
CRM first-read selection rules (CRM send units only; UG monetisation experiments are not in scope):
1. Identity:
{_CRM_IDENTITY_RULES}{_CRM_RESULTS_TASK_RULE} Only Backlog or To Do may enter the pool; In
      Progress, In Review, Done or any other status excludes the unit. The task may be assigned to
      a CRM team member; that is not a reason to skip it.
{_CRM_PAGE_RULE}
      The unit is already finalised when that page's `Results` section holds filled CRM tables for
      these sends; a template with `TBU` placeholders is not populated.
   g. Re-check: immediately before any write, re-fetch the CRM allowlist, the admin `jira_task`,
      the epic summary, the Results task status and the send windows; if the unit is no longer
      eligible, make no writes and return exactly `{NO_OP_NOTIFICATION}`.
2. Keep only sends that have at least one configured segment in the admin.
3. Timing: the admin date_start, date_end and status of a CRM row are not the send (rows stay
   running for months). Read each send's window from the calculator: `get_experiment(<id>,
   config=cfg)` returns `crm_send_ts` (the mass send) and `date_end` (the last `AB Test Start`).
   The unit is eligible when the latest send's `date_end` lies within the last calendar month and
   every send of the unit has `date_end` at least 72 hours (3 * 24 hours) ago, that is, every send's
   attribution window has closed (purchases count until `date_end` plus 3 days). An exact threshold
   value passes; a missing or unverifiable window is ineligible.
4. No pending-trials gate applies to the first read. Pending charges and the immaturity of Refunds
   14d and Churn 14d are reported as a caveat in Insights, never as a reason to exclude a unit.
5. Order the remaining units oldest first by the latest send's `date_end`, with the experiment id
   as the tie-breaker, and take the oldest one. If no unit remains, return exactly
   `{NO_OP_NOTIFICATION}` plus only the report line the upcoming-queue forecast allows. Keep
   filter/audit details internal.
"""

CRM_UPCOMING_QUEUE_FORECAST_POLICY = f"""\
Upcoming-queue forecast for CRM units (a dated Jira annotation on the next Results task to take):
- Purpose: when no Results task is started today, the nearest CRM unit a later scheduled CRM run
  will take still gets a dated forecast in Jira, so its author knows when to expect the first read.
- Queue: every CRM unit that passes identity rules a, b and d (its `Experiment results` task is in
  Backlog or To Do) and whose latest send either has not ended yet or ended within the last
  calendar month. This forecast never calculates.
- Earliest eligibility of a unit is its latest send's `date_end` plus 72 hours (the attribution
  window). A unit whose latest send has no verifiable `date_end` gets no date and no write.
- Take date: simulate the scheduled CRM runs at {CRM_RUN_TIME_TOKEN}, weekdays only, from the
  next one on. Each run takes exactly one unit: the oldest unit (by latest send's `date_end`)
  whose earliest eligibility has passed. The first unit the simulation takes is the nearest one,
  and its run date is its forecast date. Annotate only that unit's Results task.
- Before writing, re-fetch the task with status, `customfield_10312`, `duedate`, and all comments
  through `{JIRA_HELPER}` with `--as-bot` (`comments <KEY> --as-bot --limit 1000 --json`, count
  equal to `total`; incomplete → no writes) and re-verify Backlog or To Do. Read the date in the
  newest comment starting `{MATURITY_FORECAST_COMMENT_PREFIX}`. Add a new comment only when none
  exists, when that date is earlier than today, or when the new forecast date is later than it;
  otherwise report `comment: exists`.
- Dates, best effort and never blocking the comment: set Start date (`set-start-date <KEY>
  --as-bot --date YYYY-MM-DD --replace`) when it is empty or earlier than the forecast date, and
  Due date (`set-due-date`) when it differs; never move a Start date that is later than the
  forecast date.
- The comment is short Russian ADF via `add-comment <KEY> --as-bot --adf-file <file>`, opening
  with a native mention of the epic's reporter (assignee when the reporter is missing; never an
  invented account id), then `{MATURITY_FORECAST_COMMENT_PREFIX} YYYY-MM-DD.` and one sentence:
  "по текущей оценке возьму в работу в этот день", followed by what sets the date — the send's
  attribution window ends <date>, or <n> units ahead in the queue. Re-fetch the comments to verify
  it.
- These writes do not authorise assignment, a status transition, a calculation, Confluence edits,
  or any other Jira edit. Report the result as one line after the terminal line:
  `{QUEUE_FORECAST_RESULT_PREFIX} <experiment id or none> | <Results task key or -> | <forecast
  YYYY-MM-DD or none> | <comment: added|exists|failed|none> | <dates: set|unchanged|kept-later|
  failed|none> | <what sets the date, or why there is no forecast>`
  It is never posted to Slack.
"""

CRM_FIRST_READ_START_PROMPT = f"""[claude]
This is the authorised reviewer start pass for one CRM send unit first read (CRM finalizer, pass 1).
{AUTOMATED_SESSION_REPOSITORY_POLICY}
{AUTOMATED_ATLASSIAN_IDENTITY}
{AUTOMATED_RESPONSE_STYLE}
{LOCAL_CONFLUENCE_USER_DIRECTORY}
{REVIEWER_OWNERSHIP_POLICY}

Select exactly one CRM send unit using all of these gates before any finalisation or publication
write. The upcoming-queue forecast metadata exception is defined separately below:
{CRM_SELECTION_RULES}
{CRM_UPCOMING_QUEUE_FORECAST_POLICY}
Apply the upcoming-queue forecast only when this pass ends without starting a Results task.

{CRM_CALC_PROCEDURE}
Calculate the selected unit's sends once before starting its task, so the preparation pass reuses
fresh rows; this calculation is not an eligibility gate for the first read.

Only after a unit passes every gate, start its exact Results task:
{REVIEWER_START_POLICY}
Do not generate or publish Results, Insights, Decision, or Next steps in this pass.
Return exactly one terminal line after the verified start:
FINALIZATION_STARTED: <latest send id> | <project page URL> | Iteration 1 | <Results task key>
Return exactly {NO_OP_NOTIFICATION} if none qualify. On failure return exactly
Experiment finalisation failed: <reason>.
In this pass, "nothing else" after the terminal line still permits the one
`{QUEUE_FORECAST_RESULT_PREFIX}` line of the upcoming-queue forecast, and nothing more.
"""

_CRM_PUBLISH_GATE = """\
   Save the fetched pre-edit storage body as the publication baseline and run the repository
   Confluence publish gate on the exact block being added or changed. The new block must pass every
   blocking check. If, and only if, every full-page blocking finding is byte-for-byte present in the
   baseline and outside every added or modified region, prefix the `update-storage` command with
   `UG_PUBLISH_GATE_BASELINE=<pre-edit-storage-file>`. The hook then exempts only the matching
   baseline findings and still blocks every newly introduced defect. Never use
   `UG_PUBLISH_GATE_DISABLED`. This narrow exception never permits a defect in this run's block,
   changed surrounding content, invalid XML, a missing baseline, or removal of unrelated content.\
"""

_CRM_NO_ARTIFACT_PATHS = """\
   Never put local/server paths or links to logs, SQL, scripts, CSVs, ZIPs, or other run artifacts
   into the project-page body. Package useful reader-safe analysis artifacts into one bundle and
   upload it as an attachment to the existing project page instead. Verify the attachment upload.\
"""

CRM_FIRST_READ_PROMPT = f"""\
[claude]
This is the authorised CRM first-read preparation pass (CRM finalizer, pass 1). Complete the
calculation and Confluence publication in this turn. The user explicitly pre-approves progression
through all three publication stages (Results, then Insights, then Decision / Next steps),
including the required Confluence update. All Jira writes belong exclusively to the reviewer. Do
not pause to request approval between stages; keep all data-quality, verification, language, and
publication safeguards of the skills.

{AUTOMATED_SESSION_REPOSITORY_POLICY}

{AUTOMATED_ATLASSIAN_IDENTITY}

{AUTOMATED_RESPONSE_STYLE}

{LOCAL_CONFLUENCE_USER_DIRECTORY}

Goal: publish the first read of exactly one CRM send unit.

{CRM_SELECTION_RULES}
These rules describe selection BEFORE the reviewer claims a task. This preparation pass receives
an already selected and started unit: In Progress on its exact Results task is expected and does
not make it ineligible. Do not walk the pool or select another unit. Revalidate its identity,
segments and send windows; fail if any gate changed. Reuse this run's verified fresh calculation;
recalculate only if freshness cannot be established.
Fetch only the authoritative project page ID supplied below. Do not search, enumerate, fetch, or
publish another Confluence page. If it does not match the unit and task, fail explicitly.

{CRM_CALC_PROCEDURE}
Execution for the selected unit:
1. Work only on the exact unit, page and Results task supplied by the reviewer. Re-fetch and verify
   the task's In Progress status, bot assignee, and Start date. If the start cannot be verified,
   fail without Jira writes. Never substitute another unit.
{WORKER_JIRA_POLICY}
2. State the sends (ids, send dates, attribution-window ends), segments, the target project page
   and the configured table prefix.
3. Run the Results → Insights → Decision / Next steps pipeline in this exact order:
   a. For every send of the unit, oldest first, generate its first-read block with
      `ug_experiment_calculator.get_experiment_confluence_report_code(<exp_id>)` (the CRM team's
      template: header, Decision, Next steps, Significance analysis with Email campaign, Landing
      funnel when present and After Click, Insights). After Split is not generated in the first
      read: it is published later as a separate post-analysis, so never add an After Split table
      yourself. Each block names its send date and experiment id. Put the blocks into the page's
      `Results` section in place of its `TBU` template; keep the section's own heading. The
      finished-experiment Forecast/audience workflow and product metrics do not apply.
   b. Use the CRM section of `experiment-insight-research` for evidence-backed Insights (concurrent
      sends to the same recipients inside the 3-day window, link scanners, the landing funnel by
      device). Report pending charges and the immaturity of Refunds 14d and Churn 14d as a caveat.
   c. Only after insights are present, write concise Decision and Next steps grounded in them.
   Do not create a separate Confluence child page. Preserve unrelated page content. Confluence
   content must be English.
{_CRM_PUBLISH_GATE}
4. Fetch the project page again after the write. Verify that every send's block contains non-empty
   Results, Insights, Decision, and Next steps, that storage XML is valid, and that no unrelated
   block disappeared. If verification fails, report failure and do not announce success.
{_CRM_NO_ARTIFACT_PATHS}
5. Leave every Jira write and every user-facing comment to the reviewer. Return a private handoff
   only; do not send Slack yourself.

Preparation result:
- After Confluence verification, return
  `FINALIZATION_PREPARED: <latest send id> | <project page URL> | Iteration 1` as the exact first
  line, followed by the private evidence handoff the reviewer needs. Return `{NO_OP_NOTIFICATION}`
  if the unit is no longer eligible, or a concise `Experiment finalisation failed: <reason>` line
  on failure.

Use the current date/time in Asia/Nicosia for all relative-date decisions.
Never finalise more than one CRM send unit in this run.
"""

CRM_FIRST_READ_REVIEW_PROMPT = f"""\
[claude]
This is the authorised independent review pass for one prepared CRM send unit first read.
{AUTOMATED_RESPONSE_STYLE}
{AUTOMATED_SESSION_REPOSITORY_POLICY}
{AUTOMATED_ATLASSIAN_IDENTITY}
{LOCAL_CONFLUENCE_USER_DIRECTORY}
{REVIEWER_OWNERSHIP_POLICY}

Review only the exact unit, project page, and iteration supplied below. Re-fetch the page and
verify that every send's first-read block holds Results, Insights, Decision, and Next steps,
complete and valid, and that no After Split table was added. Do not search, enumerate, fetch, or
publish any other Confluence page. If the supplied page is not the matching project page, fail
instead of searching for a replacement.
Use the repository Jira helper with `--as-bot` for every Jira command. Resolve the service
account from `/myself` (`{SERVICE_ACCOUNT_ID}`). Re-fetch the exact Results task's current
status and available transitions before any status change:
- From `Backlog` or `To Do`, the intermediate transition ID `281` to `In Progress` is explicitly
  authorised for recovery of a missing preparation start. Execute it and re-fetch to verify
  `In Progress` before attempting `181`. Do not ask a human to make this authorised transition.
- From `In Progress`, use transition ID `181` to `In Review` after the completion writes below.
- If already `In Review` or `Done`, do not move it backwards or repeat a transition. Verify the
  existing publication and avoid duplicate comments; preserve a completed task's fields.
- For another status or an unavailable required transition, stop with a concise failure stating
  the observed status and missing transition. Do not infer a permissions problem from HTTP 400.
For a task still awaiting completion, assign it to the service account, preserve an existing
`Start date` (`customfield_10312`) or fill it with today's `YYYY-MM-DD` date if missing, and set
`Due date` (`duedate`) to today. Reuse an existing matching English publication comment instead
of adding a duplicate; otherwise add the short publication comment. Re-fetch and verify the
fields and comment, perform `181` only from verified `In Progress`, and re-fetch to verify
`In Review`. Return the final Slack notification in the exact production format below.
{FINAL_NOTIFICATION_POLICY} Do not send Slack yourself. On any failure return exactly
`Experiment finalisation failed: <reason>`.

Preparation result:
{{prepared_result}}
"""

CRM_POST_ANALYSIS_RULES = f"""\
CRM After Split post-analysis selection rules (CRM send units whose first read is published):
1. Identity:
{_CRM_IDENTITY_RULES}{_CRM_RESULTS_TASK_RULE} The first read must already be published: the task
      must be `In Review` or `Done`. Any other status excludes the unit.
{_CRM_PAGE_RULE}
      The page's `Results` section must hold the first-read block of every send of the unit, and
      at least one send's block must have no `Post-analysis` heading yet. A unit whose every send
      already holds its post-analysis is skipped.
2. Timing: read each send's window from `get_experiment(<id>, config=cfg)` (`date_end` = the last
   `AB Test Start`). The latest send's `date_end` must lie within the last calendar month, and
   every send of the unit must have `date_end` at least 7 complete days (7 * 24 hours) ago. An
   exact threshold value passes; a missing or unverifiable window is ineligible.
3. Pending-trials gate, strict and fail-closed, on a fresh calculation through the calculator API
   queue for every send: evaluate client `UG_CRM`, segment `CRM purchases`, across all variation
   rows. It passes when either (a) `pending trials, %` is present and strictly below 5% in every
   variation, or (b) fresh `pending_trial_cnt` and `access_trial_cnt` rows are present for every
   variation and the sum of `pending_trial_cnt` across those variations is strictly below
   {PENDING_TRIALS_SMALL_COUNT_LIMIT}. This small-count exception is an absolute tolerance, not a
   replacement percentage; a total equal to {PENDING_TRIALS_SMALL_COUNT_LIMIT} is not exempt.
   Missing, null, stale, failed, or unverifiable counts exclude the unit. Pending charges are not
   an eligibility condition. Never decide eligibility from stale or partial rows.
4. Order the eligible units oldest first by the latest send's `date_end`, with the experiment id
   as the tie-breaker, and take only the oldest one: at most one unit per run. If none qualifies,
   return exactly `{NO_OP_NOTIFICATION}` and nothing else.
5. Selection makes no forecast comments and no Jira writes of any kind. Immediately before any
   write, re-check identity, the task status, the windows and the gate against the same fresh
   calculation; if the unit is no longer eligible, make no writes and return exactly
   `{NO_OP_NOTIFICATION}`. Keep filter/audit details internal.
"""

CRM_POST_ANALYSIS_START_PROMPT = f"""[claude]
This is the authorised reviewer start pass for one CRM After Split post-analysis (CRM finalizer,
pass 2).
{AUTOMATED_SESSION_REPOSITORY_POLICY}
{AUTOMATED_ATLASSIAN_IDENTITY}
{AUTOMATED_RESPONSE_STYLE}
{LOCAL_CONFLUENCE_USER_DIRECTORY}

Select exactly one CRM send unit using all of these gates:
{CRM_POST_ANALYSIS_RULES}
{CRM_CALC_PROCEDURE}
Do not change the Results task status, assignee or any field in this pass. Do not add comments,
do not edit Confluence, and do not generate the post-analysis in this pass.
Return exactly one terminal line for the selected unit:
FINALIZATION_STARTED: <latest send id> | <project page URL> | Iteration 1 | <Results task key>
Return exactly {NO_OP_NOTIFICATION} if none qualify. On failure return exactly
Experiment finalisation failed: <reason>.
Return nothing else.
"""

CRM_POST_ANALYSIS_PROMPT = f"""\
[claude]
This is the authorised CRM After Split post-analysis preparation pass (CRM finalizer, pass 2).
Complete the Confluence publication in this turn without pausing for approval; keep all
data-quality, verification, language, and publication safeguards of the skills. All Jira writes
belong exclusively to the reviewer.

{AUTOMATED_SESSION_REPOSITORY_POLICY}

{AUTOMATED_ATLASSIAN_IDENTITY}

{AUTOMATED_RESPONSE_STYLE}

{LOCAL_CONFLUENCE_USER_DIRECTORY}

Goal: add the After Split post-analysis to exactly one CRM send unit whose first read is published.

{CRM_POST_ANALYSIS_RULES}
These rules describe selection by the reviewer. This preparation pass receives an already selected
unit. Do not walk the pool or select another unit. Revalidate its identity, the task status, the
windows and the pending-trials gate; fail if any gate changed. Reuse this run's verified fresh
calculation; recalculate only if freshness cannot be established.
Fetch only the authoritative project page ID supplied below. Do not search, enumerate, fetch, or
publish another Confluence page. If it does not match the unit and task, fail explicitly.

{CRM_CALC_PROCEDURE}
{WORKER_JIRA_POLICY}
Execution for the selected unit:
1. For every send of the unit that has no post-analysis yet, oldest first, generate its block with
   `ug_experiment_calculator.get_crm_after_split_post_analysis_code(<exp_id>)` (heading
   Post-analysis, header line, the After Split expand on segment `CRM purchases`, an empty
   `Post-analysis summary`). Insert it inside that send's existing first-read block, after its
   Insights.
2. Fill each `Post-analysis summary` with a short evidence-based text: whether After Split agrees
   with After Click, and the caveats, including the small-count exception of the pending-trials
   gate when it was used (client + segment, total pending count, total access-trial count, maximum
   variation share, the {PENDING_TRIALS_SMALL_COUNT_LIMIT}-trial threshold). Confluence content
   must be English.
3. Change nothing else on the page: the first read's header, Decision, Next steps, Significance
   analysis and Insights stay byte-unchanged, and unrelated page content is preserved.
{_CRM_PUBLISH_GATE}
4. Fetch the project page again after the write. Verify that every send holds its post-analysis
   block after its Insights, that storage XML is valid, and that nothing else changed. If
   verification fails, report failure and do not announce success.
{_CRM_NO_ARTIFACT_PATHS}
5. Leave every Jira write and every user-facing comment to the reviewer. Return a private handoff
   only; do not send Slack yourself.

Preparation result:
- After Confluence verification, return
  `FINALIZATION_PREPARED: <latest send id> | <project page URL> | Iteration 1` as the exact first
  line, followed by the private evidence handoff the reviewer needs. Return `{NO_OP_NOTIFICATION}`
  if the unit is no longer eligible, or a concise `Experiment finalisation failed: <reason>` line
  on failure.

Use the current date/time in Asia/Nicosia for all relative-date decisions.
Never post-analyse more than one CRM send unit in this run.
"""

CRM_POST_ANALYSIS_REVIEW_PROMPT = f"""\
[claude]
This is the authorised independent review pass for one prepared CRM After Split post-analysis.
{AUTOMATED_RESPONSE_STYLE}
{AUTOMATED_SESSION_REPOSITORY_POLICY}
{AUTOMATED_ATLASSIAN_IDENTITY}
{LOCAL_CONFLUENCE_USER_DIRECTORY}
{REVIEWER_OWNERSHIP_POLICY}

Review only the exact unit and project page supplied below. Re-fetch the page and verify that every
send of the unit holds its post-analysis block (heading Post-analysis, the After Split expand, a
non-empty `Post-analysis summary`) placed after its Insights, and that the first read's other
content is unchanged. Do not search, enumerate, fetch, or publish any other Confluence page. If the
supplied page is not the matching project page, fail instead of searching for a replacement.
Use the repository Jira helper with `--as-bot` for every Jira command. Do not change the Results
task status or any field: no transition, assignee, date, or other field write. Add exactly one
publication comment to the Results task: a short English note that the After Split post-analysis
was added to the project page, with its main conclusion. Before adding it, read all comments
(`comments <KEY> --as-bot --limit 1000 --json`, count equal to `total`); when a matching
post-analysis publication comment already exists, reuse it and add none. Re-fetch the comments to
verify it.

Final notification (reviewer only):
- Return exactly one Slack-facing notification as your final response; Sloperator publishes it.
  Do not send Slack yourself and do not post any kickoff, progress, or validation message.
- Start with exactly one compact heading sentence on one line in this shape:
  `[<project title>](<project page URL>) — experiment
  [<id>](https://www.ultimate-guitar.com/components/ab/experiment/view?id=<id>),
  Iteration 1. {CRM_POST_ANALYSIS_HEADING}`
  where `<id>` is the latest send's id. Do not print raw URLs.
- Add at most two extremely short bullets with the most important conclusions.
- Mention every distinct person listed in the project-page header table under DRI / Project owner
  and Team. Resolve Slack user ids and use real `<@USERID>` mentions; never invent ids. If a person
  cannot be resolved inside the agent, use their exact plain display name.
- After the heading, mentions, and at most two bullets, stop: no Jira key, audit, file paths, or
  appendix.
- For a no-eligible path return exactly `{NO_OP_NOTIFICATION}`. On any failure return exactly
  `Experiment finalisation failed: <reason>`.

Preparation result:
{{prepared_result}}
"""

CRM_OVERDUE_SWEEP_POLICY = f"""\
Overdue CRM Results sweep (runs after the daily CRM finalizer run has finished):
- Scope: every `Experiment results` task assigned to the service account whose Due date is today
  or earlier, plus the tasks this sweep parked earlier by clearing their dates. Fetch them with
  `{JIRA_HELPER} search --as-bot --json --max-results 200 --fields
  summary,status,assignee,parent,duedate,customfield_10312 --jql '<the JQL supplied below>'`.
  A task with an empty Due date is in scope only when the newest of its comments that starts with
  `{MATURITY_FORECAST_COMMENT_PREFIX}`, `{MATURITY_UNKNOWN_COMMENT_PREFIX}` or
  `{WAITING_COMMENT_PREFIX}` starts with `{WAITING_COMMENT_PREFIX}`; skip every other undated task
  without writing anything and without a report line. Never touch a task outside this scope, and
  never one whose status is not Backlog or To Do.
- For each task, read it with all comments (`comments <KEY> --as-bot --limit 1000 --json`, count
  equal to `total`; an incomplete response means no writes for that task) and map it to its CRM
  send unit with identity rules a, b and d of the CRM selection rules (its parent `[UG][CRM]` epic,
  the allowlisted sends whose admin `jira_task` names that epic, the epic's single `Experiment
  results` task). A task that cannot be mapped unambiguously gets no writes; report it as
  `skipped` with the reason.
- Classify the unit: `not-started` when no send of the epic exists yet, `running` when the latest
  send has no `date_end` yet, `ended` otherwise.
- Not started or running — the task waits for the send:
  1. Clear both dates when set: `set-start-date <KEY> --as-bot --clear` and
     `set-due-date <KEY> --as-bot --clear`. They re-read the fields; verify both are empty.
  2. Comment only once per wait: when the newest comment carrying one of the three prefixes above
     already starts with `{WAITING_COMMENT_PREFIX}`, write nothing (`comment: exists`). Otherwise
     add a short Russian ADF comment (`add-comment <KEY> --as-bot --adf-file <file>`) that opens
     with a native mention of the epic's reporter (assignee when the reporter is missing; never an
     invented account id), then `{WAITING_COMMENT_PREFIX}` and says plainly that the CRM send has
     not ended yet, that the start and due dates are removed, and that the dates will be set again
     automatically once the send has ended. Re-fetch the comments to verify it.
- Ended — the task gets a dated forecast:
  1. Earliest eligibility is the latest send's `date_end` plus 72 hours. Simulate the scheduled CRM
     runs at {CRM_RUN_TIME_TOKEN}, weekdays only, from the next run after now — today's run has
     already happened — over the whole CRM upcoming queue, one unit per run, oldest first. The run
     on which the simulation takes this unit is its date. Never calculate in this sweep.
  2. Set both dates to that date: `set-start-date <KEY> --as-bot --date YYYY-MM-DD --replace` and
     `set-due-date <KEY> --as-bot --date YYYY-MM-DD`; verify them. A failed date write is reported
     and does not stop the comment.
  3. Add the standard forecast comment unless the newest `{MATURITY_FORECAST_COMMENT_PREFIX}`
     comment already names the same date (`comment: exists`): short Russian ADF, the same epic
     mention rule, then `{MATURITY_FORECAST_COMMENT_PREFIX} YYYY-MM-DD.` and one sentence "по
     текущей оценке возьму в работу в этот день" followed by what sets the date — the attribution
     window ends <date>, or <n> units ahead in the queue. Re-fetch the comments to verify it.
- These writes do not authorise assignment, a status transition, a calculation, Confluence edits,
  Slack messages, or any other Jira edit.
- Report one line per task in scope, in exactly this shape:
  `{SWEEP_RESULT_PREFIX} <task key> | <experiment id or -> | <waiting|dated|skipped|failed> |
  <date YYYY-MM-DD or -> | <comment: added|exists|none|failed> | <dates:
  set|cleared|unchanged|failed> | <short reason>`
  then exactly one terminal line `{SWEEP_DONE_PREFIX}: <number of tasks in scope>`. If the task list
  itself cannot be read, return exactly one line `{SWEEP_FAILED_PREFIX} <reason>`.
"""

CRM_OVERDUE_SWEEP_PROMPT = f"""\
[claude]
This is the authorised automated overdue CRM Results sweep of the UG CRM finalizer. The user
explicitly authorises the Jira comments and Start/Due date changes defined below on the
`Experiment results` tasks assigned to the service account, without asking for approval.
{AUTOMATED_SESSION_REPOSITORY_POLICY}
{AUTOMATED_ATLASSIAN_IDENTITY}
{AUTOMATED_RESPONSE_STYLE}

Use the repository Jira helper with `--as-bot` for every Jira command; the service account is
`{SERVICE_ACCOUNT_ID}`. The CRM selection rules and the CRM upcoming-queue forecast define the
mapping, gates and queue this sweep relies on:
<CRM_SELECTION_RULES>
<CRM_QUEUE_POLICY>
{CRM_OVERDUE_SWEEP_POLICY}
Use the current date/time in Asia/Nicosia. Today is <CRM_TODAY>. JQL:
<CRM_JQL>
"""


def crm_overdue_sweep_jql(today: dt.date) -> str:
    """CRM `Experiment results` tasks of the service account, overdue or parked."""
    return (
        f'project = UMN AND assignee = "{SERVICE_ACCOUNT_ID}" '
        'AND summary ~ "\\"Experiment results\\"" '
        'AND status in (Backlog, "To Do") '
        f'AND (duedate <= "{today.isoformat()}" OR duedate is EMPTY) ORDER BY duedate ASC, key ASC'
    )


def crm_overdue_sweep_prompt(today: dt.date, settings: Settings) -> str:
    prompt = CRM_OVERDUE_SWEEP_PROMPT
    for token, value in (
        ("<CRM_SELECTION_RULES>", CRM_SELECTION_RULES),
        ("<CRM_QUEUE_POLICY>", CRM_UPCOMING_QUEUE_FORECAST_POLICY),
        ("<CRM_TODAY>", today.isoformat()),
        ("<CRM_JQL>", crm_overdue_sweep_jql(today)),
    ):
        prompt = prompt.replace(token, value)
    return render_crm_prompt(prompt, settings)


def crm_first_read_flow(settings: Settings) -> FinalizerFlow:
    return FinalizerFlow(
        render_crm_prompt(CRM_FIRST_READ_START_PROMPT, settings),
        render_crm_prompt(CRM_FIRST_READ_PROMPT, settings),
        CRM_FIRST_READ_REVIEW_PROMPT,
        CRM_FIRST_READ_REVIEWER_JOB,
        CRM_FIRST_READ_PREPARER_JOB,
        SUCCESS_HEADING,
        False,
        True,
    )


def crm_post_analysis_flow(settings: Settings) -> FinalizerFlow:
    return FinalizerFlow(
        render_crm_prompt(CRM_POST_ANALYSIS_START_PROMPT, settings),
        render_crm_prompt(CRM_POST_ANALYSIS_PROMPT, settings),
        CRM_POST_ANALYSIS_REVIEW_PROMPT,
        CRM_POST_ANALYSIS_REVIEWER_JOB,
        CRM_POST_ANALYSIS_PREPARER_JOB,
        CRM_POST_ANALYSIS_HEADING,
        False,
        False,
    )


@dataclass(frozen=True)
class CrmRunOutcome:
    first_read: str
    post_analysis: str
    sweep: str


async def run_crm_once(
    client: AsyncWebClient,
    agent: AgentSubmitter,
    settings: Settings,
) -> CrmRunOutcome:
    """Pass 1 (first read), pass 2 (post-analysis), then the CRM sweep; each always runs."""
    results: list[str] = []
    for flow in (crm_first_read_flow(settings), crm_post_analysis_flow(settings)):
        try:
            result = await experiment_finalizer.run_once(client, agent, settings, flow=flow)
        except Exception as error:
            LOGGER.exception("CRM finalizer pass %s failed", flow.reviewer_job)
            result = f"Experiment finalisation failed: {error}"
        results.append(result)
    try:
        sweep = await experiment_finalizer.sweep_overdue_results(
            agent,
            settings,
            job_name=CRM_SWEEP_JOB_NAME,
            prompt_builder=lambda today: crm_overdue_sweep_prompt(today, settings),
        )
    except Exception as error:
        LOGGER.exception("CRM overdue Results sweep failed")
        sweep = f"{SWEEP_FAILED_PREFIX} {error}"
    first_read, post_analysis = results
    LOGGER.info("CRM first read: %s", first_read)
    LOGGER.info("CRM After Split post-analysis: %s", post_analysis)
    LOGGER.info("CRM overdue Results sweep: %s", sweep)
    return CrmRunOutcome(first_read, post_analysis, sweep)


async def run_crm_daily(
    client: AsyncWebClient,
    agent: AgentSubmitter,
    settings: Settings,
    enabled: Callable[[], bool] = lambda: True,
    slots: SlotRunner | None = None,
) -> None:
    """Run forever at the CRM finalizer hour on weekdays."""
    slots = slots or SlotRunner(CRM_JOB_NAME)
    while True:
        target = await slots.next_slot(
            lambda now: next_run_at(
                now,
                settings.experiment_finalizer_timezone,
                settings.crm_finalizer_hour,
            ),
            LOGGER,
            "Next CRM finalizer run scheduled for %s",
        )
        if target is None:
            continue
        if not enabled():
            LOGGER.info("Scheduled CRM finalizer run disabled from admin")
            continue
        run_task: asyncio.Task[CrmRunOutcome] | None = None
        try:
            LOGGER.info("Starting scheduled CRM finalizer run")
            run_task = asyncio.create_task(
                run_crm_once(client, agent, settings),
                name="scheduled-crm-finalizer-run",
            )
            await run_task
            LOGGER.info("CRM finalizer run completed")
        except asyncio.CancelledError:
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling():
                if run_task is not None:
                    run_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await run_task
                raise
            LOGGER.info("Scheduled CRM finalizer run cancelled from admin")
        except Exception:
            LOGGER.exception("Could not complete the CRM finalizer run")


async def recover_interrupted_crm_runs(
    client: AsyncWebClient,
    agent: AgentOrchestrator,
    store: EventStore,
    settings: Settings,
) -> None:
    """Continue CRM finalizer runs a service restart interrupted, as main.py does for UGM."""
    for flow in (crm_first_read_flow(settings), crm_post_analysis_flow(settings)):
        await _recover_reviewer_runs(client, agent, store, settings, flow)
        await _recover_preparer_runs(client, agent, store, settings, flow)
    await experiment_finalizer.rerun_interrupted_overdue_sweep(
        agent,
        store,
        settings,
        job_name=CRM_SWEEP_JOB_NAME,
        prompt_builder=lambda today: crm_overdue_sweep_prompt(today, settings),
    )


async def _recover_reviewer_runs(
    client: AsyncWebClient,
    agent: AgentOrchestrator,
    store: EventStore,
    settings: Settings,
    flow: FinalizerFlow,
) -> None:
    heading = flow.success_heading
    recovered = await agent.resume_interrupted_headless(
        settings.experiment_finalizer_timeout_seconds,
        job_name=flow.reviewer_job,
        accept_result=lambda text: is_reviewer_result(text, heading),
    )
    for run in recovered:
        if split_maturity_forecasts(run.text)[0].startswith(NO_OP_PREFIX):
            # R15: a recovered CRM no-op is silent like a live one; it is never posted to Slack.
            LOGGER.info("Recovered %s run found no eligible CRM unit", flow.reviewer_job)
            if run.run_id is not None:
                await asyncio.to_thread(
                    store.finish_scheduled_agent_run,
                    run.run_id,
                    status="completed",
                    external_session_id=run.session_id,
                    result_text=run.text,
                )
            continue
        try:
            if STARTED_RE.fullmatch(run.text.strip()):
                preparation_exists = await asyncio.to_thread(
                    store.has_scheduled_agent_run_with_prompt_marker,
                    flow.preparer_job,
                    run.text.strip(),
                )
                if preparation_exists:
                    LOGGER.warning(
                        "Recovered %s start already has a preparation; skipping duplicate handoff",
                        flow.reviewer_job,
                    )
                else:
                    await experiment_finalizer.run_preparation_from_started_run(
                        client, agent, settings, run, flow=flow
                    )
            else:
                await experiment_finalizer.publish_run(client, agent, settings, run, flow=flow)
        except (InvalidFinalizationNotification, SelectionError) as error:
            LOGGER.error("Recovered %s run could not continue: %s", flow.reviewer_job, error)
            if run.run_id is not None:
                await asyncio.to_thread(
                    store.finish_scheduled_agent_run,
                    run.run_id,
                    status="failed",
                    external_session_id=run.session_id,
                    result_text=run.text,
                    last_error=repr(error),
                )
            conversation = await client.conversations_open(users=settings.slack_user_id)
            await client.chat_postMessage(
                channel=conversation["channel"]["id"],
                markdown_text=(
                    f"<@{settings.slack_user_id}> ⚠️ CRM finalizer остановлен: "
                    "восстановленный запуск не прошёл проверку или вернул нефинальный "
                    "результат. Сервис продолжает работать; задачу нужно запустить повторно."
                ),
            )
            continue
        if run.run_id is not None:
            await asyncio.to_thread(
                store.finish_scheduled_agent_run,
                run.run_id,
                status="completed",
                external_session_id=run.session_id,
                result_text=run.text,
            )


async def _recover_preparer_runs(
    client: AsyncWebClient,
    agent: AgentOrchestrator,
    store: EventStore,
    settings: Settings,
    flow: FinalizerFlow,
) -> None:
    recovered = await agent.resume_interrupted_headless(
        settings.experiment_finalizer_timeout_seconds,
        job_name=flow.preparer_job,
        accept_result=is_preparation_result,
    )
    for run in recovered:
        last_error: str | None = None
        try:
            await experiment_finalizer.run_review(client, agent, settings, run.text, flow=flow)
        except InvalidFinalizationNotification as error:
            status = "failed"
            last_error = repr(error)
        else:
            status = "completed"
        if run.run_id is not None:
            await asyncio.to_thread(
                store.finish_scheduled_agent_run,
                run.run_id,
                status=status,
                external_session_id=run.session_id,
                result_text=run.text,
                last_error=last_error,
            )
