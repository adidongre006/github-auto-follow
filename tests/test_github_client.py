"""HTTP client: success paths, retries, backoff, rate limits, secret hygiene."""

import logging

import pytest
import requests

from src.github_client import (
    AuthenticationError,
    GitHubClient,
    GitHubError,
    MalformedResponseError,
    PermissionDeniedError,
    RateLimitError,
    TransientError,
)
from tests.helpers import TOKEN, FakeResponse, FakeSession


def make_client(responses, **kwargs):
    session = FakeSession(responses)
    sleeps = []
    client = GitHubClient(TOKEN, session=session, sleep=sleeps.append, clock=lambda: 1000.0, max_retries=3,
                          max_rate_limit_wait=60, **kwargs)
    return client, session, sleeps


def test_follow_succeeds_only_on_204():
    client, session, _ = make_client([FakeResponse(204)])
    assert client.follow("octocat") is True
    assert session.calls[0][0] == "PUT" and session.calls[0][1].endswith("/user/following/octocat")


def test_follow_with_ambiguous_2xx_raises_instead_of_claiming_success():
    client, _, _ = make_client([FakeResponse(200, {"ok": True})])
    with pytest.raises(GitHubError):
        client.follow("octocat")


def test_follow_404_message_mentions_possible_permission_problem():
    client, _, _ = make_client([FakeResponse(404, {"message": "Not Found"})])
    with pytest.raises(GitHubError) as info:
        client.follow("octocat")
    assert "permission" in str(info.value)


def test_is_following_maps_204_and_404():
    client, _, _ = make_client([FakeResponse(204), FakeResponse(404)])
    assert client.is_following("a") is True
    assert client.is_following("b") is False


def test_get_user_returns_none_for_404():
    client, _, _ = make_client([FakeResponse(404, {"message": "Not Found"})])
    assert client.get_user("ghost") is None


def test_username_is_url_encoded():
    client, session, _ = make_client([FakeResponse(204)])
    client.follow("../../evil")
    assert "/user/following/..%2F..%2Fevil" in session.calls[0][1]


def test_search_sends_expected_parameters_and_omits_sort_when_none():
    client, session, _ = make_client([FakeResponse(200, {"items": [], "total_count": 0}),
                                      FakeResponse(200, {"items": [], "total_count": 0})])
    client.search_users("type:user", sort="joined", order="desc", page=2, per_page=100)
    client.search_users("type:user", sort=None, order="desc", page=1, per_page=100)
    assert session.calls[0][2] == {"q": "type:user", "per_page": 100, "page": 2, "sort": "joined", "order": "desc"}
    assert "sort" not in session.calls[1][2]


def test_malformed_bodies_raise_a_specific_error():
    client, _, _ = make_client([FakeResponse(200, {"no_items": 1}), FakeResponse(200, bad_json=True)])
    with pytest.raises(MalformedResponseError):
        client.search_users("q", sort=None, order="desc", page=1, per_page=1)
    with pytest.raises(MalformedResponseError):
        client.get_authenticated_user()


# --- error classification -----------------------------------------------------
def test_401_is_an_authentication_error_without_retry():
    client, session, _ = make_client([FakeResponse(401, {"message": "Bad credentials"})])
    with pytest.raises(AuthenticationError):
        client.get_authenticated_user()
    assert len(session.calls) == 1


def test_plain_403_is_a_permission_error_without_retry():
    client, session, _ = make_client([FakeResponse(403, {"message": "Resource not accessible"})])
    with pytest.raises(PermissionDeniedError):
        client.follow("x")
    assert len(session.calls) == 1


# --- retries and backoff ------------------------------------------------------
def test_transient_5xx_is_retried_with_exponential_backoff():
    client, session, sleeps = make_client([FakeResponse(502), FakeResponse(503), FakeResponse(204)])
    assert client.follow("x") is True
    assert sleeps == [1.0, 2.0] and len(session.calls) == 3


def test_retries_are_bounded():
    client, session, sleeps = make_client([FakeResponse(500)] * 4)
    with pytest.raises(TransientError):
        client.follow("x")
    assert len(session.calls) == 4 and sleeps == [1.0, 2.0, 4.0]  # 1 try + 3 retries


def test_connection_errors_and_timeouts_are_retried():
    client, session, _ = make_client([requests.ConnectionError("boom"), requests.Timeout("slow"), FakeResponse(204)])
    assert client.follow("x") is True and len(session.calls) == 3


def test_persistent_network_failure_raises_transient_error():
    client, _, _ = make_client([requests.ConnectionError("down")] * 4)
    with pytest.raises(TransientError):
        client.follow("x")


# --- rate limits ----------------------------------------------------------------
def test_retry_after_header_is_respected_when_short():
    limited = FakeResponse(429, {"message": "slow down"}, {"Retry-After": "7"})
    client, _, sleeps = make_client([limited, FakeResponse(204)])
    assert client.follow("x") is True and sleeps == [7.0]


def test_primary_rate_limit_waits_until_reset_when_short():
    limited = FakeResponse(403, {"message": "API rate limit exceeded"},
                           {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1030"})
    client, _, sleeps = make_client([limited, FakeResponse(204)])
    assert client.follow("x") is True and sleeps == [31.0]  # 1030 - 1000 + 1


def test_long_rate_limit_raises_instead_of_waiting():
    limited = FakeResponse(403, {"message": "API rate limit exceeded"},
                           {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "5000"})
    client, session, sleeps = make_client([limited])
    with pytest.raises(RateLimitError) as info:
        client.follow("x")
    assert info.value.wait_seconds > 60 and sleeps == [] and len(session.calls) == 1


def test_secondary_rate_limit_without_headers_defaults_to_conservative_wait():
    limited = FakeResponse(403, {"message": "You have exceeded a secondary rate limit."})
    client = GitHubClient(TOKEN, session=FakeSession([limited] * 4), sleep=lambda _s: None, max_rate_limit_wait=30)
    with pytest.raises(RateLimitError) as info:  # default wait (60s) exceeds the 30s cap -> stop, don't wait
        client.follow("x")
    assert info.value.wait_seconds == 60.0


# --- secret hygiene -------------------------------------------------------------
def test_token_only_in_authorization_header_and_never_in_errors_or_logs():
    session = FakeSession([FakeResponse(401, {"message": "Bad credentials"})])
    client = GitHubClient(TOKEN, session=session, sleep=lambda _s: None)
    assert session.headers["Authorization"] == f"Bearer {TOKEN}"

    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logging.getLogger().addHandler(Capture())
    try:
        with pytest.raises(AuthenticationError) as info:
            client.get_authenticated_user()
    finally:
        logging.getLogger().handlers = [h for h in logging.getLogger().handlers if not isinstance(h, Capture)]
    assert TOKEN not in str(info.value) and all(TOKEN not in line for line in records)


def test_network_error_messages_do_not_leak_details():
    secret_laden = requests.ConnectionError(f"failed with header Authorization: Bearer {TOKEN}")
    client = GitHubClient(TOKEN, session=FakeSession([secret_laden] * 4), sleep=lambda _s: None)
    with pytest.raises(TransientError) as info:
        client.follow("x")
    assert TOKEN not in str(info.value)
