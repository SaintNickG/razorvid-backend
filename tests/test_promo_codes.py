from datetime import datetime, timedelta, timezone

import pytest

from multicam_pipeline import billing_store


def _use_local_store(monkeypatch, tmp_path):
    monkeypatch.setattr(billing_store, "BILLING_STORE_PATH", tmp_path / "billing.json")
    monkeypatch.setattr(billing_store, "_use_dynamodb", lambda: False)


def test_pro_month_code_is_single_use_and_grants_expiring_shared_entitlement(monkeypatch, tmp_path):
    _use_local_store(monkeypatch, tmp_path)
    code = billing_store.issue_pro_month_codes(1, "admin-user")[0]
    store = billing_store._load_store()
    code_hash = billing_store._promo_hash(code)

    assert code.startswith("RVPRO-")
    assert code_hash in store["promo_codes"]
    assert code not in str(store["promo_codes"])

    result = billing_store.redeem_pro_month_code(code, "tester")
    expires = datetime.fromisoformat(result["promo_paid_until"])
    remaining = expires - datetime.now(timezone.utc)
    assert timedelta(days=29, hours=23) < remaining <= timedelta(days=30, seconds=1)
    assert billing_store.has_paid_tier_access("tester") is True
    assert billing_store.get_user_billing_quota("tester")["is_paid_tier"] is True

    with pytest.raises(ValueError, match="already been redeemed"):
        billing_store.redeem_pro_month_code(code, "another-user")


def test_pro_month_code_refuses_to_override_an_active_paid_subscription(monkeypatch, tmp_path):
    _use_local_store(monkeypatch, tmp_path)
    code = billing_store.issue_pro_month_codes(1, "admin-user")[0]
    billing_store.set_user_billing("paid-user", plan="pro", paid_tier=True)

    with pytest.raises(ValueError, match="active paid Pro subscription"):
        billing_store.redeem_pro_month_code(code, "paid-user")

    promo = billing_store._load_store()["promo_codes"][billing_store._promo_hash(code)]
    assert promo["status"] == "available"
