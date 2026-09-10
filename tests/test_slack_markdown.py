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


def test_native_mentions_preserve_formatting_links_and_code() -> None:
    from sloperator.agents import slack_message_payload

    text = (
        '<@U0149RHN7D3> <@U09CYCGN6H4> <@U0525MDT0MN>\n\n'
        '## Result\n**Impact:** [Report](https://example.com/report)\n'
        '<https://example.com|Native link> `**literal**`\n'
        '```\n**literal**\n```'
    )
    assert slack_message_payload(text) == {'text': (
        '<@U0149RHN7D3> <@U09CYCGN6H4> <@U0525MDT0MN>\n\n'
        '*Result*\n*Impact:* <https://example.com/report|Report>\n'
        '<https://example.com|Native link> `**literal**`\n'
        '```\n**literal**\n```'
    )}
