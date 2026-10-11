"""History file: creation, appends, duplicate prevention, timestamp format."""

from datetime import datetime, timedelta, timezone

import pytest

from src.history import FollowHistory, format_timestamp
from tests.helpers import NOW


def test_timestamp_is_iso8601_utc_with_z():
    assert format_timestamp(NOW) == "2026-10-10T16:30:00Z"


def test_timestamp_converts_other_timezones_to_utc():
    plus_two = datetime(2026, 10, 10, 18, 30, tzinfo=timezone(timedelta(hours=2)))
    assert format_timestamp(plus_two) == "2026-10-10T16:30:00Z"


def test_naive_timestamp_is_rejected():
    with pytest.raises(ValueError):
        format_timestamp(datetime(2026, 10, 10, 16, 30))


def test_missing_file_is_created_with_parent_directory(tmp_path):
    path = tmp_path / "data" / "users_follows.txt"
    history = FollowHistory(path)  # file does not exist yet
    assert len(history) == 0
    assert history.record("octocat", NOW) is True
    assert path.read_text(encoding="utf-8") == "2026-10-10T16:30:00Z | octocat | https://github.com/octocat\n"


def test_empty_file_is_handled(tmp_path):
    path = tmp_path / "h.txt"
    path.write_text("", encoding="utf-8")
    history = FollowHistory(path)
    assert len(history) == 0
    assert history.record("alice", NOW)


def test_existing_records_are_preserved_and_appended_to(tmp_path):
    path = tmp_path / "h.txt"
    path.write_text("# header\n2026-01-01T00:00:00Z | alice | https://github.com/alice\n", encoding="utf-8")
    history = FollowHistory(path)
    assert "alice" in history and "ALICE" in history  # case-insensitive
    history.record("bob", NOW)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# header"
    assert lines[1].endswith("| alice | https://github.com/alice")
    assert lines[2] == "2026-10-10T16:30:00Z | bob | https://github.com/bob"


def test_duplicate_is_not_written_twice(tmp_path):
    path = tmp_path / "h.txt"
    history = FollowHistory(path)
    assert history.record("bob", NOW) is True
    assert history.record("Bob", NOW) is False  # same user, different case
    assert path.read_text(encoding="utf-8").count("bob") == 2  # username + URL of ONE line


def test_record_picks_up_lines_appended_by_another_process(tmp_path):
    path = tmp_path / "h.txt"
    history = FollowHistory(path)
    with open(path, "a", encoding="utf-8") as other:  # a concurrent writer
        other.write("2026-10-10T16:00:00Z | carol | https://github.com/carol\n")
    assert history.record("carol", NOW) is False
    assert path.read_text(encoding="utf-8").count("carol | ") == 1


def test_missing_trailing_newline_does_not_glue_records(tmp_path):
    path = tmp_path / "h.txt"
    path.write_text("2026-01-01T00:00:00Z | alice | https://github.com/alice", encoding="utf-8")  # no \n
    FollowHistory(path).record("bob", NOW)
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_malformed_lines_are_ignored_not_fatal(tmp_path):
    path = tmp_path / "h.txt"
    path.write_text("garbage line\nonly | two\n2026-01-01T00:00:00Z | dave | https://github.com/dave\n",
                    encoding="utf-8")
    history = FollowHistory(path)
    assert len(history) == 1 and "dave" in history


def test_invalid_username_is_refused(tmp_path):
    history = FollowHistory(tmp_path / "h.txt")
    with pytest.raises(ValueError):
        history.record("evil | injected\nline", NOW)
    assert not (tmp_path / "h.txt").exists()
