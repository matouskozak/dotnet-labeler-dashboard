import argparse
import json
import os
import sys
from pathlib import Path

from .collect import Collector
from .github import GitHub, utcnow
from .render import render
from .store import load, save


def main():
    parser = argparse.ArgumentParser(description="Read-only .NET labeler dashboard")
    parser.add_argument("command", choices=["refresh", "render"])
    parser.add_argument("--config", type=Path, default=Path("config/repos.json"))
    parser.add_argument("--state", type=Path, default=Path(".state/state.json"))
    parser.add_argument("--output", type=Path, default=Path("_site"))
    parser.add_argument("--max-requests", type=int, default=600)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    if config.get("schema") != 1 or args.max_requests < 50:
        parser.error("Expected configuration schema 1 and a request budget of at least 50")
    state = load(args.state)
    now = utcnow()
    if args.command == "refresh":
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not token:
            parser.error("GH_TOKEN is required. Never pass a credential on the command line.")
        collector = Collector(GitHub(token, budget=args.max_requests), state, config, now)
        try:
            collector.refresh()
        finally:
            save(args.state, state)
        print(
            f"Persisted {len(state['items'])} items, {len(state['events'])} label events, "
            f"{len(state['attempts'])} item attempts. Requests: {collector.api.used}. "
            f"Backfill paused: {state.get('budget_paused', False)}.",
            flush=True,
        )
        checks = [
            check for repo in state["permissions"].values() for check in repo["checks"].values()
        ]
        if collector.failed or not checks or any(not check["ok"] for check in checks):
            print(
                "Collection or source preflight incomplete. See persisted operations.",
                file=sys.stderr,
            )
            return 2
    else:
        render(state, config, now, args.output)
        print(f"Rendered persisted evidence to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
