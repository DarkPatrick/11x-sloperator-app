"""Application lifecycle."""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Mapping
from contextlib import suppress

from aiohttp import web
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from sloperator.admin import create_admin_routes
from sloperator.agents import AgentOrchestrator, validate_agent_runtime
from sloperator.archive import periodically_synchronize_archive, synchronize_archive
from sloperator.automation_controls import AutomationControls
from sloperator.automation_error_audit import PROJECT_ROOT, TIMEOUT_SECONDS
from sloperator.automation_error_audit import (
    cancel_task as cancel_automation_error_audit,
)
from sloperator.automation_error_audit import (
    is_final_result as is_automation_error_audit_result,
)
from sloperator.automation_error_audit import (
    publish_run as publish_automation_error_audit,
)
from sloperator.automation_error_audit import (
    run_daily as run_daily_automation_error_audit,
)
from sloperator.bot import create_app
from sloperator.config import ConfigurationError, Settings
from sloperator.experiment_config_check import (
    ExperimentConfigResponder,
    is_experiment_config_trigger,
)
from sloperator.experiment_design_planner import (
    InvalidDesignResult,
    is_preparation_result,
    is_review_result,
    parse_preparation_result,
    publish_failure,
    run_review,
    select_from_jira,
    task_key_from_review_result,
)
from sloperator.experiment_design_planner import (
    cancel_task as cancel_experiment_design,
)
from sloperator.experiment_design_planner import (
    publish_notification as publish_experiment_design,
)
from sloperator.experiment_design_planner import (
    run_daily as run_daily_experiment_design,
)
from sloperator.experiment_design_selector import SelectionError
from sloperator.experiment_finalizer import (
    InvalidFinalizationNotification,
    cancel_task,
    is_finalization_notification,
    publish_run,
    run_daily,
)
from sloperator.health import create_health_app
from sloperator.payment_layer import PaymentLayerResponder, is_payment_layer_trigger
from sloperator.scheduled_jobs import EMBEDDED_SCHEDULED_JOBS_BY_JOB_NAME
from sloperator.store import EventStore
from sloperator.subscription_flow import (
    SubscriptionFlowResponder,
    is_subscription_flow_event,
)
from sloperator.vpn import VpnError, VpnManager, VpnState

LOGGER = logging.getLogger(__name__)


