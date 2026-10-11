"""Configuration: read, validate and freeze every setting the bot uses.

All settings come from environment variables so the same code runs locally and
in GitHub Actions. Empty values are treated as "not set" because GitHub Actions
expands an undefined repository variable (``${{ vars.X }}``) to an empty string.

Nothing in this module ever prints or returns the token in a message.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

# Hard safety ceiling for follow attempts per run. GitHub's Acceptable Use
# Policies forbid excessive automated bulk activity, so even a mis-set
# MAX_FOLLOWS cannot go above this.
MAX_FOLLOWS_CEILING = 50

DEFAULT_SEARCH_QUERY = "type:user repos:>=1"
VALID_SORTS = frozenset({"joined", "followers", "repositories", "best-match"})
VALID_ORDERS = frozenset({"asc", "desc"})
VALID_ACCOUNT_TYPES = frozenset({"user", "organization", "bot"})

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class ConfigError(ValueError):
    """Raised when configuration is missing or invalid (never contains secrets)."""


def _text(env: Mapping[str, str], name: str, default: str = "") -> str:
    """Return a stripped env value, or ``default`` when unset/blank."""
    raw = env.get(name)
    if raw is None:
        return default
    raw = raw.strip()
    return raw if raw else default


def _int(env: Mapping[str, str], name: str, default: int, minimum: int, maximum: int) -> int:
    """Parse a bounded integer setting."""
    raw = _text(env, name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer (got {raw!r})") from None
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum} (got {value})")
    return value


def _float(env: Mapping[str, str], name: str, default: float, minimum: float, maximum: float) -> float:
    """Parse a bounded float setting."""
    raw = _text(env, name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number (got {raw!r})") from None
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum} (got {value})")
    return value


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    """Parse a boolean setting (true/false/1/0/yes/no/on/off)."""
    raw = _text(env, name).lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ConfigError(f"{name} must be true or false (got {raw!r})")


def _name_set(env: Mapping[str, str], name: str) -> frozenset[str]:
    """Parse a comma/space separated list into a lower-cased set (drops '@')."""
    raw = _text(env, name).replace(",", " ")
    return frozenset(item.lstrip("@").lower() for item in raw.split() if item.lstrip("@"))


@dataclass(frozen=True)
class Config:
    """Immutable, validated settings for one run."""

    # repr=False keeps the token out of logs, tracebacks and debugging output.
    token: str = field(repr=False)
    max_follows: int
    search_query: str
    search_language: str
    search_sort: str
    search_order: str
    max_search_pages: int
    account_types: frozenset[str]
    min_account_age_days: int
    excluded_users: frozenset[str]
    history_file: Path
    request_timeout: float
    max_retries: int
    max_rate_limit_wait: int
    follow_delay: float
    max_runtime_seconds: int
    max_consecutive_failures: int
    dry_run: bool
    api_url: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        """Build a Config from environment variables (defaults to ``os.environ``)."""
        env = os.environ if env is None else env

        # --- Authentication -------------------------------------------------
        token = _text(env, "GH_TOKEN")
        if not token:
            raise ConfigError(
                "GH_TOKEN is not set. Create a personal access token and store it "
                "as a repository secret named GH_TOKEN (see README)."
            )
        # ghs_ is the prefix of installation tokens, which includes the
        # built-in GITHUB_TOKEN. Those act as an app/bot and cannot follow users.
        if token.startswith("ghs_"):
            raise ConfigError(
                "GH_TOKEN looks like a GitHub Actions/App installation token (ghs_...). "
                "Those cannot follow users. Use a personal access token instead."
            )

        # --- Discovery ------------------------------------------------------
        sort = _text(env, "SEARCH_SORT", "joined").lower()
        if sort not in VALID_SORTS:
            raise ConfigError(f"SEARCH_SORT must be one of {sorted(VALID_SORTS)}")
        order = _text(env, "SEARCH_ORDER", "desc").lower()
        if order not in VALID_ORDERS:
            raise ConfigError(f"SEARCH_ORDER must be one of {sorted(VALID_ORDERS)}")

        # --- Eligibility ----------------------------------------------------
        types = _name_set(env, "ACCOUNT_TYPES") or frozenset({"user"})
        unknown = types - VALID_ACCOUNT_TYPES
        if unknown:
            raise ConfigError(f"ACCOUNT_TYPES contains unknown types {sorted(unknown)}")

        api_url = _text(env, "GITHUB_API_URL", "https://api.github.com").rstrip("/")
        if not api_url.startswith("https://"):
            raise ConfigError("GITHUB_API_URL must start with https://")

        return cls(
            token=token,
            max_follows=_int(env, "MAX_FOLLOWS", 5, 1, MAX_FOLLOWS_CEILING),
            search_query=_text(env, "SEARCH_QUERY", DEFAULT_SEARCH_QUERY),
            search_language=_text(env, "SEARCH_LANGUAGE"),
            search_sort=sort,
            search_order=order,
            max_search_pages=_int(env, "MAX_SEARCH_PAGES", 3, 1, 10),
            account_types=types,
            min_account_age_days=_int(env, "MIN_ACCOUNT_AGE_DAYS", 30, 0, 3650),
            excluded_users=_name_set(env, "EXCLUDED_USERS"),
            history_file=Path(_text(env, "HISTORY_FILE", "data/users_follows.txt")),
            request_timeout=_float(env, "REQUEST_TIMEOUT", 15.0, 1.0, 120.0),
            max_retries=_int(env, "MAX_RETRIES", 3, 0, 8),
            max_rate_limit_wait=_int(env, "MAX_RATE_LIMIT_WAIT", 60, 0, 900),
            follow_delay=_float(env, "FOLLOW_DELAY", 2.0, 0.0, 60.0),
            max_runtime_seconds=_int(env, "MAX_RUNTIME_SECONDS", 600, 30, 3300),
            max_consecutive_failures=_int(env, "MAX_CONSECUTIVE_FAILURES", 5, 1, 50),
            dry_run=_bool(env, "DRY_RUN", False),
            api_url=api_url,
        )
