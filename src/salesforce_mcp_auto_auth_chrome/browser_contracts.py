"""Sanitized Salesforce browser contracts and fail-closed validators."""

from __future__ import annotations

import json
import re
from importlib.resources import files
from typing import Any, NoReturn, cast

_ID = re.compile(r"^[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?$")


def load_contracts() -> dict[str, Any]:
    """Load frozen, value-free browser request contracts."""
    path = files(__package__).joinpath("contracts/browser_requests.v1.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RuntimeError("Salesforce browser contract drift: unsupported fixture")
    return cast(dict[str, Any], payload)


def validate_salesforce_id(
    value: str, prefix: str, *, field_name: str = "record_id"
) -> str:
    """Validate a 15/18-character Salesforce ID with an expected key prefix."""
    if not isinstance(value, str) or not _ID.fullmatch(value) or not value.startswith(
        prefix
    ):
        raise ValueError(f"invalid {field_name}")
    return value


def validate_pipeline_payload(payload: object) -> list[dict[str, Any]]:
    """Validate and project browser pipeline output to its frozen allowlist."""
    contract = load_contracts()["operations"]["account_pipeline"]
    if not isinstance(payload, dict):
        _drift("pipeline response is not an object")
    required = set(contract["response_required_keys"])
    if not required.issubset(payload):
        _drift("pipeline response keys changed")
    if payload.get("done") is not True:
        _drift("pipeline response is incomplete")
    source_keys = payload.get("sourceKeys")
    expected_source_keys = set(contract["source_response_required_keys"])
    if not isinstance(source_keys, list) or not expected_source_keys.issubset(
        source_keys
    ):
        _drift("pipeline source response keys changed")
    records = payload.get("records")
    total = payload.get("totalSize")
    if not isinstance(records, list) or not isinstance(total, int):
        _drift("pipeline response types changed")
    if total != len(records):
        _drift("pipeline response count changed")

    allowed = tuple(contract["record_fields"])
    projected: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict) or not set(allowed).issubset(record):
            _drift("pipeline record shape changed")
        opportunity_id = record.get("id")
        account_id = record.get("accountId")
        if not isinstance(opportunity_id, str) or not isinstance(account_id, str):
            _drift("pipeline record IDs changed")
        try:
            validate_salesforce_id(opportunity_id, "006")
            validate_salesforce_id(account_id, "001")
        except ValueError:
            _drift("pipeline record IDs changed")
        projected.append({field: record[field] for field in allowed})
    return projected


def validate_activity_payload(
    payload: object, kind: str
) -> list[dict[str, str]]:
    """Validate and project one related-list grid payload."""
    contracts = load_contracts()["operations"]["account_activities"]["related_lists"]
    if kind not in contracts:
        raise ValueError("invalid activity kind")
    if not isinstance(payload, dict):
        _drift("activity response is not an object")
    records = payload.get("records")
    headers = payload.get("headers")
    if not isinstance(records, list) or not isinstance(headers, list):
        _drift("activity response types changed")
    if payload.get("empty") is True and not records:
        return []

    contract = contracts[kind]
    normalized_headers = {str(header).casefold() for header in headers}
    expected_headers = {
        str(header).casefold() for header in contract["required_headers"]
    }
    if not expected_headers.issubset(normalized_headers):
        _drift("activity grid headers changed")

    allowed = tuple(contract["record_fields"])
    projected: list[dict[str, str]] = []
    for record in records:
        if not isinstance(record, dict) or not set(allowed).issubset(record):
            _drift("activity record shape changed")
        values = {field: record[field] for field in allowed}
        if not all(isinstance(value, str) for value in values.values()):
            _drift("activity record types changed")
        projected.append(cast(dict[str, str], values))
    return projected


def _drift(reason: str) -> NoReturn:
    raise RuntimeError(f"Salesforce browser contract drift: {reason}")