def configure_logging(level: str) -> None:
    """Configure concise structured-enough server logs."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def serve(settings: Settings) -> None:
    """Run Socket Mode and the health endpoint until termination."""
    validate_agent_runtime(settings)
    store = EventStore(settings.database_path)
    await asyncio.to_thread(store.initialize)
    recovered = await asyncio.to_thread(store.recover_interrupted_agent_work)
    if recovered:
        LOGGER.warning("Recovered %d interrupted agent session(s)", recovered)
    vpn = VpnManager(settings)
    orchestrator = AgentOrchestrator(settings, store, vpn)
    automation_controls = AutomationControls(
        settings.database_path.parent / "automation-controls.json"
    )
    finalizer_job = EMBEDDED_SCHEDULED_JOBS_BY_JOB_NAME["experiment-finalizer"]
    experiment_design_job = EMBEDDED_SCHEDULED_JOBS_BY_JOB_NAME["experiment-design-planner"]
    error_audit_job = EMBEDDED_SCHEDULED_JOBS_BY_JOB_NAME["automation-error-audit"]
    subscription_flow_responder = SubscriptionFlowResponder(settings, store, orchestrator)
    payment_layer_responder = PaymentLayerResponder(settings, store, orchestrator)
    experiment_config_responder = ExperimentConfigResponder(settings, orchestrator)
    app = create_app(
        settings,
        store,
        orchestrator,
        vpn,
        subscription_flow_responder=subscription_flow_responder,
        payment_layer_responder=payment_layer_responder,
        automation_controls=automation_controls,
    )
    orchestrator.set_notification_client(app.client)

    async def handle_new_history_message(
        channel_id: str,
        message: Mapping[str, object],
    ) -> None:
        event = {**message, "channel": channel_id}
        if not automation_controls.disabled(
            "triggers", "subscription-flow"
        ) and is_subscription_flow_event(event, settings):
            await subscription_flow_responder.handle(event, app.client)
        if not automation_controls.disabled(
            "triggers", "payment-layer"
        ) and is_payment_layer_trigger(event, settings):
            await payment_layer_responder.handle(event, app.client)
        if not automation_controls.disabled(
            "triggers", "experiment-config"
        ) and is_experiment_config_trigger(event):
            await experiment_config_responder.handle(event, app.client)

    slack_handler = AsyncSocketModeHandler(app, settings.app_token)
    http_app = create_health_app()
    create_admin_routes(http_app, store, orchestrator, app.client, automation_controls)

    async def trigger_experiment_config(request: web.Request) -> web.Response:
        """Accept a local cron handoff after its Slack top-level message is posted."""
        if request.remote not in {"127.0.0.1", "::1"}:
            raise web.HTTPForbidden(text="loopback only")
        try:
            payload = await request.json()
        except (ValueError, TypeError) as error:
            raise web.HTTPBadRequest(text="invalid JSON") from error
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="JSON object required")
        try:
            notified = await experiment_config_responder.review_and_publish(
                payload,
                app.client,
                timeout_seconds=settings.experiment_config_timeout_seconds,
            )
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error)) from error
        return web.json_response({"ok": True, "completed": True, "notified": notified})

    http_app.router.add_post(
        "/internal/experiment-config-check",
        trigger_experiment_config,
    )
    runner = web.AppRunner(http_app, access_log=None)
    stop_event = asyncio.Event()
    archive_task: asyncio.Task[None] | None = None
    vpn_task: asyncio.Task[None] | None = None
    experiment_finalizer_task: asyncio.Task[None] | None = None
    experiment_design_task: asyncio.Task[None] | None = None
    automation_error_audit_task: asyncio.Task[None] | None = None
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    await runner.setup()
    site = web.TCPSite(runner, settings.host, settings.port)

    try:
        await site.start()
        await slack_handler.connect_async()  # type: ignore[no-untyped-call]
        await orchestrator.resume_interrupted(app.client)
        recovered_headless = await orchestrator.resume_interrupted_headless(
            settings.experiment_finalizer_timeout_seconds,
            job_name="experiment-finalizer",
            accept_result=is_finalization_notification,
        )
        for run in recovered_headless:
            try:
                await publish_run(app.client, orchestrator, settings, run)
            except InvalidFinalizationNotification as error:
                LOGGER.error(
                    "Recovered experiment finalizer returned an invalid interim response"
                )
                if run.run_id is not None:
                    await asyncio.to_thread(
                        store.finish_scheduled_agent_run,
                        run.run_id,
                        status="failed",
                        external_session_id=run.session_id,
                        result_text=run.text,
                        last_error=repr(error),
                    )
                conversation = await app.client.conversations_open(
                    users=settings.slack_user_id
                )
                await app.client.chat_postMessage(
                    channel=conversation["channel"]["id"],
                    markdown_text=(
                        f"<@{settings.slack_user_id}> ⚠️ Experiment finalizer остановлен: "
                        "после восстановления агент вернул промежуточный, но не финальный "
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
        recovered_design_reviews = await orchestrator.resume_interrupted_headless(
            settings.experiment_design_timeout_seconds,
            job_name="experiment-design-reviewer",
            accept_result=is_review_result,
        )
        for run in recovered_design_reviews:
            try:
                task_key = task_key_from_review_result(run.text)
                await publish_experiment_design(
                    app.client, orchestrator, settings, run, task_key
                )
            except InvalidDesignResult as error:
                if str(error).startswith("Experiment design automation failed:"):
                    await publish_failure(app.client, settings, str(error))
                status = "failed"
                last_error: str | None = repr(error)
            else:
                status = "completed"
                last_error = None
            if run.run_id is not None:
                await asyncio.to_thread(
                    store.finish_scheduled_agent_run,
                    run.run_id,
                    status=status,
                    external_session_id=run.session_id,
                    result_text=run.text,
                    last_error=last_error,
                )
        recovered_design_preparations = await orchestrator.resume_interrupted_headless(
            settings.experiment_design_timeout_seconds,
            job_name="experiment-design-preparer",
            accept_result=is_preparation_result,
        )
        for run in recovered_design_preparations:
            try:
                prepared = parse_preparation_result(run.text)
                if prepared is not None:
                    selected = await select_from_jira(settings)
                    if selected is None or prepared != (
                        selected.task_key,
                        selected.epic_key,
                    ):
                        raise InvalidDesignResult(
                            "Recovered preparation no longer matches deterministic selection"
                        )
                    await run_review(app.client, orchestrator, settings, *prepared)
            except (InvalidDesignResult, SelectionError) as error:
                if str(error).startswith("Experiment design automation failed:"):
                    await publish_failure(app.client, settings, str(error))
                status = "failed"
                last_error = repr(error)
            else:
                status = "completed"
                last_error = None
            if run.run_id is not None:
                await asyncio.to_thread(
                    store.finish_scheduled_agent_run,
                    run.run_id,
                    status=status,
                    external_session_id=run.session_id,
                    result_text=run.text,
                    last_error=last_error,
                )
        recovered_audits = await orchestrator.resume_interrupted_headless(
            TIMEOUT_SECONDS,
            job_name="automation-error-audit",
            workspace=PROJECT_ROOT,
            accept_result=is_automation_error_audit_result,
        )
        for run in recovered_audits:
            await publish_automation_error_audit(app.client, settings, run)
            if run.run_id is not None:
                await asyncio.to_thread(
                    store.finish_scheduled_agent_run,
                    run.run_id,
                    status="completed",
                    external_session_id=run.session_id,
                    result_text=run.text,
                )
        await synchronize_archive(app.client, store, settings.backfill_limit)
        archive_task = asyncio.create_task(
            periodically_synchronize_archive(
                app.client,
                store,
                settings.backfill_limit,
                settings.sync_interval_seconds,
                on_new_message=handle_new_history_message,
            ),
            name="slack-archive-sync",
        )
        if vpn.configured:
            vpn_task = asyncio.create_task(
                _monitor_vpn(app, settings, vpn),
                name="vpn-monitor",
            )
        if settings.experiment_finalizer_enabled:
            experiment_finalizer_task = asyncio.create_task(
                run_daily(
                    app.client,
                    orchestrator,
                    settings,
                    lambda: not automation_controls.disabled("crons", finalizer_job.display_name),
                ),
                name="daily-experiment-finalizer",
            )
        if settings.experiment_design_enabled:
            experiment_design_task = asyncio.create_task(
                run_daily_experiment_design(
                    app.client,
                    orchestrator,
                    settings,
                    lambda: not automation_controls.disabled(
                        "crons", experiment_design_job.display_name
                    ),
                ),
                name="daily-experiment-design-planner",
            )
        automation_error_audit_task = asyncio.create_task(
            run_daily_automation_error_audit(
                app.client,
                orchestrator,
                settings,
                lambda: not automation_controls.disabled("crons", error_audit_job.display_name),
            ),
            name="daily-automation-error-audit",
        )
        LOGGER.info(
            "Sloperator started; health http://%s:%d/healthz; admin /admin; archive %s",
            settings.host,
            settings.port,
            store.path,
        )
        await stop_event.wait()
    finally:
        LOGGER.info("Sloperator is shutting down")
        if archive_task is not None:
            archive_task.cancel()
            with suppress(asyncio.CancelledError):
                await archive_task
        if vpn_task is not None:
            vpn_task.cancel()
            with suppress(asyncio.CancelledError):
                await vpn_task
        await cancel_task(experiment_finalizer_task)
        await cancel_experiment_design(experiment_design_task)
        await cancel_automation_error_audit(automation_error_audit_task)
        await orchestrator.close()
        await slack_handler.close_async()  # type: ignore[no-untyped-call]
        await runner.cleanup()


async def _monitor_vpn(
    app: AsyncApp,
    settings: Settings,
    vpn: VpnManager,
) -> None:
    """Report unavailable VPN and wait for explicit owner readiness."""
    client = app.client
    ready_notice_sent = False
    waiting_notice_sent = False

    while True:
        try:
            state = await vpn.state()

            if state in {VpnState.STOPPED, VpnState.FAILED} and not ready_notice_sent:
                message = (
                    "VPN сейчас не подключён. Когда будешь готов сразу прислать "
                    "одноразовый код, напиши `vpn ready` или `готов`. "
                    "Только после этого я начну подключение."
                )
                conversation = await client.conversations_open(users=settings.slack_user_id)
                await client.chat_postMessage(
                    channel=conversation["channel"]["id"],
                    text=message,
                )
                ready_notice_sent = True
            elif state is VpnState.WAITING_OTP and not waiting_notice_sent:
                message = (
                    "VPN запущен, LDAP принят. Нужен одноразовый код: "
                    "пришлите сюда 6-8 цифр отдельным сообщением."
                )
                conversation = await client.conversations_open(users=settings.slack_user_id)
                await client.chat_postMessage(
                    channel=conversation["channel"]["id"],
                    text=message,
                )
                waiting_notice_sent = True
            elif state is VpnState.CONNECTED:
                ready_notice_sent = False
                waiting_notice_sent = False
        except VpnError as error:
            LOGGER.error("VPN monitoring failed: %s", type(error).__name__)

        await asyncio.sleep(60)


def run() -> None:
    """CLI entry point."""
    try:
        settings = Settings.from_environment()
    except ConfigurationError as error:
        raise SystemExit(f"Configuration error: {error}") from error

    configure_logging(settings.log_level)
    with suppress(KeyboardInterrupt):
        asyncio.run(serve(settings))


if __name__ == "__main__":
    run()
