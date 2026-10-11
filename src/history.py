"""Persistent follow history stored in a plain text file.

Format - one line per confirmed follow, fields separated by `` | ``::

    2026-10-10T16:30:00Z | octocat | https://github.com/octocat

Design notes
------------
* Append-only: existing lines are never rewritten.
* A line is written (and fsync'd) immediately after each confirmed follow, so
  an interrupted run loses nothing.
* Before every append the file is re-read, so a second process that appended
  in the meantime cannot cause a duplicate record.
* Blank lines and ``#`` comment lines are ignored; malformed lines are logged
  and skipped instead of crashing the run.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

SEPARATOR = " | "
# Characters GitHub logins can contain (plus "_" for enterprise-managed users).
# Validating here also guarantees a username can never inject extra fields or
# lines into the history file.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


def format_timestamp(moment: datetime) -> str:
    """Format a timezone-aware datetime as ISO 8601 UTC, e.g. ``2026-10-10T16:30:00Z``."""
    if moment.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FollowHistory:
    """Reads and appends to ``users_follows.txt``."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._usernames: set[str] = set()
        self._reload()

    @property
    def path(self) -> Path:
        return self._path

    def __contains__(self, username: object) -> bool:
        """Case-insensitive membership test (GitHub usernames are case-insensitive)."""
        return isinstance(username, str) and username.lower() in self._usernames

    def __len__(self) -> int:
        return len(self._usernames)

    def record(self, username: str, moment: datetime) -> bool:
        """Append a confirmed follow. Returns False if it was already recorded.

        Call this ONLY after GitHub confirmed the follow.
        """
        if not _USERNAME_RE.match(username):
            raise ValueError("refusing to record an invalid username")

        self._reload()  # pick up anything another process appended meanwhile
        if username.lower() in self._usernames:
            logger.info("History already contains %s; not writing a duplicate", username)
            return False

        line = (
            f"{format_timestamp(moment)}{SEPARATOR}{username}{SEPARATOR}"
            f"https://github.com/{username}\n"
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)

        # If the file exists but does not end with a newline (hand-edited?),
        # start on a fresh line so we never glue two records together.
        prefix = ""
        if self._path.exists() and self._path.stat().st_size > 0:
            with open(self._path, "rb") as existing:
                existing.seek(-1, os.SEEK_END)
                if existing.read(1) != b"\n":
                    prefix = "\n"

        # One small write in append mode, flushed to disk right away.
        with open(self._path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(prefix + line)
            handle.flush()
            os.fsync(handle.fileno())

        self._usernames.add(username.lower())
        return True

    def _reload(self) -> None:
        """(Re)read the file. A missing or empty file simply means no history."""
        self._usernames = set()
        if not self._path.exists():
            return
        text = self._path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = [part.strip() for part in stripped.split("|")]
            if len(parts) != 3 or not _USERNAME_RE.match(parts[1]):
                logger.warning("Ignoring malformed history line %d in %s", number, self._path)
                continue
            self._usernames.add(parts[1].lower())
