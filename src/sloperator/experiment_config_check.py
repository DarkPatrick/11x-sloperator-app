"""Launch an agent review for experiment-config cron notifications."""

from __future__ import annotations

import logging
import re
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient

from sloperator.agents import HeadlessAgentRun
from sloperator.automated_session_policy import (
    AUTOMATED_RESPONSE_STYLE,
    AUTOMATED_SESSION_REPOSITORY_POLICY,
)
from sloperator.config import Settings

LOGGER = logging.getLogger(__name__)

METADATA_EVENT_TYPE = "ug_experiment_config_check"
VERDICT_MARKER = "EXPERIMENT_CONFIG_VERDICT:"
ISSUE_SECTION_MARKERS = ("Нужно исправить", "Рекомендуемые добавления в segments")
FORBIDDEN_OUTPUT_FRAGMENTS = (
    "Что сошлось",
    "Подозрения / не удалось проверить",
    "Чего проверить не удалось",
    "Дата окончания не проставлена",
    "Версии приложений тут не при чём",
    "Конфиг в целом собран правильно",
)


def experiment_config_payload(event: dict[str, Any]) -> dict[str, Any] | None:
    """Return the trusted structured payload emitted by the local cron job."""
    metadata = event.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("event_type") != METADATA_EVENT_TYPE:
        return None
    payload = metadata.get("event_payload")
    if not isinstance(payload, dict):
        return None
    recipient_id = payload.get("recipient_id")
    experiments = payload.get("experiments")
    if not isinstance(recipient_id, str) or not recipient_id.startswith("U"):
        return None
    if not isinstance(experiments, list) or not experiments:
        return None
    normalized_experiments: list[dict[str, Any]] = []
    for experiment in experiments:
        if not isinstance(experiment, dict):
            return None
        experiment_id = experiment.get("id")
        name = experiment.get("name")
        if isinstance(experiment_id, str) and experiment_id.isdigit():
            experiment_id = int(experiment_id)
        if not isinstance(experiment_id, int) or not isinstance(name, str):
            return None
        normalized_experiments.append({**experiment, "id": experiment_id})
    return {**payload, "experiments": normalized_experiments}


def is_experiment_config_trigger(event: dict[str, Any]) -> bool:
    """Match only top-level DMs posted by this app's cron helper."""
    return (
        event.get("channel_type") == "im"
        and event.get("bot_id") is not None
        and not isinstance(event.get("thread_ts"), str)
        and isinstance(event.get("ts"), str)
        and experiment_config_payload(event) is not None
    )


