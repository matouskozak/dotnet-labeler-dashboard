import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = 1
AREA_PREFIXES = {"gitextensions/gitextensions": "area: "}


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def empty_state() -> dict:
    return {
        "schema": SCHEMA,
        "items": {},
        "events": {},
        "attempts": {},
        "runs": {},
        "repositories": {},
        "streams": {},
        "errors": {},
        "permissions": {},
    }


def load(path: Path) -> dict:
    if not path.exists():
        return empty_state()
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("schema") != SCHEMA:
        raise ValueError("Unsupported history schema. Refusing to overwrite evidence.")
    for key in empty_state():
        if key not in state:
            raise ValueError(f"History is missing {key}. Refusing to overwrite evidence.")
    return state


def save(path: Path, state: dict) -> None:
    """One replace commits evidence and checkpoints together, including after interruption."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(state, handle, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def record_error(state: dict, stage: str, message: str, now: str) -> None:
    key = f"{stage}:{message}"
    error = state["errors"].setdefault(
        key, {"stage": stage, "message": message, "first_seen": now, "count": 0}
    )
    error.update(last_seen=now, count=error["count"] + 1)


def stream_key(repo: str, kind: str) -> str:
    return f"{repo}:{kind}"


def pending_timeline(item):
    return bool(
        not item.get("observed_through")
        or item.get("timeline_source_updated") != item.get("updated_at")
        or item.get("timeline_error")
        or item.get("reconcile_requested", False)
    )


def timeline_coverage_through(item, repository):
    through = item.get("observed_through")
    checkpoint = repository.get("updated_checkpoint")
    if (
        through
        and checkpoint
        and repository.get("intake_complete")
        and repository.get("last_success") == checkpoint
        and not repository.get("last_error")
        and item.get("updated_at")
        and item["updated_at"] <= through
        and not pending_timeline(item)
        and not item.get("timeline_availability")
    ):
        # A complete updated-since scan corroborates an unchanged, fully fetched revision.
        return max(through, checkpoint)
    return through


def safe_github_url(value: str | None, repo: str, resource: str) -> str | None:
    import re

    pattern = rf"https://github\.com/{re.escape(repo)}/{resource}/[0-9]+"
    return value if isinstance(value, str) and re.fullmatch(pattern, value) else None


def area_label(label: str, repo: str = "") -> bool:
    prefix = AREA_PREFIXES.get(repo, "area-")
    return label.casefold().startswith(prefix) and bool(label[len(prefix) :].strip())


def relevant_label(label: str, repo: str = "") -> bool:
    return area_label(label, repo) or label == "needs-area-label"
