import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import requests

from .store import stamp


class APIError(RuntimeError):
    def __init__(self, path: str, status: int, reason: str = "GitHub request failed"):
        self.path = path
        self.status = status
        # Never retain API bodies, signed download URLs, or request credentials.
        super().__init__(f"{reason} ({status}) at {path.split('?')[0]}")


class BudgetExceeded(RuntimeError):
    def __init__(self, message="", *, rate_limited=False):
        super().__init__(message)
        self.rate_limited = rate_limited


class GitHub:
    def __init__(self, token: str, budget: int = 4000, session=None, sleep=time.sleep):
        self.session = session or requests.Session()
        self.token = token
        self.budget = budget
        self.limit = budget
        self.used = 0
        self.sleep = sleep
        self.remaining = None
        self.deadline = time.monotonic() + 45 * 60

    def check_deadline(self):
        if time.monotonic() >= self.deadline:
            raise BudgetExceeded("Collection time limit reached. Progress will be saved.")

    def request(self, path: str, params=None):
        if not path.startswith("/repos/"):
            raise ValueError("Only read-only repository API paths are allowed")
        for retry in range(3):
            self.check_deadline()
            if self.used >= min(self.budget, self.limit):
                raise BudgetExceeded("Request budget reached. Checkpoint remains resumable.")
            if self.remaining is not None and self.remaining <= 50:
                raise BudgetExceeded(
                    "Rate-limit reserve reached. Refresh will resume later.", rate_limited=True
                )
            self.used += 1
            try:
                response = self.session.get(
                    "https://api.github.com" + path,
                    params=params,
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                        "User-Agent": "dotnet-labeler-dashboard",
                    },
                    timeout=(15, 60),
                    allow_redirects=False,
                )
            except requests.RequestException:
                if retry == 2:
                    raise APIError(path, 0, "Network request failed") from None
                self.sleep(2**retry)
                continue
            remaining = response.headers.get("X-RateLimit-Remaining")
            if remaining is not None:
                self.remaining = int(remaining)
            status = response.status_code
            limited = status == 429 or (
                status == 403 and ("Retry-After" in response.headers or self.remaining == 0)
            )
            if limited:
                # Defer long waits to the next hourly run rather than hammering the API.
                delay = float(response.headers.get("Retry-After", 60))
                if self.remaining == 0 or delay > 60 or retry == 2:
                    raise BudgetExceeded("GitHub rate limit. Refresh deferred.", rate_limited=True)
                self.sleep(max(delay, 1))
                continue
            if status >= 500 and retry < 2:
                self.sleep(2**retry)
                continue
            if status >= 400:
                raise APIError(path, status)
            return response
        raise APIError(path, 0, "Retries exhausted")

    def get(self, path: str, params=None):
        response = self.request(path, params)
        if response.status_code != 200:
            raise APIError(path, response.status_code, "Unexpected API response")
        try:
            return response.json()
        except ValueError:
            raise APIError(path, 200, "Invalid JSON response") from None

    def pages(self, path: str, params=None, field: str | None = None):
        page = 1
        while True:
            response = self.request(path, {**(params or {}), "per_page": 100, "page": page})
            if response.status_code != 200:
                raise APIError(path, response.status_code, "Unexpected pagination response")
            try:
                data = response.json()
                rows = data[field] if field else data
            except (ValueError, KeyError, TypeError):
                raise APIError(path, 200, "Invalid paginated response") from None
            if not isinstance(rows, list):
                raise APIError(path, 200, "Expected a list")
            yield rows
            if "next" not in response.links:
                return
            page += 1

    def log(self, repo: str, job_id: int) -> str:
        path = f"/repos/{repo}/actions/jobs/{job_id}/logs"
        response = self.request(path)
        if response.status_code == 200:
            if len(response.content) > 20_000_000:
                raise APIError(path, 200, "Log exceeds 20 MB safety limit")
            return response.text
        if response.status_code != 302:
            raise APIError(path, response.status_code, "Unexpected log response")
        location = response.headers.get("Location", "")
        parsed = urlparse(location)
        host = parsed.hostname or ""
        if (
            parsed.scheme != "https"
            or parsed.username
            or not (
                host.endswith(".blob.core.windows.net")
                or host.endswith(".githubusercontent.com")
                or host.endswith(".actions.githubusercontent.com")
                or host == "api.github.com"
            )
        ):
            raise APIError(path, 302, "Unexpected log download host")
        if self.used >= min(self.budget, self.limit):
            raise BudgetExceeded("Request budget reached before log download")
        self.check_deadline()
        self.used += 1
        try:
            # A fresh, unauthenticated request prevents forwarding the source token.
            with requests.get(
                location, timeout=(15, 60), stream=True, allow_redirects=False
            ) as download:
                if download.status_code != 200:
                    raise APIError(path, download.status_code, "Log download failed")
                chunks = []
                size = 0
                for chunk in download.iter_content(64 * 1024):
                    self.check_deadline()
                    size += len(chunk)
                    if size > 20_000_000:
                        raise APIError(path, 200, "Log exceeds 20 MB safety limit")
                    chunks.append(chunk)
                return b"".join(chunks).decode("utf-8", errors="replace")
        except requests.RequestException:
            raise APIError(path, 0, "Log download failed") from None

    def runs_in_window(self, repo: str, workflow: int, start: datetime, end: datetime):
        """Half-open windows are split before GitHub's 1,000-result cap."""
        if start >= end:
            return []
        path = f"/repos/{repo}/actions/workflows/{workflow}/runs"
        params = {
            "created": f"{stamp(start)}..{stamp(end - timedelta(seconds=1))}",
            "per_page": 100,
        }
        first = self.get(path, {**params, "page": 1})
        count = first["total_count"]
        if count >= 1000:
            seconds = int((end - start).total_seconds())
            if seconds <= 1:
                raise APIError(path, 200, "Run window exceeds GitHub's query cap")
            middle = start + timedelta(seconds=seconds // 2)
            return self.runs_in_window(repo, workflow, start, middle) + self.runs_in_window(
                repo, workflow, middle, end
            )
        runs = list(first["workflow_runs"])
        for page in range(2, (count + 99) // 100 + 1):
            runs.extend(self.get(path, {**params, "page": page})["workflow_runs"])
        unique = {run["id"]: run for run in runs}
        if len(unique) != count:
            raise APIError(path, 200, "Run pagination changed. Window must be retried")
        return list(unique.values())


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)
