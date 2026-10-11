"""Entry point: wires everything together and prints the execution summary.

Run with:  python -m src.main

Exit codes
----------
0    finished normally (including stopping early because of rate limits or the
     internal time limit - progress is saved, the next run continues)
1    failure: bad token/permissions, repeated API failures, history write error
2    configuration error (missing GH_TOKEN, invalid setting)
130  interrupted by SIGINT/SIGTERM (e.g. the workflow was cancelled)
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Callable, Mapping

from src.config import Config, ConfigError
from src.discovery import DiscoveryService, DiscoveryStats, build_query
from src.eligibility import EligibilityCriteria
from src.follow_service import FollowService, RunStats
from src.github_client import GitHubClient, GitHubError
from src.history import FollowHistory

logger = logging.getLogger("github_profile_automation")

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG = 2
EXIT_INTERRUPTED = 130


class SecretRedactionFilter(logging.Filter):
    """Defence in depth: replace any known secret found in a log message.

    The code never logs the token on purpose; this guarantees it even if a
    future change (or a third-party library) accidentally does.
    """

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [secret for secret in secrets if secret]

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = message
        for secret in self._secrets:
            redacted = redacted.replace(secret, "***")
        if redacted != message:
            record.msg, record.args = redacted, ()
        return True


class RunControl:
    """Tracks the reasons to stop early: a signal, or the run-time budget."""

    def __init__(self, max_runtime_seconds: float, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self._deadline = monotonic() + max_runtime_seconds
        self.reason: str | None = None  # "signal" or "time limit"

    def request_stop(self, reason: str = "signal") -> None:
        if self.reason is None:
            self.reason = reason

    def should_stop(self) -> bool:
        if self.reason is None and self._monotonic() >= self._deadline:
            self.reason = "time limit"
        return self.reason is not None


def configure_logging(token: str = "") -> None:
    """Log to stdout with UTC timestamps; redact the token if one is given."""
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%SZ")
    formatter.converter = time.gmtime  # timestamps in UTC
    handler.setFormatter(formatter)
    handler.addFilter(SecretRedactionFilter([token]))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)


def build_summary(config: Config, discovery: DiscoveryStats, run: RunStats, control: RunControl) -> list[str]:
    """Create the human-readable summary lines (no secrets, ever)."""
    yes_no = lambda flag: "yes" if flag else "no"  # noqa: E731
    lines = [
        "===== Execution summary =====",
        f"Profiles discovered (raw search results): {discovery.raw_results}",
        f"Unique profiles found:                    {discovery.unique}",
        f"Unique profiles considered:               {discovery.considered}",
        f"Skipped - own account:                    {discovery.skipped_self}",
        f"Skipped - previously processed:           {discovery.skipped_history}",
        f"Rejected by eligibility filters:          {run.rejected}",
        f"Followed successfully:                    {run.followed}",
        f"Already followed (no-op):                 {run.already_following}",
        f"Failed attempts:                          {run.failed}",
    ]
    if config.dry_run:
        lines.append(f"DRY RUN - would have followed:            {run.would_follow}")
    lines += [
        f"Rate limits interrupted processing:       {yes_no(run.rate_limited)}",
        f"Interrupted (signal / time limit):        {yes_no(run.interrupted)}"
        + (f" ({control.reason})" if control.reason else ""),
        f"History file:                             {config.history_file}",
    ]
    if run.fatal_error:
        lines.append(f"ERROR: {run.fatal_error}")
    for login, timestamp in run.followed_users:
        lines.append(f"  + followed {login} at {timestamp}")
    return lines


def run(config: Config, client: GitHubClient, control: RunControl) -> int:
    """Execute one full discovery + follow pass and return the exit code."""
    try:
        me = client.get_authenticated_user()
    except GitHubError as exc:
        logger.error("Could not verify the token: %s", exc)
        return EXIT_FAILURE
    logger.info("Authenticated as %s%s", me["login"], " (DRY RUN)" if config.dry_run else "")

    history = FollowHistory(config.history_file)
    logger.info("History contains %d previously followed profiles", len(history))

    now = datetime.now(timezone.utc)
    discovery = DiscoveryService(
        client,
        query=build_query(config.search_query, config.search_language, config.min_account_age_days, now),
        sort=config.search_sort,
        order=config.search_order,
        max_pages=config.max_search_pages,
    )
    criteria = EligibilityCriteria(
        account_types=config.account_types,
        excluded_users=config.excluded_users,
        min_account_age_days=config.min_account_age_days,
    )
    service = FollowService(
        client,
        history,
        criteria,
        max_follows=config.max_follows,
        follow_delay=config.follow_delay,
        max_consecutive_failures=config.max_consecutive_failures,
        dry_run=config.dry_run,
        should_stop=control.should_stop,
    )

    candidates = discovery.discover(own_login=me["login"], own_id=me.get("id"), processed=history)
    stats = service.run(candidates)

    for line in build_summary(config, discovery.stats, stats, control):
        logger.info(line)

    if stats.fatal_error:
        return EXIT_FAILURE
    if control.reason == "signal":
        return EXIT_INTERRUPTED
    return EXIT_OK


def main(env: Mapping[str, str] | None = None) -> int:
    """Program entry point. Returns a process exit code."""
    configure_logging()
    try:
        config = Config.from_env(env)
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        return EXIT_CONFIG

    configure_logging(config.token)  # re-install handler with the redaction filter
    control = RunControl(config.max_runtime_seconds)

    # Actions sends SIGINT then SIGTERM on cancel/timeout; stop gracefully.
    def _on_signal(signum: int, _frame: object) -> None:
        control.request_stop("signal")

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    client = GitHubClient(
        config.token,
        base_url=config.api_url,
        timeout=config.request_timeout,
        max_retries=config.max_retries,
        max_rate_limit_wait=config.max_rate_limit_wait,
    )
    try:
        return run(config, client, control)
    except Exception as exc:  # noqa: BLE001 - last-resort handler, reports the type
        logger.error("Unexpected error: %s: %s", type(exc).__name__, exc)
        return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
