"""
routers/billing.py
------------------
Stripe billing endpoints for paid-tier enablement.
"""

from __future__ import annotations

import json
import os
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from multicam_pipeline.auth import require_auth, resolve_actor_id
from multicam_pipeline.billing_store import (
    append_billing_event,
    find_user_by_customer_id,
    get_user_billing,
    set_user_billing,
)

try:
    import stripe
except Exception:  # pragma: no cover - import guarded for optional runtime
    stripe = None


router = APIRouter(prefix="/billing", tags=["billing"])

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
STRIPE_DEFAULT_SUCCESS_URL = os.environ.get("STRIPE_DEFAULT_SUCCESS_URL", "https://razorvid.com/billing/success")
STRIPE_DEFAULT_CANCEL_URL = os.environ.get("STRIPE_DEFAULT_CANCEL_URL", "https://razorvid.com/billing/cancel")
STRIPE_PRICE_IDS_JSON = os.environ.get("STRIPE_PRICE_IDS_JSON", "{}").strip()


def _resolve_price_map() -> Dict[str, str]:
    try:
        parsed = json.loads(STRIPE_PRICE_IDS_JSON or "{}")
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items() if str(v).strip()}
    except json.JSONDecodeError:
        pass

    # Environment fallback for single-plan configuration.
    fallback_price_id = os.environ.get("STRIPE_DEFAULT_PRICE_ID", "").strip()
    return {"pro": fallback_price_id} if fallback_price_id else {}


def _ensure_stripe_configured() -> None:
    if stripe is None:
        raise HTTPException(status_code=500, detail="Stripe SDK is not installed.")
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="STRIPE_SECRET_KEY is not configured.")
    stripe.api_key = STRIPE_SECRET_KEY


class CreateCheckoutSessionRequest(BaseModel):
    plan: str = Field(default="pro", min_length=1)
    quantity: int = Field(default=1, ge=1, le=100)
    success_url: Optional[str] = None
    cancel_url: Optional[str] = None
    mode: str = Field(default="subscription", pattern="^(subscription|payment)$")


@router.get("/me")
async def get_my_billing(claims: dict = Depends(require_auth)):
    """Return billing state for the authenticated user."""
    user_id = resolve_actor_id(claims)
    return get_user_billing(user_id)


@router.post("/checkout-session")
async def create_checkout_session(req: CreateCheckoutSessionRequest, claims: dict = Depends(require_auth)):
    """Create a Stripe Checkout session for the authenticated user."""
    _ensure_stripe_configured()

    price_map = _resolve_price_map()
    price_id = price_map.get(req.plan)
    if not price_id:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown billing plan '{req.plan}'. Configure STRIPE_PRICE_IDS_JSON.",
        )

    user_id = resolve_actor_id(claims)
    existing = get_user_billing(user_id)

    try:
        customer_id = existing.get("stripe_customer_id")
        if not customer_id:
            customer = stripe.Customer.create(metadata={"user_id": user_id})
            customer_id = customer.id
            set_user_billing(user_id, stripe_customer_id=customer_id)

        session = stripe.checkout.Session.create(
            customer=customer_id,
            mode=req.mode,
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": req.quantity}],
            success_url=req.success_url or STRIPE_DEFAULT_SUCCESS_URL,
            cancel_url=req.cancel_url or STRIPE_DEFAULT_CANCEL_URL,
            metadata={"user_id": user_id, "plan": req.plan},
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Stripe checkout session error: {exc}") from exc

    return {
        "session_id": session.id,
        "checkout_url": session.url,
        "plan": req.plan,
    }


@router.post("/webhook", include_in_schema=False)
async def stripe_webhook(request: Request):
    """Process Stripe webhook events and update local entitlements."""
    if stripe is None:
        raise HTTPException(status_code=500, detail="Stripe SDK is not installed.")

    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")

    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="STRIPE_WEBHOOK_SECRET is not configured.")

    try:
        event = stripe.Webhook.construct_event(payload, signature, STRIPE_WEBHOOK_SECRET)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid Stripe webhook signature: {exc}") from exc

    event_type = event.get("type", "")
    data_object = (event.get("data") or {}).get("object") or {}
    append_billing_event(event_type, {"id": event.get("id"), "object": data_object.get("id")})

    def _resolve_user_id() -> Optional[str]:
        metadata = data_object.get("metadata") or {}
        if metadata.get("user_id"):
            return str(metadata["user_id"])
        customer_id = data_object.get("customer")
        if customer_id:
            return find_user_by_customer_id(str(customer_id))
        return None

    user_id = _resolve_user_id()

    if event_type == "checkout.session.completed":
        if user_id:
            mode = str(data_object.get("mode") or "subscription")
            plan = str(((data_object.get("metadata") or {}).get("plan") or "pro"))
            subscription_id = data_object.get("subscription") if mode == "subscription" else None
            set_user_billing(
                user_id,
                plan=plan,
                paid_tier=True,
                stripe_customer_id=data_object.get("customer"),
                stripe_subscription_id=str(subscription_id) if subscription_id else None,
            )

    elif event_type in {"customer.subscription.deleted", "invoice.payment_failed"}:
        if user_id:
            set_user_billing(user_id, plan="free", paid_tier=False)

    elif event_type in {"customer.subscription.updated", "invoice.paid"}:
        if user_id:
            status = str(data_object.get("status") or "")
            active = status in {"active", "trialing", "paid"} or event_type == "invoice.paid"
            set_user_billing(user_id, paid_tier=active)

    return {"received": True}
