"""Follow service: runs candidates through eligibility checks and follows them.

Per candidate the pipeline is:

    cheap eligibility (search data)
      -> fetch public profile (404 = unavailable/suspended)
      -> profile eligibility (type, account age)
      -> already following?  (no-op, not an error)
      -> PUT follow  (only a 204 counts as success)
      -> append to history  (only after confirmed success)

The service stops safely when: the per-run attempt limit is reached, a rate
limit must be waited out, authentication/permissions fail, too many calls fail
in a row, or the caller asks it to stop (signal / time limit).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from src.discovery import Candidate
from src.eligibility import EligibilityCriteria, check_candidate, check_profile
from src.github_client import (
    AuthenticationError,
    GitHubError,
    PermissionDeniedError,
    RateLimitError,
)
from src.history import FollowHistory, format_timestamp

logger = logging.getLogger(__name__)


@dataclass
class RunStats:
    """Everything the execution summary needs to know about the follow phase."""

    attempts: int = 0  # follow requests sent (or simulated in dry-run)
    followed: int = 0  # confirmed by a 204
    already_following: int = 0
    rejected: int = 0  # failed an eligibility rule or profile unavailable
    failed: int = 0
    would_follow: int = 0  # dry-run only
    consecutive_failures: int = 0
    rate_limited: bool = False
    interrupted: bool = False
    limit_reached: bool = False
    fatal_error: str | None = None
    followed_users: list[tuple[str, str]] = field(default_factory=list)  # (login, UTC timestamp)


class FollowService:
    """Applies eligibility rules and performs follows."""

    def __init__(
        self,
        client: Any,
        history: FollowHistory,
        criteria: EligibilityCriteria,
        *,
        max_follows: int,
        follow_delay: float = 0.0,
        max_consecutive_failures: int = 5,
        dry_run: bool = False,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], None] = time.sleep,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> None:
        self._client = client
        self._history = history
        self._criteria = criteria
        self._max_follows = max_follows
        self._follow_delay = follow_delay
        self._max_consecutive_failures = max_consecutive_failures
        self._dry_run = dry_run
        self._clock = clock
        self._sleep = sleep
        self._should_stop = should_stop
        self.stats = RunStats()

    def run(self, candidates: Iterable[Candidate]) -> RunStats:
        """Process candidates until a stop condition is hit. Never raises API errors."""
        stats = self.stats
        iterator = iter(candidates)
        try:
            while True:
                # Stop conditions are checked BEFORE asking for the next
                # candidate, so we never trigger an unnecessary search request.
                if self._should_stop():
                    stats.interrupted = True
                    logger.warning("Stop requested; ending the run early")
                    break
                if stats.attempts >= self._max_follows:
                    stats.limit_reached = True
                    logger.info("Per-run follow limit (%d) reached", self._max_follows)
                    break
                if stats.consecutive_failures >= self._max_consecutive_failures:
                    stats.fatal_error = (
                        f"{stats.consecutive_failures} consecutive failures; aborting. "
                        "Check the token's permissions and GitHub's status page."
                    )
                    break
                try:
                    candidate = next(iterator)  # may fetch the next search page
                except StopIteration:
                    logger.info("No more candidates")
                    break

                try:
                    self._process(candidate)
                    stats.consecutive_failures = 0
                except (AuthenticationError, PermissionDeniedError, RateLimitError):
                    raise  # run-level problems, handled below
                except GitHubError as exc:
                    # Problem with this one profile: record it and move on.
                    stats.failed += 1
                    stats.consecutive_failures += 1
                    logger.warning("Failed to process %s: %s", candidate.login, exc)
        except RateLimitError as exc:
            stats.rate_limited = True
            logger.warning("Stopping because of rate limits: %s", exc)
        except (AuthenticationError, PermissionDeniedError) as exc:
            stats.fatal_error = str(exc)
            logger.error("Stopping: %s", exc)
        except GitHubError as exc:
            # Raised by the discovery generator (e.g. persistent 5xx on search).
            stats.fatal_error = f"Discovery failed: {exc}"
            logger.error("Stopping: %s", stats.fatal_error)
        except OSError as exc:
            # A follow succeeded but could not be written to history. Stop at once
            # rather than risk following the same people again next run.
            stats.fatal_error = f"Could not write follow history: {type(exc).__name__}"
            logger.error("Stopping: %s", stats.fatal_error)
        return stats

    # ------------------------------------------------------------------ #
    def _process(self, candidate: Candidate) -> None:
        """Run one candidate through the pipeline. May raise GitHubError."""
        stats = self.stats
        login = candidate.login

        decision = check_candidate(candidate, self._criteria)
        if not decision.eligible:
            self._reject(login, decision.reason)
            return

        profile = self._client.get_user(login)
        if profile is None:
            self._reject(login, "profile unavailable (not found or suspended)")
            return
        decision = check_profile(profile, self._criteria, self._clock())
        if not decision.eligible:
            self._reject(login, decision.reason)
            return

        if self._client.is_following(login):
            stats.already_following += 1
            logger.info("Already following %s; nothing to do", login)
            return

        if self._dry_run:
            stats.attempts += 1
            stats.would_follow += 1
            logger.info("DRY RUN: would follow %s", login)
            return

        stats.attempts += 1
        try:
            self._client.follow(login)  # raises unless GitHub answered 204
        finally:
            self._pause()

        # Only reached after a confirmed success.
        moment = self._clock()
        self._history.record(login, moment)
        stats.followed += 1
        stats.followed_users.append((login, format_timestamp(moment)))
        logger.info("Followed %s at %s", login, format_timestamp(moment))

    def _reject(self, login: str, reason: str) -> None:
        self.stats.rejected += 1
        logger.info("Skipping %s: %s", login, reason)

    def _pause(self) -> None:
        """Polite delay between follow requests (skipped after the last one)."""
        if self._follow_delay > 0 and self.stats.attempts < self._max_follows and not self._should_stop():
            self._sleep(self._follow_delay)
