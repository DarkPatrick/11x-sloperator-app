"""Daily autonomous finalisation of one recent monetisation experiment."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from typing import Protocol
from zoneinfo import ZoneInfo

from slack_sdk.web.async_client import AsyncWebClient

from sloperator.agents import HeadlessAgentRun
from sloperator.automated_session_policy import (
    AUTOMATED_ATLASSIAN_IDENTITY,
    AUTOMATED_RESPONSE_STYLE,
    AUTOMATED_SESSION_REPOSITORY_POLICY,
    LOCAL_CONFLUENCE_USER_DIRECTORY,
)
from sloperator.config import Settings
from sloperator.experiment_agent_tools import agent_instructions
from sloperator.experiment_design_selector import (
    JiraRestReader,
    SelectionError,
    confluence_page_ids,
    resolve_project_page_id,
)
from sloperator.jira_agent_policy import (
    REVIEWER_OWNERSHIP_POLICY,
    REVIEWER_START_POLICY,
    WORKER_JIRA_POLICY,
)
from sloperator.project_page_people import ensure_header_mentions
from sloperator.team_channel import channel_for_task

LOGGER = logging.getLogger(__name__)
NO_OP_PREFIX = "No eligible experiment"
NO_OP_NOTIFICATION = "No eligible experiment was found for calculation today."
FAILURE_PREFIXES = (
    "Experiment finalisation failed:",
    "Experiment finalization failed:",
)
TRANSIENT_CLICKHOUSE_RETRY_DELAYS = (30, 90, 180)
PENDING_TRIALS_SMALL_COUNT_LIMIT = 10
MATURITY_FORECAST_COMMENT_PREFIX = "Прогноз готовности к итогам:"
MATURITY_FORECAST_RESULT_PREFIX = "MATURITY_FORECAST:"
QUEUE_FORECAST_RESULT_PREFIX = "QUEUE_FORECAST:"
MATURITY_UNKNOWN_COMMENT_PREFIX = "Итоги пока не взяты в работу:"
MATURITY_UNKNOWN_REPEAT_DAYS = 7
CALC_API_COMMAND = ".venv/bin/python .claude/skills/ug-experiment-calculator/scripts/calc_api.py"
# Above the API default (100) and cron (0): the finalizer's calculation goes first in the queue.
FINALIZER_CALC_PRIORITY = 500
FINALIZER_CALC_SOURCE = "sloperator:experiment-finalizer"
FINALIZER_CALC_MAX_HOURS = 3
TRANSIENT_CLICKHOUSE_RETRY_PROMPT = f"""[claude]
This is an orchestrator-authorised retry of the same experiment-finalizer turn after a transient
ClickHouse failure. {AUTOMATED_RESPONSE_STYLE}

Retry only the failed calculation for the exact same candidate. Reuse all successfully completed
selection and calculation evidence from this session; do not recalculate earlier candidates and do
not select a different candidate unless the original selection rules require continuing after this
candidate is conclusively ineligible. Re-enqueue the calculation through the calculator API queue
exactly as the original turn requires; never call `calculate_exp_info(...)` in-process. Run any
verification reads in a fresh Python process and disable
clickhouse-connect server sessions before importing clickhouse-worker or the calculator with
`clickhouse_connect.common.set_setting("autogenerate_session_id", False)`. This workflow uses
short-lived clients and package-managed physical tables, so it must not share a ClickHouse session
id across requests. Re-fetch and verify every expected row after the calculation. Do not treat rows
from the failed attempt as a successful calculation. Preserve the original no-write boundaries and
return the exact terminal format required by the original turn.
"""

SELECTION_RULES = f"""\
Selection rules:
1. Start exclusively from `ug_experiment_calculator.get_ugm_exps_list(config=cfg)`. Treat the
   returned ids as the authoritative allowlist: never inspect, calculate, select, or publish an
   experiment whose id is absent from that list. Do not use the general experiment list and then
   infer the domain from metrics, clients, segments, a project page, or other metadata. If the UGM
   allowlist lookup fails or cannot be verified, fail closed with no eligible experiment.
   As an independent sanity check, require the experiment title (case-insensitively) to contain
   `UG Monetization`, `UG Monetisation`, or the Russian stem `монетизац`. Absence of all
   three markers makes the experiment ineligible even when its id appears in the UGM allowlist.
   Apply both checks before inspecting project pages and before invoking the calculator.
2. Use the UG experiment admin/source of truth to find experiments whose actual experiment end
   timestamp is in the closed interval [now minus one calendar month, now]. Include only
   experiments that have already ended; exclude running, scheduled, paused without an actual
   end, or otherwise unfinished experiments.
3. Apply a strict, fail-closed post-stop age gate before invoking the calculator or reading any
   calculation rows. Compute elapsed age from the experiment's actual end timestamp to the current
   time: every app-only experiment must have been stopped for at least 8 complete days (8 * 24
   hours), while every experiment with a web client (`UG_WEB` or `UG_MOBWEB`) must have been stopped
   for at least 9 complete days (9 * 24 hours). For a mixed app+web experiment use the stricter
   9-day threshold. An exact threshold value passes. A younger experiment, or one with a missing,
   ambiguous, future, or unverifiable actual end timestamp/client classification, is ineligible and
   must not be calculated during this run.
4. Keep only experiments that have at least one configured segment in the admin.
5. Locate each experiment's project page and exclude it if the final Results/Итоги section for
   this experiment/iteration is already populated. Match the experiment id or its verified
   iteration to the specific results block. A generic Results heading, bandit analysis covering
   several runs, and results for an earlier experiment under the same project do not establish
   that this experiment's final results are complete. Do not mistake a template, empty
   placeholder, design table, or results for another iteration for completed итогов.
6. Locate the exact Jira Results/Итоги task for the matching experiment and iteration before any
   calculator invocation or calculation-row inspection. Exclude the experiment when that task is
   already In Progress, In Review, Done, or any other non-queued status; only a task in Backlog or
   To Do may enter the candidate pool. If the exact task or its status cannot be established, exclude
   the candidate and continue to the next experiment. When an epic contains multiple Results tasks,
   first map this experiment to its launch task using an explicit experiment-id link in the launch
   notification, task, or comment; then use the Results task from the same Jira iteration/cohort.
   Do not use an older Done Results task merely because it shares the epic or project title.
   A newer To Do task alone is not proof of a match either. Re-check the matched task's status
   immediately before any write.
7. The experiments remaining after rules 1-6 form the preliminary candidate pool. Order the whole
   pool by actual end timestamp, then by experiment id as a deterministic tie-breaker. If the pool
   is empty, stop immediately: do not invoke the calculator, do not inspect fallback experiments
   outside these rules, make no Confluence or Jira writes other than the forecast annotations
   this pass's own forecast policy defines, and return exactly `{NO_OP_NOTIFICATION}` plus only
   the report lines that policy allows.
