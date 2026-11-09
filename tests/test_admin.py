from datetime import datetime, timezone

from app.models.payment_transaction import PaymentTransaction


def test_normal_user_cannot_access_admin_api(client):
    response = client.get("/admin/overview")
    assert response.status_code == 403


def test_admin_can_access_overview(client, db, test_user):
    test_user.is_admin = True
    db.commit()

    response = client.get("/admin/overview")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"users", "payments", "subscriptions", "monitoring", "infrastructure"}
    assert payload["users"]["total_users"] >= 1
    assert payload["infrastructure"]["database_connectivity"] == "connected"


def test_revenue_excludes_non_real_and_non_paid_transactions(client, db, test_user):
    test_user.is_admin = True
    now = datetime.now(timezone.utc)
    transactions = [
        PaymentTransaction(
            user_id=test_user.id,
            external_id="admin_real_paid",
            provider="qrisgate",
            provider_mode="live",
            revenue_classification="REAL",
            amount=49000,
            currency="IDR",
            status="PAID",
            paid_at=now,
        ),
        PaymentTransaction(
            user_id=test_user.id,
            external_id="admin_real_paid_current_price",
            provider="qrisgate",
            provider_mode="live",
            revenue_classification="REAL",
            amount=29000,
            currency="IDR",
            status="PAID",
            paid_at=now,
        ),
        PaymentTransaction(
            user_id=test_user.id,
            external_id="admin_sandbox_paid",
            provider="qrisgate",
            provider_mode="sandbox",
            revenue_classification="SANDBOX",
            amount=49000,
            currency="IDR",
            status="PAID",
            paid_at=now,
        ),
        PaymentTransaction(
            user_id=test_user.id,
            external_id="admin_unknown_paid",
            provider="xendit",
            provider_mode="live",
            revenue_classification="UNKNOWN",
            amount=49000,
            currency="IDR",
            status="PAID",
            paid_at=now,
        ),
        PaymentTransaction(
            user_id=test_user.id,
            external_id="admin_real_failed",
            provider="qrisgate",
            provider_mode="live",
            revenue_classification="REAL",
            amount=49000,
            currency="IDR",
            status="FAILED",
        ),
    ]
    db.add_all(transactions)
    db.commit()

    response = client.get("/admin/overview")

    assert response.status_code == 200
    payments = response.json()["payments"]
    assert payments["real_paid_transactions"] == 2
    assert payments["revenue_all_time"] == 78000
    assert payments["unknown_revenue_classification"] == 1


def test_order_lookup_enforces_transaction_ownership(client, db, other_user):
    transaction = PaymentTransaction(
        user_id=other_user.id,
        external_id="other_users_private_order",
        provider="qrisgate",
        provider_mode="live",
        revenue_classification="REAL",
        provider_payment_id="other-private-reference",
        amount=49000,
        currency="IDR",
        status="PENDING",
    )
    db.add(transaction)
    db.commit()

    assert client.get(f"/billing/order/{transaction.external_id}").status_code == 404
    assert client.get(f"/api/v1/transactions/{transaction.id}").status_code == 404


def test_admin_payment_table_is_read_only_and_requires_admin(client, db, test_user):
    transaction = PaymentTransaction(
        user_id=test_user.id,
        external_id="admin_payment_table",
        provider="duitku",
        provider_mode="sandbox",
        revenue_classification="UNKNOWN",
        provider_payment_id="historical-reference",
        amount=49000,
        currency="IDR",
        status="EXPIRED",
    )
    db.add(transaction)
    db.commit()

    forbidden = client.get("/admin/payments")
    assert forbidden.status_code == 403

    test_user.is_admin = True
    db.commit()
    response = client.get("/admin/payments?provider=duitku&status=EXPIRED&mode=sandbox")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["items"][0]["payment_reference"] == "historical-reference"
    assert payload["items"][0]["user_email"] == test_user.email
    assert client.patch(f"/admin/payments/{transaction.id}", json={"status": "PAID"}).status_code == 404
