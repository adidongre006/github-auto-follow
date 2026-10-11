"""Thin, safe wrapper around the GitHub REST API.

Responsibilities
----------------
* Send authenticated requests with a timeout.
* Retry transient failures (network errors, 5xx) with bounded exponential backoff.
* Honour rate limits (``Retry-After`` and ``X-RateLimit-Reset``) but never wait
  longer than a configured cap: if GitHub says "come back later", we stop.
* Translate HTTP failures into specific exception types so callers can react
  differently to auth problems, permission problems, rate limits and so on.
* Never put the token, or any request header, into an error message or log.

Endpoints used (all documented GitHub REST API v3 endpoints):
    GET  /user                          who am I
    GET  /search/users                  discover profiles
    GET  /users/{username}              public profile details
    GET  /user/following/{username}     am I already following them? (204/404)
    PUT  /user/following/{username}     follow (204 = success)
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

API_VERSION = "2022-11-28"
RETRYABLE_STATUS = frozenset({500, 502, 503, 504})
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 30.0
# Used when GitHub signals a "secondary" rate limit without telling us how long.
DEFAULT_SECONDARY_WAIT_SECONDS = 60.0


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class GitHubError(Exception):
    """Base class for API failures. Messages never contain credentials."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class AuthenticationError(GitHubError):
    """401: the token is missing, expired or revoked."""


class PermissionDeniedError(GitHubError):
    """403 that is not a rate limit: the token lacks a required permission."""


class RateLimitError(GitHubError):
    """GitHub asked us to wait longer than we are willing to."""

    def __init__(self, wait_seconds: float) -> None:
        super().__init__(f"GitHub rate limit reached; retry in about {int(wait_seconds)}s", 429)
        self.wait_seconds = wait_seconds


class TransientError(GitHubError):
    """Network trouble or 5xx responses that persisted after all retries."""