def build_experiment_config_prompt(payload: dict[str, Any], *, interactive: bool) -> str:
    """Build the fail-closed project/config audit requested by the cron notification."""
    experiments = payload["experiments"]
    compact = "\n".join(
        f"- id={item['id']}; name={item['name']}"
        for item in experiments
    )
    closing = (
        "The recipient is authorised for interactive Sloperator conversations. Invite them to "
        "reply in this Slack thread with questions or corrections."
        if interactive
        else "The recipient is not authorised for interactive Sloperator conversations. End the "
        "message with a short explicit suggestion to ask this agent from Claude Code or contact "
        "the monetisation-team analysts for clarification. Do not invite a reply in Slack."
    )
    return f"""\
This is an automated review of newly started UG monetisation experiments. Work from the
ug-ai-analyst repository and follow its CLAUDE.md, skills, hooks, freshness preflight, and
source-quality rules.

{AUTOMATED_SESSION_REPOSITORY_POLICY}

{AUTOMATED_RESPONSE_STYLE}

Experiments to review:
{compact}

For every experiment, locate the authoritative Confluence project page and the experiment/design
table for the matching iteration. Fetch the current raw configuration from the experiment admin;
do not rely on parsed calculator defaults. Do not guess a project from a similar title. Read the
complete table and compare it with the current raw admin configuration and, where needed, the
relevant application source/release history. Perform all of these checks:
1. Propose a concrete, admin-ready `segments` configuration grounded in the project's target
   audience, hypotheses, metrics, and planned analysis cuts. Use the `ug-experiment-config-builder`
   skill. Validate that every proposed segment is computable from supported calculator fields;
   never invent a field or silently fall back to Total.
2. Check that configured Android/iOS/web application versions are the versions required by the
   project and actually containing the experiment implementation. Verify against source code and
   release tags when the project table alone is ambiguous.
3. Check that the configured activation event exactly matches the project table and the implemented
   exposure/activation point. Distinguish assignment from real exposure and flag uncertainty.
4. Check that the number and identifiers of test branches/variations in the admin configuration
   match the project table. Treat control as a branch and detect missing, extra, or duplicated ids.
5. Decide whether there is a material, actionable configuration/project mismatch that the starter
   should fix. This is not an experiment-health report: do not analyse early SRM, reach pace,
   experiment overlaps, metric movement, subscriptions, rendered-price evidence, or runtime data.

This review runs silently before any Slack message exists. The Slack recipient needs only changes
they should make, never an audit trail. Apply these output rules strictly:
- Never mention checks that passed or say the configuration is correct overall.
- For a web-only experiment, do not mention app versions at all. For app experiments, mention a
  version only when it is materially wrong.
- If configured segments are adequate, do not print or discuss them. Suggest additions only when
  they are materially needed to answer the project's stated decision; give only the additions and
  a concise reason, not a catalogue of alternatives or field-validation notes.
- Do not report minor observations, early-data health signals, normal running state such as an
  empty end date, or anything that could not be verified. Uncertainty means silence, not a caveat.
- Do not include skill/context disclosure lines in the Slack-facing content; Sloperator strips any
  mandatory internal disclosure lines before publication.
- Keep the visible result under 2,500 characters and use at most four bullets total.

If there is no material actionable change after applying those filters, return a concise internal
summary followed by the exact final line
`{VERDICT_MARKER} OK`; Sloperator will suppress the entire response and the starter will receive
nothing. Otherwise reply in Russian with the experiment title plus project/admin links, then only
`Нужно исправить` and, if necessary, `Рекомендуемые добавления в segments`. Finish with the exact
final line `{VERDICT_MARKER} ISSUES`. Do not add any other section.
Do not edit Confluence, Jira, source code, or experiment configuration; this is a read-only audit.
Do not mention an automated check, cron, background run, or that an agent was launched.

{closing}
"""


def normalize_experiment_config_result(text: str) -> tuple[str, str]:
    """Extract the machine verdict while failing closed on ambiguous responses."""
    lines = text.strip().splitlines()
    verdict_lines = [line.strip() for line in lines if line.strip().startswith(VERDICT_MARKER)]
    visible_text = "\n".join(
        line
        for line in lines
        if not line.strip().startswith((VERDICT_MARKER, "🧭", "📚"))
    ).strip()
    if verdict_lines:
        verdict = verdict_lines[-1].removeprefix(VERDICT_MARKER).strip()
        if verdict in {"OK", "ISSUES"}:
            if verdict == "ISSUES":
                if any(fragment in visible_text for fragment in FORBIDDEN_OUTPUT_FRAGMENTS):
                    raise ValueError("agent response contains forbidden low-value content")
                if len(visible_text) > 2_500:
                    raise ValueError(
                        "agent response is too long for experiment-config notification"
                    )
            return verdict, visible_text
        raise ValueError(f"invalid experiment-config verdict: {verdict}")
    if any(marker in visible_text for marker in ISSUE_SECTION_MARKERS):
        if any(fragment in visible_text for fragment in FORBIDDEN_OUTPUT_FRAGMENTS):
            raise ValueError("agent response contains forbidden low-value content")
        if len(visible_text) > 2_500:
            raise ValueError("agent response is too long for experiment-config notification")
        return "ISSUES", visible_text
    raise ValueError("agent response has no experiment-config verdict")


def format_slack_mrkdwn(text: str) -> str:
    """Convert the small Markdown subset agents use into Slack mrkdwn."""
    formatted: list[str] = []
    in_code = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
            formatted.append("```")
        elif in_code:
            formatted.append(line)
        else:
            formatted.append(re.sub(r"\*\*(.+?)\*\*", r"*\1*", line))
    return "\n".join(formatted).strip()


