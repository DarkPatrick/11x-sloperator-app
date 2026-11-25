"""Unit tests for helpers of sloperator.jira_code_tasks (acceptance tests live separately)."""

from __future__ import annotations

from sloperator import jira_code_tasks as tasks
from sloperator.jira_task_automation import jql_string


def test_verdict_must_be_the_last_non_empty_line() -> None:
    assert tasks.verdict_of("Report\nCODE_TASK_PR_OPENED\n\n") == "CODE_TASK_PR_OPENED"
    assert tasks.verdict_of("CODE_TASK_WAITING\nmore") is None
    assert tasks.verdict_of("Report\nTASK_DONE") is None
    assert tasks.verdict_of("") is None


def test_pull_request_urls_keep_only_muse_tech_pulls_on_pr_url_lines() -> None:
    text = (
        "see https://github.com/muse-tech/a/pull/1\n"
        "PR_URL: https://github.com/muse-tech/backend-ultimate-guitar/pull/3600\n"
        "  PR_URL:   https://github.com/muse-tech/ug.web_v2/pull/7  \n"
        "PR_URL: https://github.com/muse-tech/a/pull/1/files\n"
        "PR_URL: http://github.com/muse-tech/a/pull/2\n"
        "PR_URL: https://github.com/other/a/pull/3\n"
    )
    assert tasks.pull_request_urls(text) == [
        "https://github.com/muse-tech/backend-ultimate-guitar/pull/3600",
        "https://github.com/muse-tech/ug.web_v2/pull/7",
    ]


def test_new_human_comments_skip_service_account_and_old_ones() -> None:
    comments = [
        {"id": "1", "created": "2026-11-20T10:00:00.000+0000", "author": {"accountId": "a"}},
        {"id": "2", "created": "2026-11-21T10:00:00.000+0000",
         "author": {"accountId": tasks.SERVICE_ACCOUNT_ID}},
        {"id": "3", "created": "2026-11-22T10:00:00.000+0000", "author": {"accountId": "b"}},
        {"created": "2026-11-23T10:00:00.000+0000", "author": {"accountId": "b"}},
    ]
    found = tasks._new_human_comments(comments, "2026-11-20T10:00:00.000+0000")
    assert [comment["id"] for _, comment in found] == ["3"]
    assert len(tasks._new_human_comments(comments, None)) == 2


def test_jql_string_escapes_quotes_and_backslashes() -> None:
    assert jql_string('a"b\\c') == '"a\\"b\\\\c"'


def test_prompts_carry_the_service_interpreter_and_comment_id() -> None:
    reply = tasks.reply_prompt("UMN-1", "4242")
    assert reply.startswith("[claude]")
    assert "--reply-to-comment-id 4242" in reply
    assert "-m sloperator.workspace_session wait --task UMN-1" in tasks.continue_prompt("UMN-1")


def test_review_rules_render_without_leftover_placeholders() -> None:
    for prompt in (tasks.continue_prompt("UMN-1"), tasks.reply_prompt("UMN-1", "7"),
                   tasks.ORCHESTRATOR_PROMPT):
        assert "{" not in prompt.replace("${", "") and "}" not in prompt
        assert f"at most {tasks.MAX_REVIEW_ROUNDS} review" in prompt
    reply = " ".join(tasks.reply_prompt("UMN-1", "7").split())
    assert reply.index("deep review loop again") < reply.index("--reply-to-comment-id 7")


def test_orchestrator_rules_set_jira_start_and_due_dates() -> None:
    from sloperator import jira_code_tasks as jct

    for prompt in (
        jct.continue_prompt("UMN-1"),
        jct.reply_prompt("UMN-1", "42"),
    ):
        assert "set-start-date UMN-1 --date" in prompt
        assert "set-due-date UMN-1 --date" in prompt
        assert "never pass --replace" in prompt
