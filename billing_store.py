"""
billing_store.py
----------------
Lightweight JSON-backed billing state and entitlement helpers.

This module intentionally mirrors the existing project-store persistence style
to keep deployment simple in local and App Runner environments.

Storage strategy:
- AWS mode with BILLING_DYNAMODB_TABLE configured: DynamoDB-backed durable ledger
- Otherwise: local JSON fallback at BILLING_STORE_PATH
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import boto3

from multicam_pipeline.config import AWS_REGION, IS_AWS


BILLING_STORE_PATH = Path(os.environ.get("BILLING_STORE_PATH", "/tmp/multicam/billing.json"))
BILLING_DYNAMODB_TABLE = os.environ.get("BILLING_DYNAMODB_TABLE", "").strip()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_store_file() -> None:
    BILLING_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not BILLING_STORE_PATH.exists():
        BILLING_STORE_PATH.write_text(
            "{\"users\": {}, \"stripe_customers\": {}, \"events\": []}",
            encoding="utf-8",
        )


def _use_dynamodb() -> bool:
    return bool(IS_AWS and BILLING_DYNAMODB_TABLE)


def _ddb_table():
    dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
    return dynamodb.Table(BILLING_DYNAMODB_TABLE)


def _default_user_billing(user_id: str) -> Dict[str, Any]:
    return {
        "user_id": user_id,
        "plan": "free",
        "entitlements": {"paid_tier": False},
        "stripe_customer_id": None,
        "stripe_subscription_id": None,
        "updated_at": _now(),
    }


def _from_ddb_user_item(item: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    return {
        "user_id": user_id,
        "plan": item.get("plan", "free"),
        "entitlements": item.get("entitlements") or {"paid_tier": False},
        "stripe_customer_id": item.get("stripe_customer_id"),
        "stripe_subscription_id": item.get("stripe_subscription_id"),
        "updated_at": item.get("updated_at", _now()),
    }


def _put_ddb_user_record(record: Dict[str, Any]) -> None:
    table = _ddb_table()
    table.put_item(
        Item={
            "pk": f"USER#{record['user_id']}",
            "sk": "PROFILE",
            "item_type": "user_profile",
            "plan": record.get("plan", "free"),
            "entitlements": record.get("entitlements") or {"paid_tier": False},
            "stripe_customer_id": record.get("stripe_customer_id"),
            "stripe_subscription_id": record.get("stripe_subscription_id"),
            "updated_at": record.get("updated_at", _now()),
        }
    )


def _load_store() -> Dict[str, Any]:
    _ensure_store_file()
    with BILLING_STORE_PATH.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    payload.setdefault("users", {})
    payload.setdefault("stripe_customers", {})
    payload.setdefault("events", [])
    return payload


def _save_store(store: Dict[str, Any]) -> None:
    _ensure_store_file()
    with BILLING_STORE_PATH.open("w", encoding="utf-8") as handle:
        json.dump(store, handle, indent=2)


def get_user_billing(user_id: str) -> Dict[str, Any]:
    """Return billing data for a user, creating a default record if missing."""
    if _use_dynamodb():
        table = _ddb_table()
        response = table.get_item(Key={"pk": f"USER#{user_id}", "sk": "PROFILE"})
        item = response.get("Item")
        if item:
            return _from_ddb_user_item(item, user_id)

        record = _default_user_billing(user_id)
        _put_ddb_user_record(record)
        return record

    store = _load_store()
    users = store["users"]
    if user_id not in users:
        users[user_id] = _default_user_billing(user_id)
        _save_store(store)
    return users[user_id]


def set_user_billing(
    user_id: str,
    *,
    plan: Optional[str] = None,
    paid_tier: Optional[bool] = None,
    stripe_customer_id: Optional[str] = None,
    stripe_subscription_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Patch user billing state and persist."""
    if _use_dynamodb():
        current = get_user_billing(user_id)

        if plan is not None:
            current["plan"] = plan
        if paid_tier is not None:
            entitlements = current.setdefault("entitlements", {})
            entitlements["paid_tier"] = bool(paid_tier)
        if stripe_customer_id is not None:
            current["stripe_customer_id"] = stripe_customer_id
        if stripe_subscription_id is not None:
            current["stripe_subscription_id"] = stripe_subscription_id

        current["updated_at"] = _now()
        _put_ddb_user_record(current)

        if stripe_customer_id is not None:
            table = _ddb_table()
            table.put_item(
                Item={
                    "pk": f"CUSTOMER#{stripe_customer_id}",
                    "sk": "MAP",
                    "item_type": "customer_map",
                    "user_id": user_id,
                    "updated_at": _now(),
                }
            )

        return current

    store = _load_store()
    users = store["users"]
    current = users.get(user_id) or _default_user_billing(user_id)

    if plan is not None:
        current["plan"] = plan
    if paid_tier is not None:
        entitlements = current.setdefault("entitlements", {})
        entitlements["paid_tier"] = bool(paid_tier)
    if stripe_customer_id is not None:
        current["stripe_customer_id"] = stripe_customer_id
        store["stripe_customers"][stripe_customer_id] = user_id
    if stripe_subscription_id is not None:
        current["stripe_subscription_id"] = stripe_subscription_id

    current["updated_at"] = _now()
    users[user_id] = current
    _save_store(store)
    return current


