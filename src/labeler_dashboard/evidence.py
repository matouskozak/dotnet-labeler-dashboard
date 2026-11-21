import re

from .store import area_label, instant, relevant_label, stamp

# These are action source identities, NOT model identities.
SUPPORTED_ACTIONS = {
    "94442c952121cb5448aaa7e247f65be93c07632e": "v2.3.0",
    "160b6b1e1e8d36da09beb34ef1e4806d2c0520a3": "v2.2.0",
    "46125e85e6a568dc712f358c39f35317366f5eed": "v2.0.0",
}
PRODUCTION_EVENTS = {"issues", "pull_request", "pull_request_target", "schedule"}
LINE = re.compile(r"^(\d{4}-\d\d-\d\dT[\d:.]+Z) (.*)$")
RESULT = re.compile(r"^\[(Issue|Pull Request) ([\w.-]+/[\w.-]+)#(\d+)] (.+)$")
PARSER_VERSION = 3
NO_SELECTION_OUTCOMES = {"fallback_applied", "fallback_present", "no_selection"}


def no_selection(observation):
    return bool(observation["action_version"]) and observation["outcome"] in NO_SELECTION_OUTCOMES


def parse_log(text: str, repo: str) -> dict:
    """Whitelist result records. Never retain arbitrary log text or shell error echoes."""
    result = {
        "action_sha": None,
        "action_version": None,
        "model_version": None,
        "threshold": None,
        "cache_hit": None,
        "dry_run": None,
        "observations": [],
        "explicit_failure": False,
        "explicit_no_work": False,
    }
    thresholds = set()
    dry_runs = set()
    shell_group = False
    for line in text.splitlines():
        match = LINE.match(line.lstrip("\ufeff"))
        if not match:
            continue
        timestamp, message = match.groups()
        if message.startswith("##[group]Run "):
            shell_group = not bool(
                re.fullmatch(r"##\[group]Run dotnet/issue-labeler(?:/predict)?@\S+", message)
            )
        if message == "##[endgroup]":
            shell_group = False
            continue
        if shell_group or message.startswith("##[command]"):
            continue
        action = re.fullmatch(
            r"Download action repository 'dotnet/issue-labeler@[^']+' \(SHA:([a-f0-9]{40})\)",
            message,
        )
        if action:
            result["action_sha"] = action[1]
            result["action_version"] = SUPPORTED_ACTIONS.get(action[1])
        threshold = re.fullmatch(r"  threshold: (0(?:\.\d+)?|1(?:\.0+)?)", message)
        if threshold:
            thresholds.add(float(threshold[1]))
        dry = re.fullmatch(r"  dry_run: (true|false)", message)
        if dry:
            dry_runs.add(dry[1] == "true")
        if message == "Cache restored successfully":
            result["cache_hit"] = True
        elif message.startswith("Cache not found for input keys:"):
            result["cache_hit"] = False
        # Only executed runner annotations, not strings embedded in printed scripts.
        if message.startswith("##[error]"):
            result["explicit_failure"] = True
        if message in {"No pull requests to process.", "No issues to process."}:
            result["explicit_no_work"] = True
        record = RESULT.fullmatch(message.removeprefix("##[notice]").removeprefix("##[error]"))
        if not record or record[2] != repo:
            continue
        kind, _, number, body = record.groups()
        observation = {
            "number": int(number),
            "kind": "issue" if kind == "Issue" else "pr",
            "at": stamp(instant(timestamp)),
            "outcome": "unsupported",
            "label": None,
        }
        patterns = (
            (
                "existing_area_skip",
                r"No prediction needed\. Applicable label '([^']+)' already exists\.",
            ),
            (
                "fallback_applied",
                r"No prediction made\. Default label '(needs-area-label)' applied\.",
            ),
            (
                "fallback_present",
                r"No prediction made\. Default label '(needs-area-label)' is already applied\.",
            ),
            ("area_applied", r"(?:^| )Label '([^']+)' applied\."),
            ("dry_run", r"(?:^| )(?:Label|Default label) '([^']+)' would be applied\."),
            ("fallback_removed", r"Default label '(needs-area-label)' removed\."),
        )
        # Supported predictor failures terminate the result. Error details are not results.
        success, error, _ = body.partition("Error occurred ")
        matches = []
        for outcome, pattern in patterns:
            for found in re.finditer(pattern, success):
                is_relevant = (
                    area_label(found[1], repo)
                    if outcome in {"area_applied", "existing_area_skip"}
                    else relevant_label(found[1], repo)
                )
                if is_relevant and len(found[1]) <= 100:
                    matches.append((found.start(), outcome, found[1]))
        for _, outcome, label in sorted(matches):
            result["observations"].append({**observation, "outcome": outcome, "label": label})
        if error:
            result["observations"].append({**observation, "outcome": "application_failure"})
            result["explicit_failure"] = True
        elif not matches:
            if body == "Queued for prediction.":
                observation["outcome"] = "queued"
            elif (
                body == "No prediction made. No applicable label found. No action taken."
                or body.startswith("No prediction was made. The prediction engine")
            ):
                observation["outcome"] = "no_selection"
            result["observations"].append(observation)
    result["threshold"] = next(iter(thresholds)) if len(thresholds) == 1 else None
    result["dry_run"] = next(iter(dry_runs)) if len(dry_runs) == 1 else None
    return result


def prediction_step(job: dict) -> dict | None:
    return next(
        (
            step
            for step in job.get("steps", [])
            if step["name"] in {"Predict issue labels", "Predict pull labels"}
        ),
        None,
    )


def outcome(observations: list[dict]) -> str:
    results = [row["outcome"] for row in observations if row["outcome"] != "queued"]
    return results[-1] if results else "unknown"
