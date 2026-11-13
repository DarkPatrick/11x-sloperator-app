"""Cross-module scenarios of the CRM finalizer, run through the real modules.

Each test walks one acceptance path across config, the shared finalizer machinery, the CRM
finalizer, the scheduled-job registry, the digest and the job-name lists. Only the agent, Slack
and the Confluence page check are faked.
"""

from __future__ import annotations

import datetime as dt
import inspect
import logging
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from sloperator import config as config_module
from sloperator.agents import HeadlessAgentRun
from sloperator.config import Settings
from sloperator.experiment_finalizer import (
    MAX_START_ATTEMPTS,
    NO_OP_NOTIFICATION,
    SUCCESS_HEADING,
    SWEEP_DONE_PREFIX,
    run_once,
)

PAGE_URL = "https://alice.mu.se/pages/viewpage.action?pageId=838613487"
NICOSIA = ZoneInfo("Asia/Nicosia")


def _notification(exp_id: int, heading: str) -> str:
    return (
        f"[CRM project]({PAGE_URL}) — experiment "
        f"[{exp_id}](https://www.ultimate-guitar.com/components/ab/experiment/view?id={exp_id}), "
        f"Iteration 1. {heading}\n\n• After Click and After Split agree\n"
    )


FIRST_READ_NOTE = _notification(8225, SUCCESS_HEADING)
POST_ANALYSIS_NOTE = _notification(8018, "After Split post-analysis published.")


@pytest.fixture(autouse=True)
def _no_live_confluence(monkeypatch) -> None:
    monkeypatch.delenv("CONFLUENCE_PERSONAL_TOKEN", raising=False)
    monkeypatch.delenv("CONFLUENCE_TOKEN", raising=False)
    monkeypatch.setattr(
        "sloperator.experiment_finalizer.validate_started_page",
        AsyncMock(return_value="838613487"),
    )


