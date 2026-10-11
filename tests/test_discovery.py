"""Discovery: query building, pagination, deduplication, exclusions, safety."""

from src.discovery import DiscoveryService, build_query
from src.github_client import GitHubError, RateLimitError
from tests.helpers import NOW, FakeGitHub, make_item, page_of
import pytest


def make_service(client, max_pages=3, per_page=2):
    return DiscoveryService(client, query="type:user", sort="joined", order="desc",
                            max_pages=max_pages, per_page=per_page)


def run_discovery(service, history=(), own_login="me", own_id=999, limit=None):
    out = []
    for candidate in service.discover(own_login=own_login, own_id=own_id, processed=set(history)):
        out.append(candidate.login)
        if limit and len(out) >= limit:
            break
    return out


def test_build_query_adds_language_and_age_cutoff():
    query = build_query("type:user repos:>=1", "python", 30, NOW)
    assert query == "type:user repos:>=1 language:python created:<2026-09-10"


def test_build_query_quotes_languages_with_spaces_and_respects_existing_qualifiers():
    assert 'language:"Jupyter Notebook"' in build_query("type:user", "Jupyter Notebook", 0, NOW)
    assert build_query("type:user created:<2020-01-01", "", 30, NOW) == "type:user created:<2020-01-01"


def test_discovers_across_pages_and_dedupes_by_id():
    pages = [
        page_of([make_item("a", 1), make_item("b", 2)], total=4),
        page_of([make_item("b-renamed", 2), make_item("c", 3)], total=4),  # id 2 repeated
    ]
    service = make_service(FakeGitHub(pages))
    assert run_discovery(service) == ["a", "b", "c"]
    assert service.stats.raw_results == 4 and service.stats.unique == 3


def test_excludes_own_account_and_history():
    pages = [page_of([make_item("me", 999), make_item("Done", 5), make_item("fresh", 6)], total=3)]
    service = make_service(FakeGitHub(pages), per_page=3)
    assert run_discovery(service, history={"Done"}) == ["fresh"]
    assert service.stats.skipped_self == 1 and service.stats.skipped_history == 1


def test_empty_results_end_discovery_cleanly():
    service = make_service(FakeGitHub([page_of([])]))
    assert run_discovery(service) == []
    assert service.stats.exhausted


def test_incomplete_results_are_flagged_but_still_used():
    service = make_service(FakeGitHub([page_of([make_item("a", 1)], total=1, incomplete=True)]))
    assert run_discovery(service) == ["a"]
    assert service.stats.incomplete_results


def test_malformed_items_are_skipped():
    pages = [page_of(["junk", {"login": "x"}, {"id": 7}, make_item("ok", 8)], total=4)]
    service = make_service(FakeGitHub(pages), per_page=4)
    assert run_discovery(service) == ["ok"]
    assert service.stats.malformed == 3


def test_is_lazy_and_stops_requesting_pages_when_consumer_stops():
    client = FakeGitHub([page_of([make_item("a", 1), make_item("b", 2)], total=6),
                         page_of([make_item("c", 3), make_item("d", 4)], total=6)])
    assert run_discovery(make_service(client), limit=1) == ["a"]
    assert client.search_calls == 1  # page 2 was never requested


def test_page_limit_is_respected():
    pages = [page_of([make_item(f"u{i}", i)] * 1 + [make_item(f"v{i}", 100 + i)], total=100) for i in range(5)]
    client = FakeGitHub(pages)
    run_discovery(make_service(client, max_pages=2))
    assert client.search_calls == 2


def test_422_from_deep_pagination_is_treated_as_end_of_results():
    client = FakeGitHub([page_of([make_item("a", 1), make_item("b", 2)], total=50),
                         GitHubError("Only the first 1000 results", 422)])
    assert run_discovery(make_service(client)) == ["a", "b"]


def test_rate_limit_error_propagates_to_the_caller():
    client = FakeGitHub([RateLimitError(120)])
    with pytest.raises(RateLimitError):
        run_discovery(make_service(client))
