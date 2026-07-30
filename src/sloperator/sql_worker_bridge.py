"""Execute one bounded ClickHouse query using ug-ai-analyst's virtualenv."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--max-rows", type=int, required=True)
    args = parser.parse_args()

    workspace = args.workspace.resolve()
    # Running this file directly puts ``src/sloperator`` on sys.path, where
    # inspect.py would shadow Python's standard-library inspect module.
    script_directory = Path(__file__).resolve().parent
    sys.path = [
        entry
        for entry in sys.path
        if Path(entry or ".").resolve() != script_directory
    ]
    sys.path.insert(0, str(workspace / ".claude" / "clickhouse"))
    import clickhouse_env  # type: ignore[import-not-found]  # noqa: F401
    from clickhouse_worker import execute_sql  # type: ignore[import-not-found]

    frame = execute_sql(sys.stdin.read(), max_rows=args.max_rows)
    payload = json.loads(frame.to_json(orient="split", date_format="iso", default_handler=str))
    print(
        json.dumps(
            {
                "columns": payload["columns"],
                "rows": payload["data"],
                "row_count": len(frame.index),
                "truncated": len(frame.index) >= args.max_rows,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
