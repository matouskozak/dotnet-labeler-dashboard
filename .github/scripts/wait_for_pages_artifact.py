import os
import re
import sys
import time

import requests


def wait_for_artifact(repository, run_id, token):
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository) or not run_id.isdigit() or not token:
        raise RuntimeError("Runner repository, run ID, and token are required.")
    url = f"https://api.github.com/repos/{repository}/actions/runs/{run_id}/artifacts"
    for attempt in range(6):
        try:
            response = requests.get(
                url,
                params={"name": "github-pages", "per_page": 100},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=(10, 30),
                allow_redirects=False,
            )
        except requests.RequestException:
            raise RuntimeError("Pages artifact metadata request failed.") from None
        if response.status_code != 200:
            raise RuntimeError(
                f"Pages artifact metadata request failed (HTTP {response.status_code})."
            )
        try:
            artifacts = response.json()["artifacts"]
            if not isinstance(artifacts, list) or not all(
                isinstance(row, dict) for row in artifacts
            ):
                raise ValueError()
            matches = [
                row
                for row in artifacts
                if row.get("name") == "github-pages" and row.get("expired") is False
            ]
        except (ValueError, KeyError, TypeError):
            raise RuntimeError("Invalid Pages artifact metadata response.") from None
        if len(matches) == 1 and isinstance(matches[0].get("id"), int):
            print(f"Pages artifact {matches[0]['id']} is visible.", flush=True)
            return
        if matches:
            raise RuntimeError("Expected exactly one valid github-pages artifact for this run.")
        if attempt < 5:
            print("Pages artifact metadata is not visible yet. Retrying in 10 seconds.", flush=True)
            time.sleep(10)
    raise RuntimeError("Pages artifact metadata did not become visible after six checks.")


if __name__ == "__main__":
    try:
        wait_for_artifact(
            os.environ.get("GITHUB_REPOSITORY", ""),
            os.environ.get("GITHUB_RUN_ID", ""),
            os.environ.get("GH_TOKEN", ""),
        )
    except RuntimeError as error:
        print(f"::error::{error}", file=sys.stderr)
        raise SystemExit(1) from None
