"""Follow service: success, no-ops, failures, limits, rate limits, interruption."""

import logging

from src.discovery import Candidate
from src.eligibility import EligibilityCriteria
from src.follow_service import FollowService
from src.github_client import AuthenticationError, GitHubError, PermissionDeniedError, RateLimitError
from src.history import FollowHistory
from src.main import RunControl, SecretRedactionFilter
from tests.helpers import NOW, TOKEN, FakeGitHub, make_profile

CRITERIA = EligibilityCriteria(frozenset({"user"}), frozenset({"blocked"}), 0)


def cands(*names):
    return [Candidate(name, i + 1, "User") for i, name in enumerate(names)]


def make_service(tmp_path, client, **overrides):
    history = FollowHistory(tmp_path / "h.txt")
    kwargs = dict(max_follows=5, follow_delay=0, clock=lambda: NOW, sleep=lambda _s: None)
    kwargs.update(overrides)
    return FollowService(client, history, CRITERIA, **kwargs), history


def test_successful_follow_is_recorded_with_utc_timestamp(tmp_path):
    client = FakeGitHub()
    service, history = make_service(tmp_path, client)
    stats = service.run(cands("alice"))
    assert client.follow_calls == ["alice"]
    assert stats.followed == 1 and stats.followed_users == [("alice", "2026-10-10T16:30:00Z")]
    assert "alice" in history
    assert (tmp_path / "h.txt").read_text().strip() == "2026-10-10T16:30:00Z | alice | https://github.com/alice"


def test_already_followed_is_a_noop_not_an_error(tmp_path):
    client = FakeGitHub()
    client.following.add("alice")
    service, history = make_service(tmp_path, client)
    stats = service.run(cands("alice"))
    assert stats.already_following == 1 and stats.failed == 0 and stats.followed == 0
    assert client.follow_calls == [] and "alice" not in history


def test_failed_follow_is_counted_and_never_recorded(tmp_path):
    client = FakeGitHub()
    client.follow_errors["alice"] = GitHubError("Unexpected response 200", 200)  # ambiguous
    service, history = make_service(tmp_path, client)
    stats = service.run(cands("alice", "bob"))
    assert stats.failed == 1 and stats.followed == 1
    assert "alice" not in history and "bob" in history


def test_unavailable_and_ineligible_profiles_are_rejected(tmp_path):
    client = FakeGitHub()
    client.profiles["ghost"] = None  # 404: deleted or suspended
    service, _ = make_service(tmp_path, client)
    stats = service.run(cands("ghost", "blocked", "alice"))
    assert stats.rejected == 2 and stats.followed == 1
    assert client.follow_calls == ["alice"]


def test_per_run_attempt_limit_is_enforced_and_stops_pulling_candidates(tmp_path):
    client = FakeGitHub()
    consumed = []

    def source():
        for candidate in cands("a", "b", "c", "d"):
            consumed.append(candidate.login)
            yield candidate

    service, _ = make_service(tmp_path, client, max_follows=2)
    stats = service.run(source())
    assert client.follow_calls == ["a", "b"] and stats.limit_reached
    assert consumed == ["a", "b"]  # "c" was never requested from discovery


def test_failed_attempts_also_count_towards_the_limit(tmp_path):
    client = FakeGitHub()
    client.follow_errors["a"] = GitHubError("boom", 422)
    service, _ = make_service(tmp_path, client, max_follows=1)
    service.run(cands("a", "b"))
    assert client.follow_calls == ["a"]


def test_rate_limit_stops_safely_and_keeps_earlier_follows(tmp_path):
    client = FakeGitHub()
    client.follow_errors["b"] = RateLimitError(900)
    service, history = make_service(tmp_path, client)
    stats = service.run(cands("a", "b", "c"))
    assert stats.rate_limited and stats.fatal_error is None
    assert "a" in history and "b" not in history and client.follow_calls == ["a", "b"]


def test_rate_limit_raised_by_discovery_is_handled(tmp_path):
    def broken():
        yield Candidate("a", 1, "User")
        raise RateLimitError(500)

    service, history = make_service(tmp_path, FakeGitHub())
    stats = service.run(broken())
    assert stats.rate_limited and "a" in history


def test_auth_and_permission_errors_are_fatal(tmp_path):
    for error in (AuthenticationError("bad token", 401), PermissionDeniedError("no scope", 403)):
        client = FakeGitHub()
        client.follow_errors["a"] = error
        service, _ = make_service(tmp_path, client)
        stats = service.run(cands("a", "b"))
        assert stats.fatal_error and client.follow_calls == ["a"]


def test_too_many_consecutive_failures_aborts_the_run(tmp_path):
    client = FakeGitHub()
    for name in "abcdef":
        client.follow_errors[name] = GitHubError("Follow returned 404", 404)
    service, _ = make_service(tmp_path, client, max_follows=50, max_consecutive_failures=3)
    stats = service.run(cands(*"abcdef"))
    assert client.follow_calls == ["a", "b", "c"] and "consecutive failures" in stats.fatal_error


def test_interrupted_run_stops_without_losing_recorded_follows(tmp_path):
    client = FakeGitHub()
    stop = {"flag": False}
    original_follow = client.follow

    def follow_then_get_interrupted(login):
        result = original_follow(login)
        stop["flag"] = True  # e.g. SIGTERM arrives right after the first follow
        return result

    client.follow = follow_then_get_interrupted
    service, history = make_service(tmp_path, client, should_stop=lambda: stop["flag"])
    stats = service.run(cands("a", "b", "c"))
    assert stats.interrupted and client.follow_calls == ["a"]
    assert "a" in history  # already safely on disk


def test_dry_run_never_follows_or_writes_history(tmp_path):
    client = FakeGitHub()
    service, history = make_service(tmp_path, client, dry_run=True)
    stats = service.run(cands("a", "b"))
    assert stats.would_follow == 2 and client.follow_calls == [] and len(history) == 0
    assert not (tmp_path / "h.txt").exists()


def test_history_write_failure_is_fatal(tmp_path):
    client = FakeGitHub()
    service, history = make_service(tmp_path, client)

    def explode(*_args, **_kwargs):
        raise PermissionError("read-only file system")

    history.record = explode
    stats = service.run(cands("a", "b"))
    assert "history" in stats.fatal_error.lower() and client.follow_calls == ["a"]


def test_pause_between_follows_uses_injected_sleep(tmp_path):
    sleeps = []
    service, _ = make_service(tmp_path, FakeGitHub(), follow_delay=2.0, sleep=sleeps.append, max_follows=2)
    service.run(cands("a", "b"))
    assert sleeps == [2.0]  # no pointless sleep after the final allowed follow


# --- RunControl (interruption) ------------------------------------------------
def test_run_control_time_limit():
    clock = {"t": 0.0}
    control = RunControl(10, monotonic=lambda: clock["t"])
    assert not control.should_stop()
    clock["t"] = 11.0
    assert control.should_stop() and control.reason == "time limit"


def test_run_control_signal_reason_is_kept():
    control = RunControl(1000)
    control.request_stop("signal")
    control.request_stop("time limit")  # first reason wins
    assert control.should_stop() and control.reason == "signal"


# --- secret handling ------------------------------------------------------------
def test_redaction_filter_masks_the_token_in_log_records():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "token is %s", (TOKEN,), None)
    SecretRedactionFilter([TOKEN]).filter(record)
    assert TOKEN not in record.getMessage() and "***" in record.getMessage()
