"""Sanitized Salesforce browser contracts and fail-closed validators."""

from __future__ import annotations

import json
import math
import re
from importlib.resources import files
from typing import Any, NoReturn, cast

_ID = re.compile(r"^[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?$")
_OWNERSHIP_TERM = re.compile(r"^[^\x00-\x1f\x7f]+$")


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
    if (
        not isinstance(value, str)
        or not _ID.fullmatch(value)
        or not value.startswith(prefix)
    ):
        raise ValueError(f"invalid {field_name}")
    return value


def validate_ownership_term(value: str) -> str:
    """Validate one bounded literal term for fixed browser-owned SOSL."""
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not 1 <= len(value) <= 320
        or not _OWNERSHIP_TERM.fullmatch(value)
    ):
        raise ValueError("invalid ownership search term")
    return value


_OWNERSHIP_PREFIXES = {"Account": "001", "Contact": "003", "Lead": "00Q"}
_OWNERSHIP_ERRORS = {
    "search_page_unavailable": "ownership search page did not render",
}


def validate_ownership_payload(
    payload: object, *, page_record_limit: int
) -> tuple[
    list[dict[str, str]], list[str], dict[str, tuple[list[dict[str, str]], bool]]
]:
    """Validate and project one ownership search.

    Returns the summary page's records, the object types it reports as having
    more than it shows, and for each of those types the records read from its
    own result pages plus whether they reached the last page.
    """
    contract = load_contracts()["operations"]["ownership_search"]
    if not isinstance(payload, dict):
        _drift("ownership response is not an object")
    if "error" in payload:
        _drift(_OWNERSHIP_ERRORS.get(str(payload["error"]), "ownership search failed"))
    required = set(contract["response_required_keys"])
    if not required.issubset(payload):
        _drift("ownership response keys changed")
    summary = payload.get("summary")
    pages = payload.get("pages")
    if (
        payload.get("sourceType") != "classic_search_page"
        or not isinstance(summary, dict)
        or set(summary) != {"records", "incomplete"}
        or not isinstance(pages, dict)
    ):
        _drift("ownership response types changed")
    incomplete = summary["incomplete"]
    if (
        not isinstance(incomplete, list)
        or len(set(incomplete)) != len(incomplete)
        or any(value not in _OWNERSHIP_PREFIXES for value in incomplete)
        or set(pages) != set(incomplete)
    ):
        _drift("ownership pagination signal changed")
    allowed = tuple(contract["record_fields"])
    projected_pages: dict[str, tuple[list[dict[str, str]], bool]] = {}
    for record_type, page in pages.items():
        if (
            not isinstance(page, dict)
            or set(page) != {"records", "complete"}
            or not isinstance(page["complete"], bool)
        ):
            _drift("ownership page shape changed")
        projected_pages[record_type] = (
            _ownership_records(page["records"], allowed, page_record_limit),
            page["complete"],
        )
    return (
        _ownership_records(summary["records"], allowed, 60),
        cast(list[str], incomplete),
        projected_pages,
    )


def _ownership_records(
    records: object, allowed: tuple[str, ...], limit: int
) -> list[dict[str, str]]:
    if not isinstance(records, list) or len(records) > limit:
        _drift("ownership response types changed")
    projected: list[dict[str, str]] = []
    for record in records:
        if not isinstance(record, dict) or set(record) != set(allowed):
            _drift("ownership record shape changed")
        values = {field: record[field] for field in allowed}
        if not all(isinstance(value, str) for value in values.values()):
            _drift("ownership record types changed")
        prefix = _OWNERSHIP_PREFIXES.get(values["type"])
        try:
            if prefix is None:
                raise ValueError("invalid ownership record type")
            validate_salesforce_id(values["id"], prefix)
            if values["accountId"]:
                validate_salesforce_id(values["accountId"], "001")
        except ValueError:
            _drift("ownership record IDs changed")
        if (
            len(values["name"]) > 240
            or len(values["owner"]) > 120
            or len(values["email"]) > 320
            or len(values["website"]) > 2048
            or len(values["company"]) > 240
        ):
            _drift("ownership record values exceed bounds")
        projected.append(cast(dict[str, str], values))
    return projected


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
        string_bounds = {
            "name": 240,
            "stage": 120,
            "closeDate": 40,
            "owner": 120,
            "sizeType": 100,
            "leaseType": 120,
            "type": 120,
        }
        if any(
            not isinstance(record.get(field), str)
            or len(record[field]) > maximum
            for field, maximum in string_bounds.items()
        ):
            _drift("pipeline record strings changed")
        for field in ("amount", "expectedRevenue", "probability", "size", "term"):
            value = record.get(field)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                _drift("pipeline record numbers changed")
        probability = record.get("probability")
        if probability is not None and not 0 <= probability <= 100:
            _drift("pipeline probability changed")
        projected.append({field: record[field] for field in allowed})
    return projected


def validate_activity_payload(payload: object, kind: str) -> list[dict[str, str]]:
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