8. Walk the ordered preliminary candidate pool from oldest to newest. For each candidate, obtain
   demonstrably fresh maturity data. Before recalculating, check whether the package already has a
   complete successful calculation from the current calendar date in Asia/Nicosia. Reuse it only
   when the latest calculation-log entry is successful, no later failed attempt or later partial
   table rewrite exists, the installed calculator commit and effective configuration have not
   changed, and every expected result/stat/funnel and raw-users table has complete coverage for the
   current clients, segments, variations, and actual end date. All table watermarks must belong to
   that successful calculation window. A same-day `updated_at` value by itself is not proof: mixed
   old/new rows or rows touched after the success marker make the cache unverifiable. If all these
   checks pass, use those rows and do not invoke the calculator. Otherwise recalculate once using
   the queued calculation procedure below and verify the fresh rows.
   Never determine eligibility from stale cached results, partial results, or merely same-day rows.
   Apply a strict, fail-closed pending-trials gate to that candidate. Evaluate every configured
   client and segment independently across all of its applicable variation rows. That client +
   segment passes when either (a) `pending trials, %` is present and strictly below 5% in every
   variation, or (b) fresh `pending_trial_cnt` and `access_trial_cnt` rows are present for every
   variation and the sum of `pending_trial_cnt` across those variations is strictly below
   {PENDING_TRIALS_SMALL_COUNT_LIMIT}. This small-count exception is an absolute tolerance, not a
   replacement percentage: never combine clients or segments, and a total equal to
   {PENDING_TRIALS_SMALL_COUNT_LIMIT} is not exempt. Missing, null, stale, failed, or unverifiable
   counts exclude the candidate; when the summed count is {PENDING_TRIALS_SMALL_COUNT_LIMIT} or
   higher, any missing/null pending share or any share equal to or above 5% also excludes it. Apply
   the same rule to bandit arms. When a candidate
   is excluded by this gate, continue to the next candidate in the ordered pool and calculate its
   fresh maturity data. Stop iterating at the first candidate that passes every eligibility rule;
   select and finalise exactly that experiment. Pending charges are explicitly not an eligibility
   condition: report incomplete charge maturity as a caveat in the published Results/Insights, but
   do not exclude an otherwise eligible experiment for it. If the small-count exception is used,
   the published Results/Insights must name the affected client + segment, total pending count,
   total access-trial count, maximum branch share, and the {PENDING_TRIALS_SMALL_COUNT_LIMIT}-trial
   threshold; describe trial-to-charge and trial-derived money metrics as low-sample and still
   maturing, without labelling the whole experiment preliminary solely for that exception. Only if
   every candidate in the pool has
   been checked and excluded may you return exactly `{NO_OP_NOTIFICATION}` and nothing else. Do not
   treat this expected no-op as an error.
9. Immediately before any finalisation or publication write, re-fetch the UGM allowlist and
   experiment title and re-check both monetisation gates from rule 1, the actual end
   timestamp/client classification and age gate, the admin/page conditions, and the strict
   pending-trials gate against the same fresh calculation. If the experiment is no longer
   eligible, make no finalisation or publication writes and return exactly `{NO_OP_NOTIFICATION}`
   and nothing else. The reviewer-selection pass has one separately defined exception for an
   idempotent maturity forecast on an ineligible Results task. Keep filter/audit details internal.

"""

MATURITY_FORECAST_POLICY = f"""\
Pending-trials maturity forecast for reviewer selection only:
- When a preliminary candidate fails only the pending-trials gate in rule 8, calculate its earliest
  expected eligibility date before continuing to the next candidate. Use the same package-derived
  subscription source, identities, filters, distinct keys, client/segment/variation boundaries,
  `first_charge_expected_dt`, `pending_trial_cnt`, and `access_trial_cnt` semantics as the fresh
  production calculation. Do not estimate from aggregate trend or assume a uniform trial length.
- Evaluate future scheduled finalizer runs at 12:00 Asia/Nicosia, weekdays only. For each run,
  recompute the remaining pending trials from the row-level expected-charge timestamps. The
  forecast date is the first scheduled run on which every client + segment is expected to pass the
  production gate: either every applicable variation is strictly below 5%, or that client +
  segment's summed pending count is strictly below {PENDING_TRIALS_SMALL_COUNT_LIMIT}. Also compute
  the first scheduled date when every variation is strictly below 5% if the small-count exception
  makes eligibility earlier. If row-level timestamps or denominators are missing, unverifiable or
  contradict the fresh calculation (for example a segment larger than its Total, or a re-run that
  disagrees with the calculator), the forecast is unknown: never invent a date and never touch Start
  or Due date, but still leave the status comment defined below.
- The forecast's deliverable is the Jira comment. The user explicitly authorises one forecast
  comment per forecast date on the exact queued Results task; Start/Due date updates are secondary,
  best-effort metadata and must never block, delay, or replace the comment.
  Before any write, re-fetch that task with status, `customfield_10312`, `duedate`, and all comments
  through the repository Jira helper with `--as-bot`; use
  `comments <KEY> --as-bot --limit 1000 --json` and compare the returned count with `total`. If the
  response is incomplete, make no forecast writes and report `comment: failed`. Re-verify its experiment/iteration mapping and
  that it remains in Backlog or To Do. Search the complete comment response for the stable prefix
  `{MATURITY_FORECAST_COMMENT_PREFIX}` and read the date that follows it in the newest such comment.
  If that date is today or later, do not add, edit, or reply to a forecast comment and do not change
  either date field, even if a newer forecast differs. If it is earlier than today (the candidate
  missed its forecast and is still blocked) or no such comment exists, write a new forecast.
  An unknown forecast is written as a status comment beginning exactly
  `{MATURITY_UNKNOWN_COMMENT_PREFIX}`. Add it when no comment with either prefix exists, when the
  newest dated forecast is earlier than today, or when the newest status comment is older than
  {MATURITY_UNKNOWN_REPEAT_DAYS} days; otherwise report `comment: exists`.
- Dates (best-effort): when the existing Start date is empty or earlier than the forecast date — a
  planned date a human set before the experiment matured — set it to the forecast date with
  `set-start-date <KEY> --as-bot --date YYYY-MM-DD --replace`; then set Due date with
  `set-due-date <KEY> --as-bot --date YYYY-MM-DD` when it differs. Skip a setter whose field already
  equals the forecast date. Never move a Start date that is later than the forecast date. A refused
  or failed date write is recorded in the result line below and does not stop the comment.
- Add one short Russian Jira comment through `add-comment <KEY> --as-bot --adf-file <file>` with a
  native ADF mention of the epic's author: read the parent epic's `reporter.accountId` (fall back to
  its assignee when the reporter is missing) and open the first paragraph with
  `{{"type": "mention", "attrs": {{"id": "<accountId>", "text": "@<display name>"}}}}`, followed by
  the text `{MATURITY_FORECAST_COMMENT_PREFIX} YYYY-MM-DD.` Never invent an account id; without one,
  post the comment without a mention. Then say that the task will be taken into work automatically
  on that date, name the limiting client + segment, the forecast pending count/denominator and the
  maximum variation share. If eligibility relies on the small-count exception while a variation
  remains at or above 5%, say so and include the separate expected all-variations-below-5% date.
  Re-fetch the comments to verify the new comment and its mention.
- For an unknown forecast, add the same kind of ADF comment (same epic-author mention rule) that
  opens with `{MATURITY_UNKNOWN_COMMENT_PREFIX}` and says, in plain Russian, that the experiment
  did not pass the pending-trials maturity check today, names the limiting client + segment and the
  pending share the fresh calculation shows, gives the short reason no forecast date can be named
  (state a data contradiction as a contradiction, with both numbers), and that the check repeats on
  the next scheduled run. Re-fetch the comments to verify it.
- These forecast writes do not authorise assignment, a status transition, Confluence changes,
  calculation publication, or any other Jira edit. Never annotate a candidate rejected for any
  reason other than pending-trial maturity. A failed comment write or verification is a real
  workflow failure; do not silently claim idempotency.
- Report every candidate that failed only the pending-trials gate, written or not, as one extra
  line after the terminal line of the start result, in exactly this shape:
  `{MATURITY_FORECAST_RESULT_PREFIX} <experiment id> | <Results task key> | <forecast YYYY-MM-DD or
  unknown> | <comment: added|exists|failed> | <dates: set|unchanged|kept-later|failed> |
  <short reason when the forecast is unknown or the comment failed, else ->`
  These lines are the only text allowed after the terminal line; they are never posted to Slack.
