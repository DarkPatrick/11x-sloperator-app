"""Prompt rules added after the agent benchmark's first chain run (UMN-14201)."""
from sloperator import experiment_analytics_planner as analytics
from sloperator import experiment_design_planner as design


def test_design_review_keeps_reproducible_numbers() -> None:
    text = design.review_prompt("UMN-1", "UMN-2", "123")
    assert "re-running its own linked query as it is" in text
    assert "keep the reproducible number" in text


def test_analytics_closed_lists_from_code_in_both_passes() -> None:
    assert "Closed lists come from the code" in analytics.PREPARATION_PROMPT
    assert "pre-existing rule" in analytics.PREPARATION_PROMPT
    assert "Closed lists come from the code" in analytics.review_prompt("UMN-1", "UMN-2", "123")