def find_user_by_customer_id(customer_id: str) -> Optional[str]:
    """Resolve internal user_id from a Stripe customer ID if known."""
    if _use_dynamodb():
        table = _ddb_table()
        response = table.get_item(Key={"pk": f"CUSTOMER#{customer_id}", "sk": "MAP"})
        item = response.get("Item")
        if item and item.get("user_id"):
            return str(item["user_id"])

        # Backward compatibility path: scan profile items for direct customer linkage.
        response = table.scan(
            FilterExpression="item_type = :t AND stripe_customer_id = :cid",
            ExpressionAttributeValues={":t": "user_profile", ":cid": customer_id},
            Limit=1,
        )
        items = response.get("Items", [])
        if items:
            pk = str(items[0].get("pk", ""))
            if pk.startswith("USER#"):
                return pk.split("#", 1)[1]
        return None

    store = _load_store()
    user_id = store.get("stripe_customers", {}).get(customer_id)
    if user_id:
        return user_id

    for candidate_user_id, data in (store.get("users") or {}).items():
        if data.get("stripe_customer_id") == customer_id:
            return candidate_user_id
    return None


def append_billing_event(event_type: str, payload: Dict[str, Any]) -> None:
    """Store compact billing events for debugging/audit in low-volume deployments."""
    if _use_dynamodb():
        table = _ddb_table()
        created_at = _now()
        table.put_item(
            Item={
                "pk": "EVENTS",
                "sk": f"{created_at}#{uuid.uuid4().hex}",
                "item_type": "event",
                "event_type": event_type,
                "payload": payload,
                "created_at": created_at,
            }
        )
        return

    store = _load_store()
    events = store.setdefault("events", [])
    events.append({"event_type": event_type, "payload": payload, "created_at": _now()})
    # Keep file bounded.
    if len(events) > 1000:
        del events[:-1000]
    _save_store(store)


def has_paid_tier_access(user_id: Optional[str]) -> bool:
    """Return whether the user has paid-tier entitlements."""
    if not user_id:
        return False
    billing = get_user_billing(user_id)
    return bool((billing.get("entitlements") or {}).get("paid_tier", False))


def billing_backend_health() -> Dict[str, Any]:
    """
    Check billing backend availability and return backend-specific diagnostics.

    Returns:
        Dict with fields:
            ok: bool
            backend: "dynamodb" | "json"
            details: diagnostic key/value payload
    """
    if _use_dynamodb():
        try:
            table = _ddb_table()
            description = table.meta.client.describe_table(TableName=BILLING_DYNAMODB_TABLE)["Table"]
            key_schema = description.get("KeySchema", [])
            attr_defs = description.get("AttributeDefinitions", [])

            key_map = {entry.get("KeyType"): entry.get("AttributeName") for entry in key_schema}
            expected_pk = key_map.get("HASH") == "pk"
            expected_sk = key_map.get("RANGE") == "sk"

            return {
                "ok": bool(expected_pk and expected_sk),
                "backend": "dynamodb",
                "details": {
                    "table_name": BILLING_DYNAMODB_TABLE,
                    "table_status": description.get("TableStatus"),
                    "key_schema": key_schema,
                    "attribute_definitions": attr_defs,
                    "expected_key_schema": {"HASH": "pk", "RANGE": "sk"},
                    "key_schema_valid": bool(expected_pk and expected_sk),
                },
            }
        except Exception as exc:
            return {
                "ok": False,
                "backend": "dynamodb",
                "details": {
                    "table_name": BILLING_DYNAMODB_TABLE,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            }

    try:
        _ensure_store_file()
        # Validate JSON readability/writability by a load+save cycle.
        store = _load_store()
        _save_store(store)
        return {
            "ok": True,
            "backend": "json",
            "details": {
                "path": str(BILLING_STORE_PATH),
                "exists": BILLING_STORE_PATH.exists(),
            },
        }
    except Exception as exc:
        return {
            "ok": False,
            "backend": "json",
            "details": {
                "path": str(BILLING_STORE_PATH),
                "error": f"{type(exc).__name__}: {exc}",
            },
        }
