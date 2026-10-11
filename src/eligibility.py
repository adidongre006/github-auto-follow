"""Eligibility rules: decide whether a discovered profile may be followed.

Two cheap stages keep API usage low:

1. ``check_candidate`` uses only what the search result already told us
   (username and account type) - no extra API request needed.
2. ``check_profile`` uses the public profile (``type``, ``created_at``) and is
   only called for candidates that passed stage 1.

Every decision carries a short human-readable reason that is logged, so each
accept/reject is explainable. Only documented, public API fields are used.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.discovery import Candidate


@dataclass(frozen=True)
class EligibilityCriteria:
    """The configurable rules (built from Config in main.py)."""

    account_types: frozenset[str]  # lower-case, e.g. {"user"}
    excluded_users: frozenset[str]  # lower-case usernames
    min_account_age_days: int = 0  # 0 disables the age rule


@dataclass(frozen=True)
class Decision:
    """Outcome of an eligibility check plus the reason (for logs)."""

    eligible: bool
    reason: str


_OK = Decision(True, "eligible")


def parse_github_datetime(value: Any) -> datetime | None:
    """Parse GitHub's ISO-8601 timestamps (``2011-01-25T18:44:36Z``); None if invalid."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def check_candidate(candidate: Candidate, criteria: EligibilityCriteria) -> Decision:
    """Stage 1: rules that need nothing beyond the search result."""
    if candidate.login.lower() in criteria.excluded_users:
        return Decision(False, "username is on the exclusion list")
    if candidate.type and candidate.type.lower() not in criteria.account_types:
        return Decision(False, f"account type {candidate.type!r} is not allowed")
    return _OK


def check_profile(profile: dict[str, Any], criteria: EligibilityCriteria, now: datetime) -> Decision:
    """Stage 2: rules that need the public profile from ``GET /users/{username}``."""
    profile_type = profile.get("type")
    if isinstance(profile_type, str) and profile_type.lower() not in criteria.account_types:
        return Decision(False, f"account type {profile_type!r} is not allowed")

    if criteria.min_account_age_days > 0:
        created = parse_github_datetime(profile.get("created_at"))
        # No reliable creation date -> we cannot evaluate the rule, so skip it
        # rather than guessing (the search query also filters by age server-side).
        if created is not None:
            age_days = (now - created).days
            if age_days < criteria.min_account_age_days:
                return Decision(
                    False,
                    f"account is {age_days} days old (minimum {criteria.min_account_age_days})",
                )
    return _OK
