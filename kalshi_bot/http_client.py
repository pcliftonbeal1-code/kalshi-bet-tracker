"""Shared plumbing for talking to web APIs: rate limiting, retries, timeouts.

Both the Kalshi client and the sportsbook-odds client build on this, so the
"be polite and don't crash" logic is written (and tested) once.
"""

import time

import requests

# Status codes that mean "try again later" rather than "you did something wrong".
# 429 = too many requests (rate limited); 5xx = the server had a problem.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ApiError(Exception):
    """Raised when an API can't give us data, even after retrying."""


class HttpClient:
    """GET-only HTTP client. Subclasses set error_class to their own error type."""

    error_class = ApiError

    def __init__(self, base_url: str, requests_per_second: float = 5,
                 timeout_seconds: float = 15, max_retries: int = 4,
                 session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self.min_interval = 1.0 / requests_per_second
        self.timeout = timeout_seconds
        self.max_retries = max_retries
        # A Session reuses the same connection for many requests (faster).
        self.session = session or requests.Session()
        self._last_request_at = 0.0
        # Headers from the most recent successful response (some APIs report
        # your remaining quota here).
        self.last_headers: dict = {}

    def _redact(self, text: str) -> str:
        """Hide secrets in error text. Subclasses with API keys override this."""
        return text

    def _wait_for_rate_limit(self) -> None:
        """Sleep just long enough to keep under requests_per_second."""
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_request_at = time.monotonic()

    def get(self, path: str, params: dict | None = None) -> dict | list:
        """GET an endpoint and return the JSON body.

        Retries with "exponential backoff" (wait 1s, 2s, 4s, 8s) on network
        errors and retryable status codes. Raises error_class if all tries fail.
        """
        url = f"{self.base_url}/{path.lstrip('/')}"
        last_problem = ""
        for attempt in range(self.max_retries + 1):
            if attempt > 0:
                time.sleep(2 ** (attempt - 1))
            self._wait_for_rate_limit()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:  # no internet, DNS, timeout...
                last_problem = self._redact(f"network error: {exc}")
                continue
            if resp.status_code in RETRYABLE_STATUS:
                last_problem = f"HTTP {resp.status_code}"
                continue
            if resp.status_code != 200:
                # 400/401/404 won't fix themselves by retrying.
                raise self.error_class(self._redact(
                    f"GET {url} failed: HTTP {resp.status_code} {resp.text[:200]}"))
            # Lowercase the names so lookups don't depend on capitalization.
            headers = getattr(resp, "headers", None) or {}
            self.last_headers = {k.lower(): v for k, v in headers.items()}
            return resp.json()
        raise self.error_class(
            f"GET {url} gave up after {self.max_retries + 1} tries ({last_problem})")