"""

UPCOMING_QUEUE_FORECAST_POLICY = f"""\
Upcoming-queue forecast (a dated Jira annotation on the next Results task to be taken):
- Purpose: when no Results task is started today, the nearest one a later scheduled run will
  take still gets a dated forecast in Jira, so its author knows when to expect итоги. An empty
  candidate pool (for example every ended experiment still inside the age gate) is exactly the
  case this covers; it is not a reason to skip it.
- Queue: every experiment that passes rule 1 (allowlist and title markers), rule 4 (configured
  segments) and rule 6 (its exact Results task is in Backlog or To Do) and that is either still
  running or ended within the last calendar month. Only the end and age gates of rules 2-3 are
  ignored here. This forecast never calculates: it reads existing calculation rows only (the
  daily cron keeps them fresh), and a running or under-age experiment is never sent to the
  calculator for it.
- Earliest eligibility of each queued experiment is the latest of: (a) its actual end, or for a
  running experiment its planned end — the verified due date of the epic's "Остановить
  эксперимент" task, else the admin start plus the planned duration from the project page's
  Experiment design; with neither verifiable the experiment gets no date and no write; (b) that
  end plus the rule-3 age threshold; (c) for an ended experiment with existing fresh rows, the
  pending-trials eligibility date computed as in the maturity forecast (row-level
  `first_charge_expected_dt` against the strict gate of rule 8). When (c) cannot be computed the
  date is the earliest possible one and the comment says "не раньше".
- Take date: simulate the scheduled runs (weekdays, 12:00 Asia/Nicosia) from the next one on.
  Each run takes exactly one task: the oldest-ended experiment whose earliest eligibility has
  passed (running experiments ordered by planned end). The first task the simulation takes is
  the nearest task, and its run date is its forecast date. Annotate only that task; tasks blocked
  by pending trials keep the maturity forecast policy when the pass defines it.
- Before writing, re-fetch the task with status, `customfield_10312`, `duedate`, and all comments
  through `.claude/jira/jira_issue.py` with `--as-bot` (`comments <KEY> --as-bot --limit 1000
  --json`, count equal to `total`; incomplete → no writes) and re-verify Backlog or To Do. Read the
  date in the newest comment starting `{MATURITY_FORECAST_COMMENT_PREFIX}`. Add a new comment
  only when none exists, when that date is earlier than today, or when the new forecast date is
  later than it (the queue slipped); otherwise report `comment: exists`.
- Dates, best effort and never blocking the comment: set Start date (`set-start-date <KEY>
  --as-bot --date YYYY-MM-DD --replace`) when it is empty or earlier than the forecast date, and
  Due date (`set-due-date`) when it differs; never move a Start date that is later than the
  forecast date.
- The comment is short Russian ADF via `add-comment <KEY> --as-bot --adf-file <file>`, opening
  with a native mention of the epic's reporter (assignee when the reporter is missing; never an
  invented account id), then `{MATURITY_FORECAST_COMMENT_PREFIX} YYYY-MM-DD.` and one sentence:
  "по текущей оценке возьму в работу в этот день" (or "не раньше этого дня"), followed by what
  sets the date — the experiment runs until <date>, the post-stop wait ends <date>, pending trials,
  or <n> tasks ahead in the queue. Re-fetch the comments to verify it.
- These writes do not authorise assignment, a status transition, a calculation, Confluence edits,
  or any other Jira edit. Report the result as one line after the terminal line:
  `{QUEUE_FORECAST_RESULT_PREFIX} <experiment id or none> | <Results task key or -> | <forecast
  YYYY-MM-DD or none> | <comment: added|exists|failed|none> | <dates: set|unchanged|kept-later|
  failed|none> | <what sets the date, or why there is no forecast>`
  It is never posted to Slack.
"""

SWEEP_JOB_NAME = "results-overdue-sweep"
SWEEP_RESULT_PREFIX = "OVERDUE_SWEEP:"
SWEEP_DONE_PREFIX = "OVERDUE_SWEEP_DONE"
SWEEP_FAILED_PREFIX = "OVERDUE_SWEEP_FAILED:"
WAITING_COMMENT_PREFIX = "Итоги ждут завершения эксперимента:"
SERVICE_ACCOUNT_ID = "712020:e603f3a9-4b70-4ed8-866f-280460a661c5"
JIRA_HELPER = ".venv/bin/python .claude/jira/jira_issue.py"


def overdue_sweep_jql(today: dt.date) -> str:
    """Results tasks of the service account that are overdue or parked until the experiment ends."""
    return (
        f'project = UMN AND assignee = "{SERVICE_ACCOUNT_ID}" AND summary ~ "Итоги" '
        'AND status in (Backlog, "To Do") '
        f'AND (duedate <= "{today.isoformat()}" OR duedate is EMPTY) ORDER BY duedate ASC, key ASC'
    )


OVERDUE_SWEEP_POLICY = f"""\
Overdue Results sweep (runs after the daily finalizer run has finished):
- Scope: every Results/Итоги task assigned to the service account whose Due date is today or
  earlier, plus the tasks this sweep parked earlier by clearing their dates. Fetch them with
  `{JIRA_HELPER} search --as-bot --json --max-results 200 --fields
  summary,status,assignee,parent,duedate,customfield_10312 --jql '<the JQL supplied below>'`.
  A task with an empty Due date is in scope only when the newest of its comments that starts with
  `{MATURITY_FORECAST_COMMENT_PREFIX}`, `{MATURITY_UNKNOWN_COMMENT_PREFIX}` or
  `{WAITING_COMMENT_PREFIX}` starts with `{WAITING_COMMENT_PREFIX}`; skip every other undated task
  without writing anything and without a report line. Never touch a task outside this scope, and
  never one whose status is not Backlog or To Do.
- For each task, read it with all comments (`comments <KEY> --as-bot --limit 1000 --json`, count
  equal to `total`; an incomplete response means no writes for that task) and map it to its exact
  experiment id with the rule-6 mapping (its epic's launch task for the same iteration, with an
  explicit experiment-id link). A task that cannot be mapped unambiguously, or whose experiment
  fails rule 1, gets no writes; report it as `skipped` with the reason.
- Classify the experiment from the UG experiment admin: `not-started` (no actual start yet),
  `running` (started, no actual end) or `ended` (has an actual end timestamp).
- Not started or running — the task waits for the experiment:
  1. Clear both dates when set: `set-start-date <KEY> --as-bot --clear` and
     `set-due-date <KEY> --as-bot --clear`. They re-read the fields; verify both are empty.
  2. Comment only once per wait: when the newest comment carrying one of the three prefixes above
     already starts with `{WAITING_COMMENT_PREFIX}`, write nothing (`comment: exists`). Otherwise
     add a short Russian ADF comment (`add-comment <KEY> --as-bot --adf-file <file>`) that opens
     with a native mention of the epic's reporter (assignee when the reporter is missing; never an
     invented account id), then `{WAITING_COMMENT_PREFIX}` and says plainly that experiment <id>
     is still running (or has not started yet), that the start and due dates are removed, and that
     the dates will be set again automatically once the experiment is stopped. Name the planned end
     when it is verifiable (the "Остановить эксперимент" task due date, or admin start plus the
     planned duration). Re-fetch the comments to verify it.
