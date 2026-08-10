from __future__ import annotations

import json
import re

import pytest

from salesforce_mcp_auto_auth_chrome.browser_contracts import (
    load_contracts,
    validate_activity_payload,
    validate_pipeline_payload,
    validate_salesforce_id,
)


def test_captured_contract_fixture_is_sanitized() -> None:
    contracts = load_contracts()
    encoded = json.dumps(contracts, sort_keys=True).lower()

    assert contracts["schema_version"] == 1
    assert ".salesforce.com" not in encoded
    assert ".force.com" not in encoded
    assert "authorization" not in encoded
    assert "request_body" not in encoded
    assert not re.search(r"\b(?:001|006|005)[a-z0-9]{12,15}\b", encoded)


@pytest.mark.parametrize(
    ("value", "prefix"),
    [
        ("001000000000000", "001"),
        ("001000000000000AAA", "001"),
        ("006000000000000", "006"),
    ],
)
def test_validate_salesforce_id_accepts_only_expected_prefix(
    value: str, prefix: str
) -> None:
    assert validate_salesforce_id(value, prefix) == value


@pytest.mark.parametrize(
    "value",
    [
        "00100000000000",
        "0010000000000000",
        "001000000000000AAA/../",
        "006000000000000",
        "not-an-id",
    ],
)
def test_validate_salesforce_id_rejects_invalid_account_ids(value: str) -> None:
    with pytest.raises(ValueError, match="account_id"):
        validate_salesforce_id(value, "001", field_name="account_id")


def test_pipeline_contract_rejects_response_shape_drift() -> None:
    with pytest.raises(RuntimeError, match="contract drift"):
        validate_pipeline_payload({"records": []})


def test_pipeline_contract_normalizes_allowed_fields_only() -> None:
    payload = {
        "done": True,
        "sourceKeys": ["count", "nextPageToken", "records"],
        "totalSize": 1,
        "records": [
            {
                "id": "006000000000000AAA",
                "accountId": "001000000000000AAA",
                "name": "Example",
                "stage": "Qualification",
                "closeDate": "2026-12-01",
                "owner": "Owner",
                "amount": 125000,
                "expectedRevenue": 50000,
                "probability": 40,
                "type": "New Business",
                "ignored": "must not escape",
            }
        ],
    }

    result = validate_pipeline_payload(payload)

    assert result == [
        {
            "id": "006000000000000AAA",
            "accountId": "001000000000000AAA",
            "name": "Example",
            "stage": "Qualification",
            "closeDate": "2026-12-01",
            "owner": "Owner",
            "amount": 125000,
            "expectedRevenue": 50000,
            "probability": 40,
            "type": "New Business",
        }
    ]


def test_activity_contract_rejects_missing_headers() -> None:
    payload = {"empty": False, "headers": ["Subject"], "records": []}

    with pytest.raises(RuntimeError, match="contract drift"):
        validate_activity_payload(payload, "open")


def test_activity_contract_allows_explicit_empty_state() -> None:
    payload = {"empty": True, "headers": [], "records": []}

    assert validate_activity_payload(payload, "history") == []
