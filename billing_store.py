"""
billing_store.py
----------------
Lightweight JSON-backed billing state and entitlement helpers.

This module intentionally mirrors the existing project-store persistence style
to keep deployment simple in local and ECS environments.

Storage strategy:
- AWS mode with BILLING_DYNAMODB_TABLE configured: DynamoDB-backed durable ledger
- Otherwise: local JSON fallback at BILLING_STORE_PATH
"""

from __future__ import annotations

import json
import hashlib
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import boto3
from boto3.dynamodb.types import TypeSerializer

from multicam_pipeline.config import AWS_REGION, IS_AWS


BILLING_STORE_PATH = Path(os.environ.get("BILLING_STORE_PATH", "/tmp/multicam/billing.json"))
BILLING_DYNAMODB_TABLE = os.environ.get("BILLING_DYNAMODB_TABLE", "").strip()
PROMO_CODES_TABLE = os.environ.get("PROMO_CODES_TABLE", "multicam-promo-codes").strip()
PROMO_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


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
        "promo_paid_until": None,
        "updated_at": _now(),
    }


def _from_ddb_user_item(item: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    return {
        "user_id": user_id,
        "plan": item.get("plan", "free"),
        "entitlements": item.get("entitlements") or {"paid_tier": False},
        "stripe_customer_id": item.get("stripe_customer_id"),
        "stripe_subscription_id": item.get("stripe_subscription_id"),
        "promo_paid_until": item.get("promo_paid_until"),
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
            "promo_paid_until": record.get("promo_paid_until"),
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
            record = _from_ddb_user_item(item, user_id)
            record["promo_paid_until"] = _get_durable_promo_expiry(user_id)
            return record

        record = _default_user_billing(user_id)
        record["promo_paid_until"] = _get_durable_promo_expiry(user_id)
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
    promo_paid_until: Optional[str] = None,
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
        if promo_paid_until is not None:
            current["promo_paid_until"] = promo_paid_until

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
    if promo_paid_until is not None:
        current["promo_paid_until"] = promo_paid_until

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
    return _has_effective_paid_access(billing)


def _has_effective_paid_access(billing: Dict[str, Any]) -> bool:
    if bool((billing.get("entitlements") or {}).get("paid_tier", False)):
        return True
    promo_paid_until = billing.get("promo_paid_until")
    if not promo_paid_until:
        return False
    try:
        expiry = datetime.fromisoformat(str(promo_paid_until).replace("Z", "+00:00"))
        return expiry > datetime.now(timezone.utc)
    except ValueError:
        return False


def _promo_hash(code: str) -> str:
    normalized = "".join(character for character in code.upper() if character.isalnum())
    return hashlib.sha256(normalized.encode("ascii")).hexdigest()


def _get_durable_promo_expiry(user_id: str) -> Optional[str]:
    if not PROMO_CODES_TABLE:
        return None
    table = boto3.resource("dynamodb", region_name=AWS_REGION).Table(PROMO_CODES_TABLE)
    item = table.get_item(Key={"promo_hash": f"USER#{user_id}"}, ConsistentRead=True).get("Item")
    return str(item["paid_until"]) if item and item.get("paid_until") else None


def _new_promo_code() -> str:
    value = "".join(secrets.choice(PROMO_CODE_ALPHABET) for _ in range(16))
    return "RVPRO-" + "-".join(value[index:index + 4] for index in range(0, len(value), 4))


def issue_pro_month_codes(count: int, creator_id: str) -> list[str]:
    """Create single-use Pro Basic vouchers; plaintext is returned only once."""
    if not 1 <= count <= 25:
        raise ValueError("count must be between 1 and 25")
    now = datetime.now(timezone.utc)
    redemption_deadline = int((now + timedelta(days=90)).timestamp())
    codes: list[str] = []
    if _use_dynamodb():
        table = boto3.resource("dynamodb", region_name=AWS_REGION).Table(PROMO_CODES_TABLE)
        with table.batch_writer() as batch:
            for _ in range(count):
                code = _new_promo_code()
                batch.put_item(Item={
                    "promo_hash": _promo_hash(code),
                    "status": "available",
                    "plan": "pro_basic",
                    "duration_days": 30,
                    "created_by": creator_id,
                    "created_at": now.isoformat(),
                    "redeem_before": redemption_deadline,
                })
                codes.append(code)
        return codes

    store = _load_store()
    promos = store.setdefault("promo_codes", {})
    for _ in range(count):
        code = _new_promo_code()
        promos[_promo_hash(code)] = {
            "status": "available",
            "plan": "pro_basic",
            "duration_days": 30,
            "created_by": creator_id,
            "created_at": now.isoformat(),
            "redeem_before": redemption_deadline,
        }
        codes.append(code)
    _save_store(store)
    return codes


def redeem_pro_month_code(code: str, user_id: str) -> Dict[str, Any]:
    """Redeem one voucher and extend the shared Pro entitlement by 30 days."""
    normalized_hash = _promo_hash(code)
    now = datetime.now(timezone.utc)
    if _use_dynamodb():
        table = boto3.resource("dynamodb", region_name=AWS_REGION).Table(PROMO_CODES_TABLE)
        response = table.get_item(Key={"promo_hash": normalized_hash}, ConsistentRead=True)
        promo = response.get("Item")
        if not promo:
            raise ValueError("That promo code is invalid.")
        if promo.get("status") != "available":
            raise ValueError("That promo code has already been redeemed.")
        if int(promo.get("redeem_before", 0)) < int(now.timestamp()):
            raise ValueError("That promo code has expired.")
        current = get_user_billing(user_id)
        if bool((current.get("entitlements") or {}).get("paid_tier", False)):
            raise ValueError("Your account already has an active paid Pro subscription.")
        previous_expiry = current.get("promo_paid_until")
        start = now
        if previous_expiry:
            try:
                parsed_expiry = datetime.fromisoformat(str(previous_expiry).replace("Z", "+00:00"))
                if parsed_expiry > start:
                    start = parsed_expiry
            except ValueError:
                pass
        expiry = (start + timedelta(days=30)).isoformat()
        serializer = TypeSerializer()
        promo_table = PROMO_CODES_TABLE
        user_entitlement = {
            "promo_hash": f"USER#{user_id}",
            "item_type": "user_promo_entitlement",
            "user_id": user_id,
            "paid_until": expiry,
            "updated_at": now.isoformat(),
        }
        put_request = {
            "TableName": promo_table,
            "Item": {key: serializer.serialize(value) for key, value in user_entitlement.items()},
        }
        existing_entitlement = table.get_item(Key={"promo_hash": f"USER#{user_id}"}, ConsistentRead=True).get("Item")
        if existing_entitlement and existing_entitlement.get("paid_until"):
            put_request["ConditionExpression"] = "paid_until = :previous_expiry"
            put_request["ExpressionAttributeValues"] = {
                ":previous_expiry": serializer.serialize(existing_entitlement["paid_until"]),
            }
        else:
            put_request["ConditionExpression"] = "attribute_not_exists(promo_hash)"
        try:
            table.meta.client.transact_write_items(TransactItems=[
                {"Update": {
                    "TableName": promo_table,
                    "Key": {"promo_hash": {"S": normalized_hash}},
                    "UpdateExpression": "SET #status = :redeemed, redeemed_by = :user, redeemed_at = :now, entitlement_expires_at = :expiry",
                    "ConditionExpression": "#status = :available AND redeem_before >= :epoch",
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": {
                        ":redeemed": {"S": "redeemed"}, ":available": {"S": "available"},
                        ":user": {"S": user_id}, ":now": {"S": now.isoformat()},
                        ":expiry": {"S": expiry}, ":epoch": {"N": str(int(now.timestamp()))},
                    },
                }},
                {"Put": put_request},
            ])
        except Exception as exc:
            if type(exc).__name__ in {"ConditionalCheckFailedException", "TransactionCanceledException"}:
                raise ValueError("That promo code has already been redeemed or has expired.") from exc
            raise
        return {"plan": "pro_basic", "promo_paid_until": expiry}

    store = _load_store()
    promo = (store.get("promo_codes") or {}).get(normalized_hash)
    if not promo:
        raise ValueError("That promo code is invalid.")
    if promo.get("status") != "available":
        raise ValueError("That promo code has already been redeemed.")
    if int(promo.get("redeem_before", 0)) < int(now.timestamp()):
        raise ValueError("That promo code has expired.")
    current = get_user_billing(user_id)
    if bool((current.get("entitlements") or {}).get("paid_tier", False)):
        raise ValueError("Your account already has an active paid Pro subscription.")
    previous_expiry = current.get("promo_paid_until")
    start = now
    if previous_expiry:
        try:
            parsed_expiry = datetime.fromisoformat(str(previous_expiry).replace("Z", "+00:00"))
            if parsed_expiry > start:
                start = parsed_expiry
        except ValueError:
            pass
    expiry = (start + timedelta(days=30)).isoformat()
    promo.update({"status": "redeemed", "redeemed_by": user_id, "redeemed_at": now.isoformat(), "entitlement_expires_at": expiry})
    store["promo_codes"][normalized_hash] = promo
    current["promo_paid_until"] = expiry
    current["updated_at"] = now.isoformat()
    store["users"][user_id] = current
    _save_store(store)
    return {"plan": "pro_basic", "promo_paid_until": expiry}


def get_user_monthly_render_count(user_id: str, month_key: Optional[str] = None) -> int:
    """Return the number of renders submitted by the user in the given UTC calendar month (YYYY-MM)."""
    if not user_id:
        return 0
    if not month_key:
        month_key = datetime.now(timezone.utc).strftime("%Y-%m")

    if _use_dynamodb():
        table = _ddb_table()
        response = table.get_item(Key={"pk": f"USER#{user_id}", "sk": f"USAGE#{month_key}"})
        item = response.get("Item")
        return int(item.get("render_count", 0)) if item else 0

    store = _load_store()
    usage = store.setdefault("monthly_renders", {})
    user_usage = usage.setdefault(user_id, {})
    return int(user_usage.get(month_key, 0))


def increment_user_monthly_render_count(user_id: str, month_key: Optional[str] = None) -> int:
    """Increment and return the updated render count for the current month."""
    if not user_id:
        return 0
    if not month_key:
        month_key = datetime.now(timezone.utc).strftime("%Y-%m")

    if _use_dynamodb():
        table = _ddb_table()
        response = table.update_item(
            Key={"pk": f"USER#{user_id}", "sk": f"USAGE#{month_key}"},
            UpdateExpression="SET render_count = if_not_exists(render_count, :zero) + :inc, updated_at = :now, item_type = :type",
            ExpressionAttributeValues={
                ":zero": 0,
                ":inc": 1,
                ":now": _now(),
                ":type": "monthly_usage",
            },
            ReturnValues="UPDATED_NEW",
        )
        return int(response.get("Attributes", {}).get("render_count", 1))

    store = _load_store()
    usage = store.setdefault("monthly_renders", {})
    user_usage = usage.setdefault(user_id, {})
    current_count = int(user_usage.get(month_key, 0)) + 1
    user_usage[month_key] = current_count
    _save_store(store)
    return current_count


def get_user_billing_quota(user_id: str) -> Dict[str, Any]:
    """
    Return comprehensive quota and plan entitlements for the user.
    Free tier: 2 renders/month limit.
    Paid tier (Pro): unlimited non-Rekognition renders.
    """
    if not user_id:
        return {
            "user_id": "",
            "plan": "free",
            "is_paid_tier": False,
            "month_key": datetime.now(timezone.utc).strftime("%Y-%m"),
            "monthly_limit": 2,
            "renders_used": 0,
            "renders_remaining": 2,
            "can_render": True,
        }

    billing = get_user_billing(user_id)
    is_paid = _has_effective_paid_access(billing)
    month_key = datetime.now(timezone.utc).strftime("%Y-%m")
    used = get_user_monthly_render_count(user_id, month_key)
    limit = None if is_paid else 2
    remaining = None if is_paid else max(0, 2 - used)
    can_render = is_paid or (used < 2)

    return {
        "user_id": user_id,
        "plan": "pro" if is_paid else billing.get("plan", "free"),
        "is_paid_tier": is_paid,
        "month_key": month_key,
        "monthly_limit": limit,
        "renders_used": used,
        "renders_remaining": remaining,
        "can_render": can_render,
        "promo_paid_until": billing.get("promo_paid_until"),
    }


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
