"""Local, content-free archive inspection CLI."""

from __future__ import annotations

import argparse

from sloperator.config import Settings
from sloperator.store import EventStore


def run() -> None:
    """Print local archive counts or the channel map."""
    parser = argparse.ArgumentParser(description="Inspect the local Sloperator archive")
    parser.add_argument("command", choices=("status", "channels"), nargs="?", default="status")
    parser.add_argument("--members-only", action="store_true")
    args = parser.parse_args()

    store = EventStore(Settings.from_environment().database_path)
    store.initialize()
    if args.command == "status":
        for key, value in store.summary().items():
            print(f"{key}: {value}")
        return

    for channel_id, name, kind, is_member in store.channel_map():
        if args.members_only and not is_member:
            continue
        marker = "*" if is_member else "-"
        print(f"{marker} {channel_id} {kind} {name or ''}".rstrip())


if __name__ == "__main__":
    run()