- Ended — the task gets a dated forecast:
  1. Compute its take date exactly as the upcoming-queue forecast does: earliest eligibility is
     the latest of the actual end, the end plus the rule-3 age threshold, and the pending-trials
     eligibility date from existing fresh rows (row-level `first_charge_expected_dt` against the
     strict rule-8 gate); when that last date cannot be computed use the earliest possible date and
     say "не раньше". Then simulate the scheduled runs (weekdays, 12:00 Asia/Nicosia) from the next
     run after now — today's run has already happened — over the whole upcoming queue of that
     policy, one task per run, oldest-ended first. The run on which the simulation takes this task
     is its date. Never calculate in this sweep; read existing calculation rows only.
  2. Set both dates to that date: `set-start-date <KEY> --as-bot --date YYYY-MM-DD --replace` and
     `set-due-date <KEY> --as-bot --date YYYY-MM-DD`; verify them. A failed date write is reported
     and does not stop the comment.
  3. Add the standard forecast comment unless the newest `{MATURITY_FORECAST_COMMENT_PREFIX}`
     comment already names the same date (`comment: exists`): short Russian ADF, the same epic
     mention rule, then `{MATURITY_FORECAST_COMMENT_PREFIX} YYYY-MM-DD.` and one sentence "по
     текущей оценке возьму в работу в этот день" (or "не раньше этого дня") followed by what sets
     the date — the post-stop wait ends <date>, pending trials, or <n> tasks ahead in the queue.
     Re-fetch the comments to verify it.
- These writes do not authorise assignment, a status transition, a calculation, Confluence edits,
  Slack messages, or any other Jira edit.
- Report one line per task in scope, in exactly this shape:
  `{SWEEP_RESULT_PREFIX} <task key> | <experiment id or -> | <waiting|dated|skipped|failed> |
  <date YYYY-MM-DD or -> | <comment: added|exists|none|failed> | <dates:
  set|cleared|unchanged|failed> | <short reason>`
  then exactly one terminal line `{SWEEP_DONE_PREFIX}: <number of tasks in scope>`. If the task list
  itself cannot be read, return exactly one line `{SWEEP_FAILED_PREFIX} <reason>`.
"""

OVERDUE_SWEEP_PROMPT = f"""\
[claude]
This is the authorised automated overdue Results sweep of the UG experiment finaliser. The user
explicitly authorises the Jira comments and Start/Due date changes defined below on the Results
tasks assigned to the service account, without asking for approval.
{AUTOMATED_SESSION_REPOSITORY_POLICY}
{AUTOMATED_ATLASSIAN_IDENTITY}
{AUTOMATED_RESPONSE_STYLE}

Use the repository Jira helper with `--as-bot` for every Jira command; the service account is
`{SERVICE_ACCOUNT_ID}`. The finalizer's selection rules and its upcoming-queue forecast define
the mapping, gates and queue this sweep relies on:
{{selection_rules}}
{{queue_policy}}
{OVERDUE_SWEEP_POLICY}
Use the current date/time in Asia/Nicosia. Today is {{today}}. JQL:
{{jql}}
"""


def overdue_sweep_prompt(today: dt.date) -> str:
    # Plain replacement: the embedded policies contain literal braces (ADF JSON examples).
    prompt = OVERDUE_SWEEP_PROMPT
    for token, value in (
        ("{selection_rules}", SELECTION_RULES),
        ("{queue_policy}", UPCOMING_QUEUE_FORECAST_POLICY),
        ("{today}", today.isoformat()),
        ("{jql}", overdue_sweep_jql(today)),
    ):
        prompt = prompt.replace(token, value)
    return prompt


def is_sweep_result(text: str) -> bool:
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return False
    if len(lines) == 1 and lines[0].startswith(SWEEP_FAILED_PREFIX):
        return True
    done = re.fullmatch(rf"{SWEEP_DONE_PREFIX}: (\d+)", lines[-1])
    return (
        done is not None
        and int(done.group(1)) == len(lines) - 1
        and all(line.startswith(SWEEP_RESULT_PREFIX) for line in lines[:-1])
    )


async def sweep_overdue_results(
    agent: AgentSubmitter,
    settings: Settings,
    now: dt.datetime | None = None,
) -> str:
    """Re-date overdue Results tasks, or park them while their experiment is not over."""
    today = (now or dt.datetime.now(dt.UTC)).astimezone(
        ZoneInfo(settings.experiment_finalizer_timezone)
    ).date()
    run = await agent.execute_once(
        overdue_sweep_prompt(today),
        settings.experiment_finalizer_timeout_seconds,
        job_name=SWEEP_JOB_NAME,
        accept_result=is_sweep_result,
        failure_result=lambda text: text.strip().startswith(SWEEP_FAILED_PREFIX),
    )
    for line in run.text.strip().splitlines():
        LOGGER.info("Overdue Results sweep: %s", line.strip())
    return run.text.strip()


FINALIZATION_PROMPT = f"""\
[claude]
This is the authorised daily autonomous experiment-finalisation preparation pass. Complete the
calculation and Confluence publication in this turn. The user explicitly pre-approves progression through all three
publication stages (Results, then Insights, then Decision / Next steps), including the required
Confluence update. All Jira writes belong exclusively to the reviewer. Do not pause to request approval between stages. This
instruction intentionally overrides only the interactive approval pauses in the skills; keep
all their data-quality, maturity, verification, language, and publication safeguards.

{AUTOMATED_SESSION_REPOSITORY_POLICY}

{AUTOMATED_ATLASSIAN_IDENTITY}

{AUTOMATED_RESPONSE_STYLE}

{LOCAL_CONFLUENCE_USER_DIRECTORY}

Goal: finalise exactly one eligible UG monetisation experiment.

{SELECTION_RULES}
These rules describe selection BEFORE the reviewer claims a task. This preparation pass receives
an already selected and started task: In Progress on that exact task is expected and does not make
it ineligible. Do not walk the pool or select another experiment. Revalidate its monetisation,
age, segment, iteration, and fresh maturity evidence; fail if any gate changed. Reuse this run's
verified fresh calculation; recalculate only if freshness cannot be established.
Fetch only the authoritative project page ID supplied below. Do not search, enumerate, fetch, or
publish another Confluence page. If it does not match the experiment and task, fail explicitly.

Execution for the selected experiment:
1. Work only on the exact experiment, page, iteration, and Results task supplied by the reviewer.
   The reviewer has already verified eligibility and started that task. Re-fetch and verify its
   In Progress status, bot assignee, and Start date. If the start cannot be verified, fail without
   Jira writes. Never substitute another candidate.
{WORKER_JIRA_POLICY}
2. State the selected title, id, end timestamp, clients, segments, target project page, iteration,
   configured table prefix, and affected package-managed tables.
