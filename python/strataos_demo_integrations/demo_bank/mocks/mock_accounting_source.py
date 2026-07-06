"""
backend/integrations/mocks/mock_accounting_source.py — Mock AccountingProvider.

# @featuretrace:financial_integration_v2 — mock supplier accounting integration.
# Layer: service
# Data flow: mock supplier bills → AccountingProvider → AP queue → invoice lifecycle (building-scoped).
# Related: backend/integrations/mocks/routers/accounting_router.py
#          backend/domain/invoice_lifecycle.py
#          backend/routers/ap_approval.py

Maintains simulated Xero/MYOB bills in the `mock_accounting_bills` collection.
An admin endpoint lets testers generate realistic invoice documents without
needing a real accounting system connection.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from integrations.envelopes import AccountingBill, EventEnvelope, generate_ulid

logger = logging.getLogger(__name__)


class MockAccountingSource:
    """Mock AccountingProvider backed by MongoDB-persisted simulated bills.

    name = "mock_accounting_source"
    """

    name = "mock_accounting_source"

    async def list_bills(
            self,
            since: datetime,
    ) -> list[AccountingBill]:
        """Return all mock bills with issue_date >= since."""
        from database import db
        from request_context import get_ctx_building_id

        building_id = get_ctx_building_id()
        cursor = db.mock_accounting_bills.find(
            {
                "tenant_id": building_id,
                "issue_date": {"$gte": since.isoformat()},
            }
        ).sort("issue_date", 1)

        bills: list[AccountingBill] = []
        async for doc in cursor:
            try:
                bills.append(AccountingBill(
                    provider_bill_id=doc.get("bill_id", str(doc["_id"])),
                    vendor_name=doc.get("vendor_name", "Unknown Vendor"),
                    vendor_abn=doc.get("vendor_abn"),
                    invoice_number=doc.get("invoice_number"),
                    issue_date=datetime.fromisoformat(doc["issue_date"])
                    if doc.get("issue_date") else None,
                    due_date=datetime.fromisoformat(doc["due_date"])
                    if doc.get("due_date") else None,
                    total_cents=doc.get("total_cents", 0),
                    gst_cents=doc.get("gst_cents", 0),
                    raw=doc,
                ))
            except Exception as exc:
                logger.warning("Skipping malformed mock bill %s: %s", doc.get("_id"), exc)

        return bills

    async def submit_invoice(
            self,
            invoice: dict,
            idempotency_key: str,
    ) -> EventEnvelope:
        """Record that the strata platform submitted an invoice to the accounting system."""
        from request_context import get_ctx_building_id

        building_id = get_ctx_building_id()
        now = datetime.now(timezone.utc)

        return EventEnvelope(
            provider_event_id=idempotency_key,
            idempotency_key=idempotency_key,
            tenant_id=building_id,
            occurred_at=now,
            event_type="accounting_invoice_submitted",
            raw={"invoice": invoice, "provider": self.name},
        )

    async def verify_webhook(
            self,
            headers: dict[str, str],
            raw_body: bytes,
    ) -> bool:
        return False

    async def parse_webhook(
            self,
            raw_body: bytes,
    ) -> list[EventEnvelope]:
        return []

    # ── Mock data generation (used by admin API endpoint) ─────────────────────

    async def create_mock_bill(
            self,
            building_id: str,
            vendor_name: str,
            vendor_abn: Optional[str],
            invoice_number: str,
            total_cents: int,
            gst_cents: int,
            issue_date: datetime,
            due_date: Optional[datetime] = None,
            is_test_data: bool = True,
    ) -> str:
        """Insert a simulated bill for end-to-end AP pipeline testing."""
        from database import db

        bill_id = generate_ulid()
        now = datetime.now(timezone.utc)

        await db.mock_accounting_bills.insert_one({
            "tenant_id": building_id,
            "bill_id": bill_id,
            "vendor_name": vendor_name,
            "vendor_abn": vendor_abn,
            "invoice_number": invoice_number,
            "total_cents": total_cents,
            "gst_cents": gst_cents,
            "issue_date": issue_date.isoformat(),
            "due_date": due_date.isoformat() if due_date else None,
            "is_test_data": is_test_data,
            "created_at": now.isoformat(),
        })
        return bill_id
