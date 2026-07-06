"""
tests/backend/integrations/test_bank_feed_router.py — Tests for the mock bank feed upload router.

Covers the bulk insert path (insert_many with ordered=False), BulkWriteError duplicate handling,
role guards, file size limit, and unknown bank rejection.

No running backend needed. DB is mocked with AsyncMock.
"""
from __future__ import annotations

import io
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pymongo.errors import BulkWriteError

from strataos_demo_integrations.demo_bank.mocks.routers.bank_feed_router import upload_bank_statement

_BUILDING = "16244"

_MANAGER_USER = {"role": "strata_manager", "effective_role": "strata_manager", "user_id": "u1"}
_OWNER_USER = {"role": "owner", "effective_role": "owner", "user_id": "u2"}

CBA_CSV = (
    "Date,Amount,Description,Balance\n"
    "15/04/2026,Opening Balance,,\n"
    "16/04/2026,-1500.00,BPAY LEVY PAYMENT,-12500.00\n"
    "17/04/2026,2937.00,EFT CREDIT BOND,5437.00\n"
)


def _make_upload_file(content: str = CBA_CSV, filename: str = "statement.csv") -> MagicMock:
    uf = MagicMock()
    uf.filename = filename
    uf.read = AsyncMock(return_value=content.encode())
    return uf


def _make_insert_many_result(count: int) -> MagicMock:
    result = MagicMock()
    result.inserted_ids = [f"id{i}" for i in range(count)]
    return result


class TestUploadBankStatement:
    """Covers the happy path and bulk insert mechanics."""

    @pytest.mark.asyncio
    async def test_happy_path_all_accepted(self):
        """Valid CBA CSV with 2 data rows → both accepted in a single insert_many."""
        mock_db = MagicMock()
        mock_db.integration_inbox.insert_many = AsyncMock(
            return_value=_make_insert_many_result(2)
        )

        with patch("database.db", mock_db):
            result = await upload_bank_statement(
                file=_make_upload_file(),
                bank="cba",
                account_ref="trust-001",
                building_id=_BUILDING,
                current_user=_MANAGER_USER,
            )

        assert result.accepted == 2
        assert result.duplicate == 0
        assert result.errors == 0
        assert result.bank == "cba"
        assert result.account_ref == "trust-001"

    @pytest.mark.asyncio
    async def test_bulk_insert_called_once(self):
        """insert_many is called exactly once regardless of row count (no N+1)."""
        mock_db = MagicMock()
        mock_db.integration_inbox.insert_many = AsyncMock(
            return_value=_make_insert_many_result(2)
        )

        with patch("database.db", mock_db):
            await upload_bank_statement(
                file=_make_upload_file(),
                bank="cba",
                account_ref="trust-001",
                building_id=_BUILDING,
                current_user=_MANAGER_USER,
            )

        mock_db.integration_inbox.insert_many.assert_called_once()

    @pytest.mark.asyncio
    async def test_bulk_write_error_duplicates_counted(self):
        """BulkWriteError with code 11000 increments duplicate counter, not errors."""
        bwe = BulkWriteError({
            "writeErrors": [
                {"code": 11000, "errmsg": "duplicate key"},
                {"code": 11000, "errmsg": "duplicate key"},
            ],
            "nInserted": 0,
        })
        mock_db = MagicMock()
        mock_db.integration_inbox.insert_many = AsyncMock(side_effect=bwe)

        with patch("database.db", mock_db):
            result = await upload_bank_statement(
                file=_make_upload_file(),
                bank="cba",
                account_ref="trust-001",
                building_id=_BUILDING,
                current_user=_MANAGER_USER,
            )

        assert result.duplicate == 2
        assert result.errors == 0
        assert result.accepted == 0

    @pytest.mark.asyncio
    async def test_bulk_write_error_mixed_accepted_and_duplicate(self):
        """BulkWriteError: nInserted counts accepted, duplicate-key errors count duplicates."""
        bwe = BulkWriteError({
            "writeErrors": [
                {"code": 11000, "errmsg": "duplicate key"},
            ],
            "nInserted": 1,
        })
        mock_db = MagicMock()
        mock_db.integration_inbox.insert_many = AsyncMock(side_effect=bwe)

        with patch("database.db", mock_db):
            result = await upload_bank_statement(
                file=_make_upload_file(),
                bank="cba",
                account_ref="trust-001",
                building_id=_BUILDING,
                current_user=_MANAGER_USER,
            )

        assert result.accepted == 1
        assert result.duplicate == 1
        assert result.errors == 0

    @pytest.mark.asyncio
    async def test_empty_csv_no_insert_called(self):
        """If CSV parses to zero rows, insert_many is not called."""
        mock_db = MagicMock()
        mock_db.integration_inbox.insert_many = AsyncMock()
        empty_csv = "Date,Amount,Description,Balance\n"

        with patch("database.db", mock_db):
            result = await upload_bank_statement(
                file=_make_upload_file(empty_csv),
                bank="cba",
                account_ref="trust-001",
                building_id=_BUILDING,
                current_user=_MANAGER_USER,
            )

        mock_db.integration_inbox.insert_many.assert_not_called()
        assert result.accepted == 0
        assert result.duplicate == 0


