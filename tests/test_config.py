"""Configuration parsing, validation and secret handling."""

import pytest

from src.config import MAX_FOLLOWS_CEILING, Config, ConfigError
from src.main import RunControl, build_summary
from src.discovery import DiscoveryStats
from src.follow_service import RunStats
from tests.helpers import TOKEN


def env(**overrides):
    base = {"GH_TOKEN": TOKEN}
    base.update(overrides)
    return base


def test_defaults_are_conservative():
    config = Config.from_env(env())
    assert config.max_follows == 5 and config.min_account_age_days == 30 and not config.dry_run
    assert config.account_types == frozenset({"user"})


def test_missing_token_fails_safely_with_a_helpful_message():
    with pytest.raises(ConfigError) as info:
        Config.from_env({})
    assert "GH_TOKEN" in str(info.value)


def test_blank_token_is_treated_as_missing():
    with pytest.raises(ConfigError):
        Config.from_env({"GH_TOKEN": "   "})


def test_actions_builtin_token_is_rejected():
    with pytest.raises(ConfigError) as info:
        Config.from_env({"GH_TOKEN": "ghs_builtinactionstoken"})
    assert "ghs_builtinactionstoken" not in str(info.value)


def test_token_never_appears_in_repr():
    config = Config.from_env(env())
    assert TOKEN not in repr(config) and TOKEN not in str(config)


def test_empty_strings_from_unset_actions_variables_fall_back_to_defaults():
    config = Config.from_env(env(MAX_FOLLOWS="", SEARCH_QUERY="", DRY_RUN="", EXCLUDED_USERS=""))
    assert config.max_follows == 5 and config.search_query == "type:user repos:>=1"


def test_max_follows_is_validated_and_capped():
    assert Config.from_env(env(MAX_FOLLOWS="12")).max_follows == 12
    for bad in ("0", "-1", str(MAX_FOLLOWS_CEILING + 1), "many"):
        with pytest.raises(ConfigError):
            Config.from_env(env(MAX_FOLLOWS=bad))


def test_lists_are_normalised():
    config = Config.from_env(env(EXCLUDED_USERS="@Alice, bob  CAROL", ACCOUNT_TYPES="User,Bot"))
    assert config.excluded_users == frozenset({"alice", "bob", "carol"})
    assert config.account_types == frozenset({"user", "bot"})


def test_invalid_choices_are_rejected():
    for bad in ({"SEARCH_SORT": "random"}, {"SEARCH_ORDER": "sideways"}, {"ACCOUNT_TYPES": "alien"},
                {"DRY_RUN": "maybe"}, {"GITHUB_API_URL": "http://insecure.example"}):
        with pytest.raises(ConfigError):
            Config.from_env(env(**bad))


def test_summary_contains_all_required_fields_and_no_secret():
    config = Config.from_env(env(DRY_RUN="false"))
    run = RunStats(followed=1, already_following=2, rejected=3, failed=4, rate_limited=True,
                   followed_users=[("alice", "2026-10-10T16:30:00Z")])
    discovery = DiscoveryStats(raw_results=10, unique=8, considered=6, skipped_history=2)
    text = "\n".join(build_summary(config, discovery, run, RunControl(100)))
    for expected in ("discovered", "Unique profiles considered", "Rejected", "Followed successfully",
                     "Already followed", "previously processed", "Failed attempts", "Rate limits",
                     "users_follows.txt", "alice at 2026-10-10T16:30:00Z"):
        assert expected in text
    assert TOKEN not in text