class MalformedResponseError(GitHubError):
    """The response body was not the JSON shape the API documents."""


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class GitHubClient:
    """Minimal GitHub REST client used by discovery and the follow service."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.github.com",
        timeout: float = 15.0,
        max_retries: int = 3,
        max_rate_limit_wait: float = 60.0,
        session: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._max_rate_limit_wait = max_rate_limit_wait
        self._sleep = sleep  # injectable so tests never really sleep
        self._clock = clock
        self._session = session if session is not None else requests.Session()
        # The Authorization header lives only on the session object. It is
        # never logged and never copied into exception messages.
        self._session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "Authorization": f"Bearer {token}",
                "User-Agent": "github-profile-automation",
            }
        )

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def get_authenticated_user(self) -> dict[str, Any]:
        """GET /user. Returns the account that owns the token."""
        data = self._json(self._request("GET", "/user"))
        if not isinstance(data, dict) or not isinstance(data.get("login"), str) or not isinstance(data.get("id"), int):
            raise MalformedResponseError("GET /user returned an unexpected body")
        return data

    def search_users(
        self,
        query: str,
        *,
        sort: str | None,
        order: str,
        page: int,
        per_page: int,
    ) -> dict[str, Any]:
        """GET /search/users. Returns the parsed body (must contain ``items``)."""
        params: dict[str, Any] = {"q": query, "per_page": per_page, "page": page}
        if sort:
            params["sort"] = sort
            params["order"] = order
        data = self._json(self._request("GET", "/search/users", params=params))
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise MalformedResponseError("GET /search/users returned an unexpected body")
        return data

    def get_user(self, username: str) -> dict[str, Any] | None:
        """GET /users/{username}. Returns None when the account is gone/suspended (404)."""
        resp = self._request("GET", f"/users/{quote(username, safe='')}", allowed=(200, 404))
        if resp.status_code == 404:
            return None
        data = self._json(resp)
        if not isinstance(data, dict):
            raise MalformedResponseError("GET /users/{username} returned an unexpected body")
        return data

    def is_following(self, username: str) -> bool:
        """GET /user/following/{username}. 204 = following, 404 = not following."""
        resp = self._request("GET", f"/user/following/{quote(username, safe='')}", allowed=(204, 404))
        return resp.status_code == 204

    def follow(self, username: str) -> bool:
        """PUT /user/following/{username}. True only on an explicit 204.

        Any other outcome raises, so a caller can never mistake an ambiguous
        response for success.
        """
        path = f"/user/following/{quote(username, safe='')}"
        try:
            self._request("PUT", path, allowed=(204,))
        except GitHubError as exc:
            if exc.status == 404 and type(exc) is GitHubError:
                # GitHub returns 404 for missing users *and* for tokens that
                # lack permission, so say both.
                raise GitHubError(
                    "Follow returned 404 (user not found, or the token lacks follow permission)",
                    404,
                ) from None
            raise
        return True

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        allowed: tuple[int, ...] = (200,),
    ) -> Any:
        """Send one logical request, with bounded retries. Returns the response."""
        url = f"{self._base_url}{path}"
        for attempt in range(self._max_retries + 1):
            is_last_attempt = attempt == self._max_retries

            # --- send -------------------------------------------------------
            try:
                resp = self._session.request(method, url, params=params, timeout=self._timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                # Only the exception *type* is reported: messages from the
                # requests library can echo URLs and are not needed here.
                if is_last_attempt:
                    raise TransientError(
                        f"Network error after {attempt + 1} attempts: {type(exc).__name__}"
                    ) from None
                self._backoff(attempt, f"network error ({type(exc).__name__})")
                continue
            except requests.RequestException as exc:
                raise GitHubError(f"Request failed: {type(exc).__name__}") from None

            status = resp.status_code

            # --- classify ---------------------------------------------------
            if status == 401:
                raise AuthenticationError("GitHub rejected the token (401). Is it valid and unexpired?", 401)

            wait = self._rate_limit_wait(resp)
            if wait is not None:
                if is_last_attempt or wait > self._max_rate_limit_wait:
                    raise RateLimitError(wait)
                logger.warning("Rate limited; waiting %.0fs before retrying", wait)
                self._sleep(wait)
                continue

            if status in RETRYABLE_STATUS:
                if is_last_attempt:
                    raise TransientError(f"GitHub returned {status} after {attempt + 1} attempts", status)
                self._backoff(attempt, f"HTTP {status}")
                continue

            if status in allowed:
                return resp

            message = self._message(resp)
            if status == 403:
                raise PermissionDeniedError(f"Forbidden (403): {message or 'token lacks a required permission'}", 403)
            raise GitHubError(f"Unexpected response {status}: {message}".rstrip(": "), status)

        # The loop always returns or raises; this is a defensive backstop.
        raise TransientError("Retries exhausted")  # pragma: no cover

    def _backoff(self, attempt: int, reason: str) -> None:
        """Sleep 1s, 2s, 4s, ... capped, before retrying a transient failure."""
        delay = min(BACKOFF_BASE_SECONDS * (2**attempt), BACKOFF_CAP_SECONDS)
        logger.warning("Transient failure (%s); retrying in %.0fs", reason, delay)
        self._sleep(delay)

    def _rate_limit_wait(self, resp: Any) -> float | None:
        """Return seconds to wait if this is a rate-limit response, else None."""
        status = resp.status_code
        headers = resp.headers
        retry_after = headers.get("Retry-After")
        remaining = headers.get("X-RateLimit-Remaining")

        limited = status == 429 or (
            status == 403
            and (remaining == "0" or retry_after is not None or "rate limit" in self._message(resp).lower())
        )
        if not limited:
            return None

        # 1) Explicit Retry-After (seconds) wins.
        if retry_after:
            try:
                return max(float(retry_after), 0.0)
            except ValueError:
                pass
        # 2) Primary limit: wait until the reset time GitHub reports.
        reset = headers.get("X-RateLimit-Reset")
        if remaining == "0" and reset:
            try:
                return max(float(reset) - self._clock() + 1.0, 1.0)
            except ValueError:
                pass
        # 3) Secondary limit without guidance: be conservative.
        return DEFAULT_SECONDARY_WAIT_SECONDS

    @staticmethod
    def _message(resp: Any) -> str:
        """Best-effort extraction of GitHub's ``message`` field (truncated)."""
        try:
            body = resp.json()
        except ValueError:
            return ""
        if isinstance(body, dict) and isinstance(body.get("message"), str):
            return body["message"][:200]
        return ""

    @staticmethod
    def _json(resp: Any) -> Any:
        """Decode a JSON body or raise MalformedResponseError."""
        try:
            return resp.json()
        except ValueError:
            raise MalformedResponseError("Response body was not valid JSON", resp.status_code) from None