class TestUploadRoleGuard:
    """Role gate: only strata_manager / chairman / super_admin allowed."""

    @pytest.mark.asyncio
    async def test_owner_role_rejected(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            await upload_bank_statement(
                file=_make_upload_file(),
                bank="cba",
                account_ref="trust-001",
                building_id=_BUILDING,
                current_user=_OWNER_USER,
            )
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_chairman_role_accepted(self):
        # "chairman" is not a top-level role (migration 0025 / commit 67fbc4a5) — it is
        # ECPosition.CHAIRMAN on a user whose role is ec_member. This fixture predates
        # that change; the router's role gate correctly checks for "ec_member".
        mock_db = MagicMock()
        mock_db.integration_inbox.insert_many = AsyncMock(
            return_value=_make_insert_many_result(2)
        )
        chairman = {
            "role": "ec_member", "effective_role": "ec_member",
            "ec_position": "CHAIRMAN", "user_id": "u3",
        }

        with patch("database.db", mock_db):
            result = await upload_bank_statement(
                file=_make_upload_file(),
                bank="cba",
                account_ref="trust-001",
                building_id=_BUILDING,
                current_user=chairman,
            )
        assert result.accepted == 2

    @pytest.mark.asyncio
    async def test_super_admin_role_accepted(self):
        mock_db = MagicMock()
        mock_db.integration_inbox.insert_many = AsyncMock(
            return_value=_make_insert_many_result(2)
        )
        admin = {"role": "owner", "effective_role": "super_admin", "user_id": "u4"}

        with patch("database.db", mock_db):
            result = await upload_bank_statement(
                file=_make_upload_file(),
                bank="cba",
                account_ref="trust-001",
                building_id=_BUILDING,
                current_user=admin,
            )
        assert result.accepted == 2


class TestUploadValidation:
    """File size limit and unknown bank rejection."""

    @pytest.mark.asyncio
    async def test_oversized_file_rejected(self):
        from fastapi import HTTPException
        big = _make_upload_file()
        big.read = AsyncMock(return_value=b"x" * (10 * 1024 * 1024 + 1))

        with pytest.raises(HTTPException) as exc_info:
            await upload_bank_statement(
                file=big,
                bank="cba",
                account_ref="trust-001",
                building_id=_BUILDING,
                current_user=_MANAGER_USER,
            )
        assert exc_info.value.status_code == 413

    @pytest.mark.asyncio
    async def test_unknown_bank_rejected(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            await upload_bank_statement(
                file=_make_upload_file(),
                bank="fictional_bank",
                account_ref="trust-001",
                building_id=_BUILDING,
                current_user=_MANAGER_USER,
            )
        assert exc_info.value.status_code == 422

    @pytest.mark.asyncio
    async def test_multi_tenant_building_id_in_docs(self):
        """Documents inserted must carry the caller's building_id, not a hardcoded one."""
        captured_docs = []

        async def mock_insert_many(docs, ordered=True):
            captured_docs.extend(docs)
            result = MagicMock()
            result.inserted_ids = [f"id{i}" for i in range(len(docs))]
            return result

        mock_db = MagicMock()
        mock_db.integration_inbox.insert_many = mock_insert_many

        with patch("database.db", mock_db):
            await upload_bank_statement(
                file=_make_upload_file(),
                bank="cba",
                account_ref="trust-001",
                building_id="harbourside_view",
                current_user=_MANAGER_USER,
            )

        assert len(captured_docs) > 0
        for doc in captured_docs:
            assert doc["tenant_id"] == "harbourside_view"
            assert "13195" not in str(doc["tenant_id"])