def extract_project_links_and_clean_body(
    text: str,
    experiments: list[dict[str, Any]],
) -> tuple[list[str], str]:
    """Extract project URLs for the intro and remove duplicated identity/link lines."""
    project_urls: list[str] = []
    body_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Проект:"):
            match = re.search(r"https?://[^\s>|)]+", stripped)
            if match is not None:
                project_urls.append(match.group(0))
            continue
        if stripped.startswith("Админка:"):
            continue
        if any(str(experiment["name"]) in stripped for experiment in experiments):
            continue
        body_lines.append(line)
    if len(project_urls) != len(experiments):
        raise ValueError("agent response must contain one project link per experiment")
    return project_urls, "\n".join(body_lines).strip()


def build_notification_intro(
    recipient_id: str,
    experiments: list[dict[str, Any]],
    project_urls: list[str],
) -> str:
    """Build the human opening shown only after an issues verdict."""
    if len(experiments) == 1:
        experiment = experiments[0]
        admin_url = (
            "https://www.ultimate-guitar.com/components/ab/experiment/view"
            f"?id={experiment['id']}"
        )
        return (
            f":wave: Привет, <@{recipient_id}>! Ты недавно запустил эксперимент "
            f"<{project_urls[0]}|«{experiment['name']}»> "
            f"(<{admin_url}|id {experiment['id']}>). "
            "Вот что стоит поправить:"
        )
    bullets = "\n".join(
        f"• <{project_url}|«{experiment['name']}»> "
        "(<https://www.ultimate-guitar.com/components/ab/experiment/view"
        f"?id={experiment['id']}|id {experiment['id']}>)"
        for experiment, project_url in zip(experiments, project_urls, strict=True)
    )
    return (
        f":wave: Привет, <@{recipient_id}>! Ты недавно запустил эксперименты:\n"
        f"{bullets}\n\nВот что стоит поправить:"  # noqa: RUF001
    )


class ExperimentConfigResponder:
    """Turn cron DMs into durable agent-backed Slack threads."""

    def __init__(self, settings: Settings, agent: Any) -> None:
        self.settings = settings
        self.agent = agent

    async def handle(self, event: dict[str, Any], client: AsyncWebClient) -> None:
        payload = experiment_config_payload(event)
        message_ts = event.get("ts")
        channel_id = event.get("channel")
        if payload is None or not isinstance(message_ts, str) or not isinstance(channel_id, str):
            return
        recipient_id = str(payload["recipient_id"])
        interactive = recipient_id in self.settings.conversation_user_ids
        await self.agent.submit(
            client,
            channel_id=channel_id,
            message_ts=f"{message_ts}:experiment-config-review",
            thread_ts=message_ts,
            text=build_experiment_config_prompt(payload, interactive=interactive),
            show_status=False,
            automated=True,
        )

    async def review_and_publish(
        self,
        payload: dict[str, Any],
        client: AsyncWebClient,
        *,
        timeout_seconds: int,
    ) -> bool:
        """Run a silent audit and publish only a confirmed issue/suspicion result."""
        normalized = experiment_config_payload(
            {
                "metadata": {
                    "event_type": METADATA_EVENT_TYPE,
                    "event_payload": payload,
                }
            }
        )
        if normalized is None:
            raise ValueError("invalid experiment config payload")
        recipient_id = str(normalized["recipient_id"])
        interactive = recipient_id in self.settings.conversation_user_ids
        run: HeadlessAgentRun = await self.agent.execute_once(
            build_experiment_config_prompt(normalized, interactive=interactive),
            timeout_seconds,
        )
        verdict, visible_text = normalize_experiment_config_result(run.text)
        if verdict == "OK":
            LOGGER.info("Experiment config audit passed; suppressing Slack notification")
            return False
        conversation = await client.conversations_open(users=recipient_id)
        project_urls, clean_body = extract_project_links_and_clean_body(
            visible_text,
            normalized["experiments"],
        )
        message_text = (
            f"{build_notification_intro(recipient_id, normalized['experiments'], project_urls)}"
            f"\n\n{format_slack_mrkdwn(clean_body)}"
        )
        posted = await client.chat_postMessage(
            channel=conversation["channel"]["id"],
            text=message_text,
            unfurl_links=False,
        )
        await self.agent.attach_session(
            conversation["channel"]["id"],
            posted["ts"],
            run,
        )
        return True
