"""Profile discovery via the GitHub user search API.

Discovery only *finds* profiles. It never follows anyone and never decides
final eligibility (see eligibility.py / follow_service.py).

``DiscoveryService.discover`` is a lazy generator: the next search page is only
requested when the consumer asks for more candidates, so stopping early (limit
reached, rate limited, interrupted) automatically stops further API calls.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Container, Iterator

from src.github_client import GitHubError

logger = logging.getLogger(__name__)

PER_PAGE = 100  # maximum page size the search API allows
SEARCH_RESULT_CAP = 1000  # the search API only exposes the first 1000 results


@dataclass(frozen=True)
class Candidate:
    """A profile found by search. Identified by its stable numeric ID."""

    login: str
    id: int
    type: str


@dataclass
class DiscoveryStats:
    """Counters reported in the execution summary."""

    pages_fetched: int = 0
    raw_results: int = 0  # every item the API returned (incl. duplicates)
    malformed: int = 0  # items without a usable login/id
    unique: int = 0  # distinct accounts (by ID)
    skipped_self: int = 0  # the authenticated account itself
    skipped_history: int = 0  # already processed in an earlier run
    considered: int = 0  # handed to the follow service
    incomplete_results: bool = False  # GitHub warned the page may be partial
    exhausted: bool = False  # ran out of results before hitting limits


def build_query(base: str, language: str, min_age_days: int, now: datetime) -> str:
    """Compose the final search string from the configured pieces.

    * ``language`` adds ``language:<x>`` unless the base query already has one.
    * ``min_age_days`` adds ``created:<YYYY-MM-DD`` so GitHub itself excludes
      brand-new accounts (the age is re-checked later on the real profile).
    """
    parts = [base.strip()]
    if language and "language:" not in base:
        parts.append(f'language:"{language}"' if " " in language else f"language:{language}")
    if min_age_days > 0 and "created:" not in base:
        cutoff = (now - timedelta(days=min_age_days)).date()
        parts.append(f"created:<{cutoff.isoformat()}")
    return " ".join(part for part in parts if part)


def _parse_item(raw: Any) -> Candidate | None:
    """Validate one search item; None if it lacks a usable login and numeric id."""
    if not isinstance(raw, dict):
        return None
    login, user_id, account_type = raw.get("login"), raw.get("id"), raw.get("type")
    if not isinstance(login, str) or not login:
        return None
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        return None
    return Candidate(login=login, id=user_id, type=account_type if isinstance(account_type, str) else "")


class DiscoveryService:
    """Finds candidate profiles through ``GET /search/users``."""

    def __init__(
        self,
        client: Any,
        *,
        query: str,
        sort: str,
        order: str,
        max_pages: int,
        per_page: int = PER_PAGE,
    ) -> None:
        self._client = client
        self._query = query
        # "best-match" means "do not send a sort parameter".
        self._sort = None if sort == "best-match" else sort
        self._order = order
        # Never ask for pages beyond what the API will return.
        self._max_pages = max(1, min(max_pages, SEARCH_RESULT_CAP // per_page))
        self._per_page = per_page
        self.stats = DiscoveryStats()

    def discover(
        self,
        *,
        own_login: str,
        own_id: int | None,
        processed: Container[str],
    ) -> Iterator[Candidate]:
        """Yield new, unique candidates (own account and history already removed)."""
        stats = self.stats
        seen_ids: set[int] = set()
        own_login_lower = own_login.lower()

        for page in range(1, self._max_pages + 1):
            try:
                data = self._client.search_users(
                    self._query, sort=self._sort, order=self._order, page=page, per_page=self._per_page
                )
            except GitHubError as exc:
                # 422 = "only the first 1000 results are available" / bad query
                # for this page. Treat as the end of results, not a crash.
                if exc.status == 422:
                    logger.warning("Search stopped at page %d: %s", page, exc)
                    stats.exhausted = True
                    return
                raise  # auth, rate limit, transient: the caller decides

            stats.pages_fetched += 1
            if data.get("incomplete_results"):
                stats.incomplete_results = True
                logger.warning("GitHub reported incomplete search results on page %d", page)

            items = data["items"]
            if not items:
                stats.exhausted = True
                return

            for raw in items:
                stats.raw_results += 1
                candidate = _parse_item(raw)
                if candidate is None:
                    stats.malformed += 1
                    logger.warning("Skipping malformed search item")
                    continue
                if candidate.id in seen_ids:  # same account listed twice
                    continue
                seen_ids.add(candidate.id)
                stats.unique += 1

                if candidate.id == own_id or candidate.login.lower() == own_login_lower:
                    stats.skipped_self += 1
                    continue
                if candidate.login in processed:
                    stats.skipped_history += 1
                    continue

                stats.considered += 1
                yield candidate

            # A short page, or having reached the reported total, means no more.
            total = data.get("total_count")
            if len(items) < self._per_page or (isinstance(total, int) and page * self._per_page >= total):
                stats.exhausted = True
                return