3. Use the `ug-experiment-calculator` skill and calculate through the calculator API queue. Never
   call `calculate_exp_info(...)` in-process in this job: a direct run bypasses the queue and can
   read the shared subscription source tables while a queued calculation is rebuilding them
   (exp 7994 on 2026-10-15 got iOS Free trials without their charges that way). First run the
   repository freshness preflight. Enqueue with
   `{CALC_API_COMMAND} calculate <exp_id> --priority {FINALIZER_CALC_PRIORITY} --source {FINALIZER_CALC_SOURCE} --wait --timeout 3300`;
   on this server it reaches the API through the corporate bridge. A higher priority puts the job
   ahead of queued ones, but a running calculation is never pre-empted, so it may wait first.
   Exit 0 means the calculation succeeded; exit 3 means it is still queued or running — continue
   with `{CALC_API_COMMAND} wait <request_id> --timeout 3300` until it ends, for at most
   {FINALIZER_CALC_MAX_HOURS} hours in total. If the enqueue answer says `"duplicate": true`, the
   experiment is already queued or running: wait for that request id, do not enqueue another.
   Before importing clickhouse-worker for verification reads, disable clickhouse-connect server
   sessions with `clickhouse_connect.common.set_setting("autogenerate_session_id", False)`, and use
   the standard `ExperimentCalculatorConfig.from_env()` configuration with the
   `ug_monetization_sloperator_` table prefix. This queued calculation is explicitly authorised for
   this scheduled job, including its documented writes and subscription-source refresh. During
   selection, run this procedure once for each preliminary candidate in rule 7 until one passes the
   maturity gate; do not calculate the selected experiment a second time when its verified
   calculation is still fresh. Wait for each calculation to finish and verify fresh successful rows
   in every expected result/stat/funnel and raw users table before continuing. Do not silently use
   stale results. If the calculation fails, is lost, runs past the limit, or the API or the bridge
   is unreachable, stop without publishing partial итогов and report the exact failure.
3. Run the complete Results → Insights → Decision / Next steps pipeline in this exact order:
   a. Generate the full publishable Results/Итоги block through `ug-experiment-calculator`,
      including its required finished-experiment Forecast/audience workflow and maturity checks.
      Insert it into the correct experiment iteration on the existing project page with Decision,
      Next steps, and Insights initially empty.
   b. Use `experiment-insight-research` to perform the full evidence-backed insight research.
      Insert the insight summary and supported question blocks into that same Results block.
   c. Only after insights are present, write concise Decision and Next steps grounded in them and
      insert them into the same block.
   Do not create a separate Confluence child page. Preserve unrelated page content and other
   iterations. Confluence content must be English.
   Save the fetched pre-edit storage body as the publication baseline and run the repository
   Confluence publish gate on the exact Results/Insights/Decision/Next steps block being added or
   changed. The new block must pass every blocking check. If, and only if, every full-page blocking
   finding is byte-for-byte present in the baseline and outside every added or modified region,
   prefix the `update-storage` command with
   `UG_PUBLISH_GATE_BASELINE=<pre-edit-storage-file>`. The hook then exempts only the matching
   baseline findings and still blocks every newly introduced defect. Never use
   `UG_PUBLISH_GATE_DISABLED`. This narrow exception never permits a defect in this run's block,
   changed surrounding content, invalid XML, a missing baseline, or removal of unrelated content.
4. Fetch the project page again after the write. Verify that the correct experiment id/iteration
   contains non-empty Results, Insights, Decision, and Next steps, that storage XML is valid, and
   that no unrelated block disappeared. If verification fails, report failure and do not announce
   success.
   Never put local/server paths or links to logs, SQL, scripts, CSVs, ZIPs, or other run artifacts
   into the project-page body. Package useful reader-safe analysis artifacts into one bundle and
   upload it as an attachment to the existing project page instead. Verify the attachment upload.
5. Leave every Jira write and every user-facing comment to the reviewer. Return a private handoff
   only; do not send Slack yourself.

Preparation result:
- After Confluence verification, return
  `FINALIZATION_PREPARED: <experiment id> | <project page URL> | Iteration <n>` as the exact first
  line, followed by the private evidence handoff the reviewer needs. Return `{NO_OP_NOTIFICATION}`
  for no eligible candidates, or a concise `Experiment finalisation failed: <reason>` line on
  failure.

Final notification (reviewer only):
- Return the final notification to Sloperator only from the independent reviewer; it will publish it as one top-level message in the
  configured production channel and attach this same agent session to the resulting Slack thread.
- Do not send any kickoff, progress, validation, QA, waiting, or completion-soon messages through
  Slack tools. In particular, never post messages such as "starting the daily finalisation" or
  "Validating: running a QA check". Produce exactly one Slack-facing notification, and only after
  the calculation, Confluence publication and verification, and Jira comment verification have
  all completed. Return that notification solely as the final response; do not send it yourself.
- Return the notification only in your final response; do not send it yourself through Slack tools.
- Never prefix the notification with artifact, design-review, validation, publication, verification,
  or other operational commentary. Sloperator rejects text outside the formats below.
- Do not return `SLOPERATOR_ARTIFACT` and do not attach analysis artifacts to Slack. The analysis
  bundle belongs only on the project page as described above.
- Start with exactly one compact heading sentence. Render it on one line in this shape:
  `[<project/experiment title>](<project page URL>) — experiment
  [<id>](https://www.ultimate-guitar.com/components/ab/experiment/view?id=<id>),
  Iteration <n>. Results calculated and published.`
  Put the project-page link into the title and the UG admin link into the experiment id. Do not
  print raw URLs.
- Add at most two extremely short bullets with the most important conclusions.
- Mention every distinct person listed in the project-page header table under DRI / Project owner
  and Team. Resolve Slack user ids and use real `<@USERID>` mentions; never invent ids. If a person
  cannot be resolved inside the agent, use their exact plain display name without a resolution-gap
  note; Sloperator performs the authoritative workspace lookup at delivery time.
- Do not include a separate Project page line, Jira link/key/epic, Execution audit, calculation
  metadata, verification details, artifact list, file paths, or any other operational appendix.
  After the heading, mentions, and at most two conclusion bullets, stop.
- For every no-eligible-candidate path, return exactly `{NO_OP_NOTIFICATION}` as the entire
  response: one sentence, no bullets, candidate list, filter details, audit, explanation, or
  appendix. For a failed workflow, start with exactly `Experiment finalisation failed:` and give
  only the concise operator-facing failure. Do not put any text before these formats.

Use the current date/time in Asia/Nicosia for all relative-date and completion decisions.
Never finalise more than one experiment in this run.
"""

FINAL_NOTIFICATION_POLICY = FINALIZATION_PROMPT.split("Final notification (reviewer only):", 1)[1]

START_PROMPT = f"""[claude]
This is the authorised reviewer start pass for one UG experiment finalisation.
{AUTOMATED_SESSION_REPOSITORY_POLICY}
{AUTOMATED_ATLASSIAN_IDENTITY}
{AUTOMATED_RESPONSE_STYLE}
{LOCAL_CONFLUENCE_USER_DIRECTORY}
{REVIEWER_OWNERSHIP_POLICY}

Select exactly one experiment using all of these gates before any finalisation or publication
write. The maturity-forecast metadata exception is defined separately below:
{SELECTION_RULES}
{MATURITY_FORECAST_POLICY}
{UPCOMING_QUEUE_FORECAST_POLICY}
Apply the upcoming-queue forecast only when this pass ends without starting a Results task.
Before the project-page check in rule 5, identify the exact Results task and its parent epic using
rule 6. Resolve its project page from the experiment admin object's `project` value and
independently verify it against the Results task's parent epic. For the Jira check, request
`/rest/api/3/issue/<EPIC-KEY>/remotelink` explicitly: the Jira UI's `Confluence content` /
`mentioned in` block is a remote link and is not returned by the normal issue fields, issue links,
description, or comments. Accept the unique Confluence `object.url` whose application type is
`com.atlassian.confluence`; its `globalId` `pageId` is an equivalent fallback. The experiment and
Jira sources must identify the same page when both are present. Fetch only that page; do not search
or enumerate Confluence spaces or fetch another page. If a candidate has a Results task but neither
source provides a unique project page, fail with the experiment id, task key, and epic key. If the
page does not match the experiment and iteration, fail rather than searching for a replacement.