@pytest.fixture
def env_settings(monkeypatch) -> Settings:
    """Settings loaded from the environment with a non-default CRM hour (16)."""
    monkeypatch.setattr(config_module, "load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setenv("SLACK_USER_ID", "UOWNER")
    monkeypatch.setenv("SLOPERATOR_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLOPERATOR_SLACK_BOT_SOCKET_TOKEN_ID", "xapp-test")
    monkeypatch.setenv("SLACK_ALLOWED_CONVERSATION_USERS", "")
    monkeypatch.setenv("CRM_FINALIZER_HOUR", "16")
    monkeypatch.setenv("EXPERIMENT_FINALIZER_CHANNEL", "CFINAL")
    for name in ("CRM_FINALIZER_ENABLED", "EXPERIMENT_FINALIZER_HOUR",
                 "EXPERIMENT_FINALIZER_TIMEZONE"):
        monkeypatch.delenv(name, raising=False)
    return Settings.from_environment()


class ScriptedAgent:
    """Agent that answers each job name from its own queue and records every call."""

    def __init__(self, script: dict[str, list[str]]) -> None:
        self.script = {job: list(texts) for job, texts in script.items()}
        self.calls: list[tuple[str, str]] = []
        self.counter: defaultdict[str, int] = defaultdict(int)
        self.attach_session = AsyncMock()

    async def execute_once(self, prompt, *args, **kwargs):
        job = kwargs["job_name"]
        self.calls.append((job, prompt))
        queue = self.script[job]
        text = queue.pop(0) if len(queue) > 1 else queue[0]
        self.counter[job] += 1
        return HeadlessAgentRun("claude", "opus", f"{job}-{self.counter[job]}", text)

    def jobs(self) -> list[str]:
        return [job for job, _ in self.calls]

    def prompts(self, job: str) -> list[str]:
        return [prompt for name, prompt in self.calls if name == job]


def _client() -> SimpleNamespace:
    return SimpleNamespace(
        conversations_history=AsyncMock(
            return_value={"messages": [], "response_metadata": {"next_cursor": ""}}
        ),
        chat_postMessage=AsyncMock(return_value={"channel": "CFINAL", "ts": "300.1"}),
        conversations_open=AsyncMock(return_value={"channel": {"id": "DOWNER"}}),
    )


def _posts(client) -> list[str]:
    return [call.kwargs["markdown_text"] for call in client.chat_postMessage.await_args_list]


def _publishing_script() -> dict[str, list[str]]:
    return {
        "crm-finalizer-reviewer": [
            f"FINALIZATION_STARTED: 8225 | {PAGE_URL} | Iteration 1 | UMN-14210",
            FIRST_READ_NOTE,
        ],
        "crm-finalizer-preparer": [f"FINALIZATION_PREPARED: 8225 | {PAGE_URL} | Iteration 1"],
        "crm-post-analysis-reviewer": [
            f"FINALIZATION_STARTED: 8018 | {PAGE_URL} | Iteration 1 | UMN-14211",
            POST_ANALYSIS_NOTE,
        ],
        "crm-post-analysis-preparer": [f"FINALIZATION_PREPARED: 8018 | {PAGE_URL} | Iteration 1"],
        "crm-results-overdue-sweep": [f"{SWEEP_DONE_PREFIX}: 0"],
    }


# X2 ------------------------------------------------------------------------------------------
async def test_x2_crm_run_publishes_first_read_then_post_analysis_then_sweeps(
    env_settings: Settings,
) -> None:
    from sloperator import daily_activity_digest as digest
    from sloperator.crm_finalizer import crm_overdue_sweep_jql, run_crm_once
    from sloperator.planner_task_tracking import completed_task

    agent = ScriptedAgent(_publishing_script())
    client = _client()

    outcome = await run_crm_once(client, agent, env_settings)

    assert agent.jobs() == [
        "crm-finalizer-reviewer", "crm-finalizer-preparer", "crm-finalizer-reviewer",
        "crm-post-analysis-reviewer", "crm-post-analysis-preparer", "crm-post-analysis-reviewer",
        "crm-results-overdue-sweep",
    ]
    # The configured CRM hour reaches every rendered prompt; no template token is left.
    for _job, prompt in agent.calls:
        assert "<CRM_RUN_TIME>" not in prompt
    assert "16:00 Asia/Nicosia" in agent.prompts("crm-finalizer-reviewer")[0]
    assert "16:00 Asia/Nicosia" in agent.prompts("crm-results-overdue-sweep")[0]
    # Pass 1 calls the calculator first read, pass 2 the new post-analysis export.
    prep1 = " ".join(agent.prompts("crm-finalizer-preparer")[0].split())
    prep2 = " ".join(agent.prompts("crm-post-analysis-preparer")[0].split())
    assert "ug_experiment_calculator.get_experiment_confluence_report_code(<exp_id>)" in prep1
    assert "After Split is not generated in the first read" in prep1
    assert "ug_experiment_calculator.get_crm_after_split_post_analysis_code(<exp_id>)" in prep2
    # The CRM sweep covers only "Experiment results" tasks.
    sweep_prompt = agent.prompts("crm-results-overdue-sweep")[0]
    jql = crm_overdue_sweep_jql(dt.datetime.now(NICOSIA).date())
    assert jql in sweep_prompt
    assert 'summary ~ "\\"Experiment results\\""' in jql and "Итоги" not in jql

    posts = _posts(client)
    assert len(posts) == 2
    assert "Iteration 1. Results calculated and published." in posts[0]
    assert "After Split post-analysis published." in posts[1]
    assert outcome.first_read == posts[0] and outcome.post_analysis == posts[1]
    assert outcome.sweep == f"{SWEEP_DONE_PREFIX}: 0"

    # Run history: the job-name lists and the digest recognise both reviewers' real outputs.
    review1 = agent.prompts("crm-finalizer-reviewer")[1]
    review2 = agent.prompts("crm-post-analysis-reviewer")[1]
    assert completed_task("crm-finalizer-reviewer", outcome.first_read, review1) == "UMN-14210"
    post_key = completed_task("crm-post-analysis-reviewer", outcome.post_analysis, review2)
    assert post_key == "UMN-14211"
    # A first-read notification never counts as a post-analysis and vice versa.
    assert completed_task("crm-post-analysis-reviewer", outcome.first_read, review1) is None
    for job, prompt, result, key in (
        ("crm-finalizer-reviewer", review1, outcome.first_read, "UMN-14210"),
        ("crm-post-analysis-reviewer", review2, outcome.post_analysis, "UMN-14211"),
    ):
        run = {
            "channel_name": job,
            "status": "completed",
            "messages": [{"text": prompt}, {"text": result}],
        }
        assert digest._valid_completed_run(run, "In Review")
        assert digest._run_task_keys(run) == (key,)


# X3 ------------------------------------------------------------------------------------------
async def test_x3_nothing_to_do_is_silent_while_the_ugm_run_still_posts_its_no_op(
    env_settings: Settings,
) -> None:
    from sloperator.crm_finalizer import run_crm_once

    agent = ScriptedAgent({
        "crm-finalizer-reviewer": [NO_OP_NOTIFICATION],
        "crm-post-analysis-reviewer": [NO_OP_NOTIFICATION],
        "crm-results-overdue-sweep": [f"{SWEEP_DONE_PREFIX}: 0"],
        "experiment-finalizer-reviewer": [NO_OP_NOTIFICATION],
    })
    client = _client()

    outcome = await run_crm_once(client, agent, env_settings)

    assert agent.jobs() == [
        "crm-finalizer-reviewer", "crm-post-analysis-reviewer", "crm-results-overdue-sweep",
    ]
    assert outcome.first_read == outcome.post_analysis == NO_OP_NOTIFICATION
    client.chat_postMessage.assert_not_awaited()

    # The UGM run with the same settings keeps today's behaviour: one no-op post.
    await run_once(client, agent, env_settings)
    assert _posts(client) == [NO_OP_NOTIFICATION]
    assert agent.jobs()[-1] == "experiment-finalizer-reviewer"


# X4 ------------------------------------------------------------------------------------------
async def test_x4_failed_first_read_still_runs_post_analysis_and_sweep(
    env_settings: Settings,
) -> None:
    from sloperator.crm_finalizer import run_crm_once

    script = _publishing_script()
    script["crm-finalizer-reviewer"] = ["Experiment finalisation failed: page not linked"]
    agent = ScriptedAgent(script)
    client = _client()

    outcome = await run_crm_once(client, agent, env_settings)

    jobs = agent.jobs()
    assert jobs[:MAX_START_ATTEMPTS] == ["crm-finalizer-reviewer"] * MAX_START_ATTEMPTS
    assert "crm-finalizer-preparer" not in jobs
    assert jobs[MAX_START_ATTEMPTS:] == [
        "crm-post-analysis-reviewer", "crm-post-analysis-preparer", "crm-post-analysis-reviewer",
        "crm-results-overdue-sweep",
    ]
    assert outcome.first_read.startswith("Experiment finalisation failed:")
    posts = _posts(client)
    failures = [post for post in posts if "Experiment finalisation failed:" in post]
    assert len(failures) == MAX_START_ATTEMPTS  # R13: once per failed attempt
    assert sum("After Split post-analysis published." in post for post in posts) == 1
    assert outcome.post_analysis == posts[-1]


# X5 ------------------------------------------------------------------------------------------
async def test_x5_hour_registry_logs_and_run_names_agree_across_modules(
    env_settings: Settings, caplog
) -> None:
    from sloperator import crm_finalizer, daily_activity_digest, jira_agent_policy
    from sloperator import store as store_module
    from sloperator.scheduled_jobs import (
        EMBEDDED_SCHEDULED_JOBS_BY_JOB_NAME,
        EMBEDDED_SCHEDULED_JOBS_BY_NAME,
    )

    job = EMBEDDED_SCHEDULED_JOBS_BY_JOB_NAME["crm-finalizer"]
    assert EMBEDDED_SCHEDULED_JOBS_BY_NAME[job.display_name] is job
    assert job.schedule(env_settings) == "weekdays Mon-Fri 16:00 Asia/Nicosia"
    assert tuple(job.run_job_names) == crm_finalizer.CRM_RUN_JOB_NAMES
    assert job.logger_name == crm_finalizer.LOGGER.name

    # Admin reads the loop's log lines by the registry strings: drive the real loop once.
    class StopLoop(Exception):
        pass

    class OneSlot:
        def __init__(self) -> None:
            self.used = False

        async def next_slot(self, compute, logger, message):
            if self.used:
                raise StopLoop
            self.used = True
            target = compute(dt.datetime(2026, 11, 13, 9, 0, tzinfo=dt.UTC))
            logger.info(message, target.isoformat())
            return target

    agent = ScriptedAgent({
        "crm-finalizer-reviewer": [NO_OP_NOTIFICATION],
        "crm-post-analysis-reviewer": [NO_OP_NOTIFICATION],
        "crm-results-overdue-sweep": [f"{SWEEP_DONE_PREFIX}: 0"],
    })
    caplog.set_level(logging.INFO, logger=job.logger_name)
    with pytest.raises(StopLoop):
        await crm_finalizer.run_crm_daily(_client(), agent, env_settings, slots=OneSlot())
    messages = [r.getMessage() for r in caplog.records if r.name == job.logger_name]
    assert f"{job.scheduled_prefix}2026-11-13T16:00:00+02:00" in messages
    assert job.started_message in messages
    assert job.completed_message in messages

    # Every CRM run name is known to the Jira policy and the digest parsers.
    for name in crm_finalizer.CRM_RUN_JOB_NAMES:
        assert name.startswith(daily_activity_digest.FINALIZER_JOB_PREFIXES)
        assert (name in jira_agent_policy.WORKER_JOBS) == name.endswith("-preparer")
        assert (name in jira_agent_policy.REVIEWER_JOBS) == name.endswith("-reviewer")
    store_source = inspect.getsource(store_module)
    for name in (crm_finalizer.CRM_FIRST_READ_REVIEWER_JOB,
                 crm_finalizer.CRM_POST_ANALYSIS_REVIEWER_JOB):
        assert f"'{name}'" in store_source
    digest_job = EMBEDDED_SCHEDULED_JOBS_BY_JOB_NAME["daily-activity-digest"]
    assert daily_activity_digest.CRM_FINALIZATION_FORECAST_JOB_NAME in digest_job.run_job_names


# X6 ------------------------------------------------------------------------------------------
async def test_x6_digest_crm_forecast_targets_the_configured_crm_run(
    env_settings: Settings,
) -> None:
    from sloperator import daily_activity_digest as digest

    agent = ScriptedAgent({"daily-activity-crm-forecast": ["FINALIZATION_FORECAST_NONE"]})
    now = dt.datetime(2026, 11, 13, 17, 0, tzinfo=NICOSIA)  # Friday 19:00 digest

    facts = await digest._add_crm_finalization_forecast(
        digest.DigestFacts(completed=(), pending=()), env_settings, agent, now=now
    )

    prompt = agent.prompts("daily-activity-crm-forecast")[0]
    assert "<CRM_RUN_TIME>" not in prompt
    assert "16:00 Asia/Nicosia" in prompt
    assert prompt.endswith("Authoritative next scheduled run time: 2026-11-16T16:00:00+02:00")
    assert "Do not write to Jira" in prompt
    assert facts.queue_notes == (digest.CRM_FORECAST_NONE_NOTE,)
    assert facts.pending == ()


# X7 ------------------------------------------------------------------------------------------
class _StopLoop(Exception):
    pass


class _OneSlot:
    def __init__(self) -> None:
        self.targets: list[dt.datetime] = []

    async def next_slot(self, compute, logger, message):
        if self.targets:
            raise _StopLoop
        self.targets.append(compute(dt.datetime(2026, 11, 13, 9, 0, tzinfo=dt.UTC)))
        return self.targets[-1]


async def test_x7_admin_toggle_of_one_job_never_stops_the_other(
    env_settings: Settings, tmp_path, caplog
) -> None:
    from sloperator import crm_finalizer, experiment_finalizer
    from sloperator.automation_controls import AutomationControls
    from sloperator.scheduled_jobs import EMBEDDED_SCHEDULED_JOBS_BY_JOB_NAME

    controls = AutomationControls(tmp_path / "automation-controls.json")
    crm_name = EMBEDDED_SCHEDULED_JOBS_BY_JOB_NAME["crm-finalizer"].display_name
    ugm_name = EMBEDDED_SCHEDULED_JOBS_BY_JOB_NAME["experiment-finalizer"].display_name
    script = {
        "crm-finalizer-reviewer": [NO_OP_NOTIFICATION],
        "crm-post-analysis-reviewer": [NO_OP_NOTIFICATION],
        "crm-results-overdue-sweep": [f"{SWEEP_DONE_PREFIX}: 0"],
        "experiment-finalizer-reviewer": [NO_OP_NOTIFICATION],
        "results-overdue-sweep": [f"{SWEEP_DONE_PREFIX}: 0"],
    }
    caplog.set_level(logging.INFO)

    async def drive(disabled: str) -> tuple[list[str], list[dt.datetime], list[dt.datetime]]:
        controls.set_enabled("crons", crm_name, True)
        controls.set_enabled("crons", ugm_name, True)
        controls.set_enabled("crons", disabled, False)
        agent = ScriptedAgent(script)
        crm_slot, ugm_slot = _OneSlot(), _OneSlot()
        with pytest.raises(_StopLoop):
            await crm_finalizer.run_crm_daily(
                _client(), agent, env_settings,
                lambda: not controls.disabled("crons", crm_name), slots=crm_slot,
            )
        with pytest.raises(_StopLoop):
            await experiment_finalizer.run_daily(
                _client(), agent, env_settings,
                lambda: not controls.disabled("crons", ugm_name), slots=ugm_slot,
            )
        return agent.jobs(), crm_slot.targets, ugm_slot.targets

    jobs, crm_targets, ugm_targets = await drive(crm_name)
    assert not any(job.startswith("crm-") for job in jobs)
    assert jobs[0] == "experiment-finalizer-reviewer"
    assert "Scheduled CRM finalizer run disabled from admin" in caplog.text
    assert crm_targets == [dt.datetime(2026, 11, 13, 16, 0, tzinfo=NICOSIA)]
    assert ugm_targets == [dt.datetime(2026, 11, 13, 12, 0, tzinfo=NICOSIA)]

    caplog.clear()
    jobs, _, _ = await drive(ugm_name)
    assert not any(job.startswith("experiment-finalizer") for job in jobs)
    assert jobs[:3] == [
        "crm-finalizer-reviewer", "crm-post-analysis-reviewer", "crm-results-overdue-sweep",
    ]
