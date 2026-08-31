from sloperator.agents import normalize_slack_markdown


def test_summary_fields_are_not_rendered_as_inline_code_in_slack() -> None:
    text = (
        "**Web | conversion**\n"
        "`Alert: Real, but not a business problem.`\n"
        "`Cause: Bot traffic inflated the denominator.`\n"
        "Use `card 6243` for details."
    )

    assert normalize_slack_markdown(text) == (
        "**Web | conversion**\n"
        "**Alert:** Real, but not a business problem.\n"
        "**Cause:** Bot traffic inflated the denominator.\n"
        "Use `card 6243` for details."
    )
