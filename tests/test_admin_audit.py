"""admin/audit.py のマスク判定のテスト。"""

from __future__ import annotations

import pytest

from admin import audit


@pytest.mark.parametrize(
    "key",
    [
        "api_key",
        "OPENAI_API_KEY",
        "password",
        "CHAINLIT_AUTH_SECRET",
        "HF_TOKEN",
        "access_token",
        "main_url",
        "sub_url",
        "LLM_MAIN_URL",
        "AUTH_USERS",
        "ADMIN_USERS",
    ],
)
def test_sensitive_keys_are_masked(key):
    assert audit.mask(key, "value") == "***"


@pytest.mark.parametrize(
    "key",
    ["max_tokens", "track_token_usage", "token_usage_warn_threshold", "reasoning_budget_tokens", "base_url", "model"],
)
def test_non_sensitive_token_related_keys_are_not_masked(key):
    assert audit.mask(key, "123") == "123"


def test_mask_keeps_none():
    assert audit.mask("password", None) is None
