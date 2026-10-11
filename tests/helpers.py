"""Shared test doubles. Nothing here ever touches the network or real GitHub."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from requests.structures import CaseInsensitiveDict

from src.github_client import GitHubError

NOW = datetime(2026, 10, 10, 16, 30, 0, tzinfo=timezone.utc)
TOKEN = "ghp_SUPERSECRETTOKENVALUE1234567890"


class FakeResponse:
    """Minimal stand-in for ``requests.Response``."""

    def __init__(self, status: int = 200, body: Any = None, headers: dict[str, str] | None = None,
                 bad_json: bool = False) -> None:
        self.status_code = status
        self._body = body
        self._bad_json = bad_json
        self.headers = CaseInsensitiveDict(headers or {})

    def json(self) -> Any:
        if self._bad_json:
            raise ValueError("not json")
        return self._body


class FakeSession:
    """Replays queued responses/exceptions and records every call."""

    def __init__(self, responses: list[Any]) -> None:
        self.headers: dict[str, str] = {}
        self.responses = list(responses)
        self.calls: list[tuple[str, str, Any]] = []

    def request(self, method: str, url: str, params: Any = None, timeout: Any = None) -> Any:
        self.calls.append((method, url, params))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_item(login: str, user_id: int, kind: str = "User") -> dict[str, Any]:
    """A search-result item shaped like GitHub's."""
    return {"login": login, "id": user_id, "type": kind}


def make_profile(login: str, created: str = "2020-01-01T00:00:00Z", kind: str = "User") -> dict[str, Any]:
    """A /users/{login} body shaped like GitHub's (only the fields we use)."""
    return {"login": login, "id": 1, "type": kind, "created_at": created}


class FakeGitHub:
    """Duck-typed replacement for GitHubClient used by discovery/service tests."""

    def __init__(self, pages: list[dict[str, Any]] | None = None) -> None:
        self.pages = pages or []
        self.profiles: dict[str, dict[str, Any] | None] = {}
        self.following: set[str] = set()
        self.follow_errors: dict[str, Exception] = {}
        self.search_calls = 0
        self.follow_calls: list[str] = []

    def search_users(self, query: str, *, sort: Any, order: str, page: int, per_page: int) -> dict[str, Any]:
        self.search_calls += 1
        if page - 1 < len(self.pages):
            page_data = self.pages[page - 1]
            if isinstance(page_data, Exception):
                raise page_data
            return page_data
        return {"items": [], "total_count": 0}

    def get_user(self, login: str) -> dict[str, Any] | None:
        if login in self.profiles:
            return self.profiles[login]
        return make_profile(login)

    def is_following(self, login: str) -> bool:
        return login in self.following

    def follow(self, login: str) -> bool:
        self.follow_calls.append(login)
        if login in self.follow_errors:
            raise self.follow_errors[login]
        return True


def page_of(items: list[dict[str, Any]], total: int | None = None, incomplete: bool = False) -> dict[str, Any]:
    return {"items": items, "total_count": total if total is not None else len(items),
            "incomplete_results": incomplete}


__all__ = ["NOW", "TOKEN", "FakeResponse", "FakeSession", "FakeGitHub", "GitHubError",
           "make_item", "make_profile", "page_of"]
