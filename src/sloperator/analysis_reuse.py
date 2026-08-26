"""Shared recent-analysis reuse contract for automated alert agents."""

from __future__ import annotations


def recent_analysis_reuse_policy(
    channel_id: str,
    identity: str,
    mention_line: str | None = None,
) -> str:
    """Require a Slack-history preflight before an expensive repeated investigation."""
    result_contract = (
        "return exactly two lines: the mandatory mention line "
        f"`{mention_line}`, then"
        if mention_line
        else "return exactly this one line:"
    )
    return f"""\
Before the freshness preflight, repository research, or any data query, check whether this exact
alert was already investigated in Slack during the previous five days:
1. Use the available Slack read/search tools to read channel `{channel_id}` back to at least
   `now - 5 days`; paginate rather than relying only on the first page. Inspect candidate alert
   threads and the completed Sloperator analysis replies inside them.
2. Match on the complete set of selected `{identity}` identities in this prompt. Ignore changing
   observed values, percentages, Z/drift/Prophet values, timestamps, and diagnostic prose. A
   partial overlap, a different identity, a raw monitor alert without an analysis, or a
   progress/status reply is not a match.
3. Reuse only a completed prior analysis that covers every currently selected item. If several
   match, use the newest one. Do not assume its conclusion still applies and do not run even a
   small validation query: this path is only a routing/dedup action.
4. If a match exists, stop immediately and {result_contract}
   `SLOPERATOR_REUSE_ANALYSIS: <permalink to the existing completed analysis thread>`
   Do not create an HTML report, ZIP, `SLOPERATOR_ARTIFACT`, fresh diagnosis, or extra prose.
5. Only when no qualifying prior analysis exists, continue with the normal investigation and
   artifact contract below.
"""