Verified identity for the current banner backlog (re-check live status and every other gate):
the [launch notification](https://muse-group.enterprise.slack.com/archives/C02TE2S5YQL/p1789732463872359)
links experiment 7940 to Jira launch task UMN-13458. Its "Iteration 18 Sep" cohort contains
Results task UMN-13460. UMN-12550 is the older Done Results task under the same epic; its
status does not determine eligibility for experiment 7940. This binding establishes task
identity only, not data maturity or permission to publish results.

Fresh calculation procedure used to establish candidate maturity:
3. Use the `ug-experiment-calculator` skill and calculate through the calculator API queue. Never
   call `calculate_exp_info(...)` in-process in this job: a direct run bypasses the queue and can
   read the shared subscription source tables while a queued calculation is rebuilding them
   (exp 7994 on 2026-10-15 got iOS Free trials without their charges that way). First run the
   repository freshness preflight. Enqueue with
   `{CALC_API_COMMAND} calculate <exp_id> --priority {FINALIZER_CALC_PRIORITY} --source {FINALIZER_CALC_SOURCE} --wait --timeout 3300`;
   on this server it reaches the API through the corporate bridge. A higher priority puts the job
   ahead of queued ones, but a running calculation is never pre-empted, so it may wait first.
   Exit 0 means the calculation succeeded; exit 3 means it is still queued or running — continue
   with `{CALC_API_COMMAND} wait <request_id> --timeout 3300` until it ends, for at most
   {FINALIZER_CALC_MAX_HOURS} hours in total. If the enqueue answer says `"duplicate": true`, the
   experiment is already queued or running: wait for that request id, do not enqueue another.
   Before importing clickhouse-worker for verification reads, disable clickhouse-connect server
   sessions with `clickhouse_connect.common.set_setting("autogenerate_session_id", False)`, and use
   the standard `ExperimentCalculatorConfig.from_env()` configuration with the
   `ug_monetization_sloperator_` table prefix. This queued calculation is explicitly authorised for
   this scheduled job, including its documented writes and subscription-source refresh. During
   selection, run this procedure once for each preliminary candidate in rule 7 until one passes the
   maturity gate; do not calculate the selected experiment a second time when its verified
   calculation is still fresh. Wait for each calculation to finish and verify fresh successful rows
   in every expected result/stat/funnel and raw users table before continuing. Do not silently use
   stale results. If the calculation fails, is lost, runs past the limit, or the API or the bridge
   is unreachable, stop without publishing partial итогов and report the exact failure.

Only after a candidate passes every gate, start its exact Results task:
{REVIEWER_START_POLICY}
Do not generate or publish Results, Insights, Decision, or Next steps in this pass.
Return exactly one terminal line after the verified start:
FINALIZATION_STARTED: <experiment id> | <project page URL> | Iteration <n> | <Results task key>
Return exactly {NO_OP_NOTIFICATION} if none qualify. On failure return exactly
Experiment finalisation failed: <reason>.
In this pass, "nothing else" after the terminal line still permits the
`{MATURITY_FORECAST_RESULT_PREFIX}` lines defined in the maturity forecast policy and the one
`{QUEUE_FORECAST_RESULT_PREFIX}` line of the upcoming-queue forecast, and nothing more.
"""


REVIEW_PROMPT = f"""\
[claude]
This is the authorised independent review pass for one prepared UG experiment finalisation.
{AUTOMATED_RESPONSE_STYLE}
{AUTOMATED_SESSION_REPOSITORY_POLICY}
{AUTOMATED_ATLASSIAN_IDENTITY}
{LOCAL_CONFLUENCE_USER_DIRECTORY}
{REVIEWER_OWNERSHIP_POLICY}

Review only the exact experiment, project page, and iteration supplied below. Re-fetch the page and
verify Results, Insights, Decision, and Next steps are complete, valid, and belong to that iteration.
Do not search, enumerate, fetch, or publish any other Confluence page. If the supplied page is not
the matching project page, fail instead of searching for a replacement.
Use the repository Jira helper with `--as-bot` for every Jira command. Resolve the service account from
`/myself` (`712020:e603f3a9-4b70-4ed8-866f-280460a661c5`). Re-fetch the exact Results task's current
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
{FINAL_NOTIFICATION_POLICY} Do not send Slack yourself. On any failure return exactly `Experiment finalisation failed: <reason>`.

Preparation result:
{{prepared_result}}
"""


class AgentSubmitter(Protocol):
    async def execute_once(
        self,
        text: str,
        timeout_seconds: int,
        *,
        job_name: str = "scheduled-agent",
        accept_result: Callable[[str], bool] = lambda _: True,
        failure_result: Callable[[str], bool] = lambda _: False,
        max_interim_results: int = 2,
        existing_session_id: str | None = None,
    ) -> HeadlessAgentRun: ...

    async def attach_session(
        self,
        channel_id: str,
        thread_ts: str,
        run: HeadlessAgentRun,
    ) -> None: ...


class InvalidFinalizationNotification(ValueError):
    """The scheduled agent returned text that is unsafe to publish directly."""


PREPARED_RE = re.compile(
    r"^FINALIZATION_PREPARED:\s*(\d+)\s*\|\s*(\S+)\s*\|\s*Iteration\s+(\d+)[ \t]*(?:\n|$)"
)


STARTED_RE = re.compile(
    r"FINALIZATION_STARTED: (\d+) \| (https://[^\s|]+) \| Iteration (\d+) \| (UMN-\d+)"
)

MISSING_PAGE_FAILURE_RE = re.compile(
    r"experiment\s+(\d+).*?Results task\s+(UMN-\d+).*?epic\s+(UMN-\d+).*?"
    r"(?:has no|without (?:a|the)) Confluence project(?:-| )page link",
    re.IGNORECASE | re.DOTALL,
)


def split_maturity_forecasts(text: str) -> tuple[str, tuple[str, ...]]:
    """Separate trailing maturity-forecast report lines from the terminal start result."""
    kept: list[str] = []
    forecasts: list[str] = []
    for line in text.strip().splitlines():
        if line.strip().startswith((MATURITY_FORECAST_RESULT_PREFIX, QUEUE_FORECAST_RESULT_PREFIX)):
            forecasts.append(line.strip())
        else:
            kept.append(line)
    return "\n".join(kept).strip(), tuple(forecasts)


def is_start_result(text: str) -> bool:
    terminal, _ = split_maturity_forecasts(text)
    return bool(STARTED_RE.fullmatch(terminal)) or terminal.startswith(
        (NO_OP_PREFIX, *FAILURE_PREFIXES)
    )


def is_reviewer_result(text: str) -> bool:
    return is_start_result(text) or is_finalization_notification(text)


def is_preparation_result(text: str) -> bool:
    stripped = text.strip()
    return bool(
        PREPARED_RE.match(stripped)
        or stripped.startswith(NO_OP_PREFIX)
        or stripped.startswith(FAILURE_PREFIXES)
    )


def is_finalization_failure(text: str) -> bool:
    """Return whether a terminal finalizer response represents a failed run."""
    return text.strip().startswith(FAILURE_PREFIXES)


def is_transient_clickhouse_failure(text: str) -> bool:
    """Recognize ClickHouse failures that are safe to retry in a fresh process."""
    if not is_finalization_failure(text):
        return False
    normalized = text.casefold()
    if "session_is_locked" in normalized or "session is locked" in normalized:
        return True
    if (
        "query_with_same_id_is_already_running" in normalized
        or (
            "query with id" in normalized
            and "is already running" in normalized
        )
    ):
        return True
    if "clickhouse" not in normalized:
        return False
    return any(
        marker in normalized
        for marker in (
            "502 bad gateway",
            "503 service unavailable",
            "504 gateway time-out",
            "504 gateway timeout",
            "http status 502",
            "http status 503",
            "http status 504",
            "connection reset",
            "remote end closed connection",
        )
    )


async def execute_finalizer_turn(
    agent: AgentSubmitter,
    prompt: str,
    timeout_seconds: int,
    *,
    job_name: str,
    accept_result: Callable[[str], bool],
    existing_session_id: str | None = None,
) -> HeadlessAgentRun:
    """Execute one finalizer turn and retry only transient ClickHouse failures."""
    run = await agent.execute_once(
        prompt,
        timeout_seconds,
        job_name=job_name,
        accept_result=accept_result,
        failure_result=is_finalization_failure,
        existing_session_id=existing_session_id,
    )
    for attempt, delay in enumerate(TRANSIENT_CLICKHOUSE_RETRY_DELAYS, start=1):
        if not is_transient_clickhouse_failure(run.text):
            return run
        LOGGER.warning(
            "Transient ClickHouse failure in %s; retry %d/%d in %d seconds",
            job_name,
            attempt,
            len(TRANSIENT_CLICKHOUSE_RETRY_DELAYS),
            delay,
        )
        await asyncio.sleep(delay)
        run = await agent.execute_once(
            TRANSIENT_CLICKHOUSE_RETRY_PROMPT + "\nPrevious failure:\n" + run.text,
            timeout_seconds,
            job_name=job_name,
            accept_result=accept_result,
            failure_result=is_finalization_failure,
            existing_session_id=run.session_id,
        )
    return run


def normalize_finalization_notification(text: str) -> str:
    """Remove model preamble and enforce one of the production notification shapes."""
    stripped = text.strip()
    lines = stripped.splitlines()
    for index, line in enumerate(lines):
        candidate = line.strip()
        if (
            candidate.startswith(("[", "<http"))
            and " — experiment " in candidate
            and "components/ab/experiment/view?id=" in candidate
            and "Results calculated and published." in candidate
        ):
            return "\n".join(lines[index:]).strip()
    if stripped.startswith(NO_OP_PREFIX):
        return NO_OP_NOTIFICATION
    if stripped.startswith(FAILURE_PREFIXES):
        return stripped
    raise InvalidFinalizationNotification(
        "Scheduled agent response has no valid completion, no-op, or failure heading"
    )


def is_finalization_notification(text: str) -> bool:
    """Return whether a headless result is publishable rather than an interim update."""
    try:
        normalize_finalization_notification(text)
    except InvalidFinalizationNotification:
        return False
    return True


def next_run_at(
    now: dt.datetime,
    timezone_name: str = "Asia/Nicosia",
    hour: int = 12,
) -> dt.datetime:
    """Return the next weekday wall-clock run time, preserving Cyprus DST."""
    timezone = ZoneInfo(timezone_name)
    local_now = now.astimezone(timezone)
    candidate = local_now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += dt.timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += dt.timedelta(days=1)
    return candidate


async def run_once(
    client: AsyncWebClient,
    agent: AgentSubmitter,
    settings: Settings,
) -> str:
    """Run headlessly, publish once, and attach the resumable session."""
    start_run = await execute_finalizer_turn(
        agent,
        START_PROMPT,
        settings.experiment_finalizer_timeout_seconds,
        job_name="experiment-finalizer-reviewer",
        accept_result=is_start_result,
    )
    _, forecasts = split_maturity_forecasts(start_run.text)
    recovered_start = await recover_missing_project_page(settings, agent, start_run)
    if recovered_start is not None:
        start_run = recovered_start
    terminal, recovered_forecasts = split_maturity_forecasts(start_run.text)
    for forecast in dict.fromkeys((*forecasts, *recovered_forecasts)):
        LOGGER.info("Experiment finalizer forecast: %s", forecast)
    start_run = replace(start_run, text=terminal)
    if start_run.text.strip().startswith(NO_OP_PREFIX):
        no_op_run = replace(start_run, text=NO_OP_NOTIFICATION)
        return await publish_run(client, agent, settings, no_op_run)
    if STARTED_RE.fullmatch(start_run.text.strip()) is None:
        raise InvalidFinalizationNotification(start_run.text)
    return await run_preparation_from_started_run(client, agent, settings, start_run)


async def run_preparation_from_started_run(
    client: AsyncWebClient,
    agent: AgentSubmitter,
    settings: Settings,
    start_run: HeadlessAgentRun,
) -> str:
    """Validate a reviewer start before handing it to the preparation agent."""
    try:
        project_page_id = await validate_started_page(settings, start_run.text)
    except SelectionError as error:
        started = STARTED_RE.fullmatch(start_run.text.strip())
        await client.chat_postMessage(
            channel=await channel_for_task(
                settings,
                started.group(4) if started else None,
                settings.experiment_finalizer_channel,
            ),
            markdown_text=f"Experiment finalisation failed: {error}",
            unfurl_links=False,
            unfurl_media=False,
        )
        raise
    return await run_preparation(client, agent, settings, start_run, project_page_id)


async def recover_missing_project_page(
    settings: Settings,
    agent: AgentSubmitter,
    failed_run: HeadlessAgentRun,
) -> HeadlessAgentRun | None:
    """Resume a reviewer when Jira's UI-only Confluence link was overlooked."""
    match = MISSING_PAGE_FAILURE_RE.search(failed_run.text)
    if match is None or not settings.jira_username or not settings.jira_api_token:
        return None
    experiment_id, task_key, reported_epic_key = match.groups()
    jira = JiraRestReader(settings.jira_url, settings.jira_username, settings.jira_api_token)
    actual_epic_key = await jira.issue_parent_key(task_key)
    if actual_epic_key != reported_epic_key:
        raise SelectionError(
            f"Results task {task_key} belongs to {actual_epic_key}, not reported epic "
            f"{reported_epic_key}"
        )
    project_page_id = await resolve_project_page_id(jira, actual_epic_key)
    project_page_url = (
        f"https://alice.mu.se/pages/viewpage.action?pageId={project_page_id}"
    )
    recovery_prompt = f"""[claude]
This is the authorised continuation of the same experiment-finalisation reviewer pass.
{AUTOMATED_RESPONSE_STYLE}
{AUTOMATED_ATLASSIAN_IDENTITY}
{REVIEWER_OWNERSHIP_POLICY}

Sloperator independently read Jira REST `/rest/api/3/issue/{actual_epic_key}/remotelink` and
verified the parent of Results task {task_key}. Jira's `Confluence content` / `mentioned in` remote
link identifies this authoritative project page:
{project_page_url}

Resume only experiment {experiment_id}, the previously selected iteration, and Results task
{task_key}. Verify that this page matches that experiment and iteration and re-check every remaining
eligibility gate. If it matches, start the exact task under the reviewer start policy and return
exactly `FINALIZATION_STARTED: {experiment_id} | {project_page_url} | Iteration <n> | {task_key}`.
Do not switch candidates or search for another Confluence page. On a real mismatch or another
failure, return exactly `Experiment finalisation failed: <reason>`.
"""
    return await execute_finalizer_turn(
        agent,
        recovery_prompt,
        settings.experiment_finalizer_timeout_seconds,
        job_name="experiment-finalizer-reviewer",
        accept_result=is_start_result,
        existing_session_id=failed_run.session_id,
    )


async def validate_started_page(settings: Settings, start_text: str) -> str:
    """Verify the selected page against the Results task's parent epic before preparation."""
    started = STARTED_RE.fullmatch(start_text.strip())
    if started is None:
        raise SelectionError("Finalisation start has no experiment, page, and Results task")
    if not settings.jira_username or not settings.jira_api_token:
        raise SelectionError("Jira credentials are unavailable to project-page resolver")
    task_key = started.group(4)
    jira = JiraRestReader(settings.jira_url, settings.jira_username, settings.jira_api_token)
    epic_key = await jira.issue_parent_key(task_key)
    expected_id = await resolve_project_page_id(jira, epic_key)
    if confluence_page_ids(started.group(2)) != {expected_id}:
        raise SelectionError(
            f"Results task {task_key} selected a page other than the one linked to "
            f"Jira epic {epic_key}"
        )
    return expected_id


async def run_preparation(
    client: AsyncWebClient,
    agent: AgentSubmitter,
    settings: Settings,
    started_run: HeadlessAgentRun,
    project_page_id: str,
) -> str:
    prepared_run = await execute_finalizer_turn(
        agent,
        FINALIZATION_PROMPT + "\n\nAuthoritative project page ID: " + project_page_id
        + "\n" + agent_instructions(project_page_id, started_run.text.strip().split(" | ")[-1])
        + "\nReviewer verified start:\n" + started_run.text,
        settings.experiment_finalizer_timeout_seconds,
        job_name="experiment-finalizer-preparer",
        accept_result=is_preparation_result,
    )
    prepared_text = prepared_run.text.strip()
    if prepared_text.startswith(NO_OP_PREFIX) or prepared_text.startswith(FAILURE_PREFIXES):
        raise InvalidFinalizationNotification(prepared_text)
    if PREPARED_RE.match(prepared_text) is None:
        raise InvalidFinalizationNotification("Preparation agent returned no FINALIZATION_PREPARED marker")
    prepared = PREPARED_RE.match(prepared_text)
    started = STARTED_RE.fullmatch(started_run.text.strip())
    if prepared is None or started is None:
        raise InvalidFinalizationNotification("Invalid finalization start or preparation marker")
    if prepared.groups() != started.groups()[:3]:
        raise InvalidFinalizationNotification("Preparation does not match reviewer selection")
    return await run_review(
        client, agent, settings, prepared_text,
        reviewer_session_id=started_run.session_id,
        start_context=f"Authoritative project page ID: {project_page_id}\n{started_run.text}",
    )


async def run_review(
    client: AsyncWebClient,
    agent: AgentSubmitter,
    settings: Settings,
    prepared_text: str,
    reviewer_session_id: str | None = None,
    start_context: str = "",
) -> str:
    if PREPARED_RE.match(prepared_text.strip()) is None:
        raise InvalidFinalizationNotification("Invalid preparation result")
    review_text = REVIEW_PROMPT.format(prepared_result=prepared_text + "\n" + start_context)
    page_match = re.search(r"Authoritative project page ID: (\d+)", start_context)
    task_match = re.search(r"\| (UMN-\d+)$", start_context.strip())
    if page_match and task_match:
        review_text += "\n" + agent_instructions(page_match.group(1), task_match.group(1))
    review_run = await execute_finalizer_turn(
        agent,
        review_text,
        settings.experiment_finalizer_timeout_seconds,
        job_name="experiment-finalizer-reviewer",
        accept_result=is_finalization_notification,
        existing_session_id=reviewer_session_id,
    )
    return await publish_run(
        client, agent, settings, review_run,
        task_key=task_match.group(1) if task_match else None,
        project_page_id=page_match.group(1) if page_match else None,
    )


async def publish_run(
    client: AsyncWebClient,
    agent: AgentSubmitter,
    settings: Settings,
    run: HeadlessAgentRun,
    task_key: str | None = None,
    project_page_id: str | None = None,
) -> str:
    """Publish and attach a completed or restart-recovered finalizer run."""
    notification = normalize_finalization_notification(run.text)
    if notification != NO_OP_NOTIFICATION and not notification.startswith(FAILURE_PREFIXES):
        heading_page_ids = confluence_page_ids(notification.splitlines()[0])
        page_id = project_page_id or (
            next(iter(heading_page_ids)) if len(heading_page_ids) == 1 else None
        )
        notification = await ensure_header_mentions(
            client, notification, page_id, settings.agent_workspace
        )
    published_run = replace(run, text=notification)
    channel = await channel_for_task(settings, task_key, settings.experiment_finalizer_channel)
    existing_ts = await find_existing_publication(
        client,
        channel,
        notification,
    )
    if existing_ts is not None:
        LOGGER.warning(
            "Experiment finalization notification already exists at %s; "
            "skipping duplicate Slack post",
            existing_ts,
        )
        return notification
    response = await client.chat_postMessage(
        channel=channel,
        markdown_text=notification,
        unfurl_links=False,
        unfurl_media=False,
    )
    channel_id = response.get("channel", channel)
    await agent.attach_session(
        channel_id,
        response["ts"],
        published_run,
    )
    return notification


async def find_existing_publication(
    client: AsyncWebClient,
    channel: str,
    notification: str,
    *,
    max_pages: int = 5,
) -> str | None:
    """Find an already published success notification using its stable identity."""
    experiment = re.search(
        r"components/ab/experiment/view\?id=(\d+).*?Iteration\s+(\d+)",
        notification,
        re.DOTALL,
    )
    if experiment is None:
        return None
    history = getattr(client, "conversations_history", None)
    if not callable(history):
        return None
    experiment_id, iteration = experiment.groups()
    cursor: str | None = None
    for _ in range(max_pages):
        response = await history(
            channel=channel,
            limit=200,
            cursor=cursor,
        )
        for message in response.get("messages", []):
            text = message.get("text", "")
            if not isinstance(text, str):
                continue
            if (
                f"components/ab/experiment/view?id={experiment_id}" in text
                and f"Iteration {iteration}. Results calculated and published." in text
            ):
                timestamp = message.get("ts")
                if isinstance(timestamp, str):
                    return timestamp
        metadata = response.get("response_metadata", {})
        next_cursor = metadata.get("next_cursor") if isinstance(metadata, dict) else None
        if not isinstance(next_cursor, str) or not next_cursor:
            break
        cursor = next_cursor
    return None


async def run_daily(
    client: AsyncWebClient,
    agent: AgentSubmitter,
    settings: Settings,
    enabled: Callable[[], bool] = lambda: True,
) -> None:
    """Run forever at the configured local wall-clock hour on weekdays."""
    while True:
        now = dt.datetime.now(dt.UTC)
        target = next_run_at(
            now,
            settings.experiment_finalizer_timezone,
            settings.experiment_finalizer_hour,
        )
        delay = (target.astimezone(dt.UTC) - now).total_seconds()
        LOGGER.info("Next experiment finalizer run scheduled for %s", target.isoformat())
        await asyncio.sleep(delay)
        if not enabled():
            LOGGER.info("Scheduled experiment finalizer run disabled from admin")
            continue
        run_task: asyncio.Task[str] | None = None
        try:
            LOGGER.info("Starting scheduled experiment finalizer run")
            run_task = asyncio.create_task(
                run_once(client, agent, settings),
                name="scheduled-experiment-finalizer-run",
            )
            result = await run_task
            LOGGER.info("Experiment finalizer run completed")
            if not is_finalization_failure(result):
                await sweep_overdue_results(agent, settings)
        except asyncio.CancelledError:
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling():
                if run_task is not None:
                    run_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await run_task
                raise
            LOGGER.info("Scheduled experiment finalizer run cancelled from admin")
        except Exception:
            LOGGER.exception("Could not start the daily experiment finalizer")


async def cancel_task(task: asyncio.Task[None] | None) -> None:
    """Cancel a scheduler task during application shutdown."""
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
