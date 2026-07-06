from unittest.mock import AsyncMock, MagicMock

import pytest

from services.bank_feed_service import BankFeedService


@pytest.mark.asyncio
async def test_bank_feed_service_reports_provider_metadata():
    mock_db = MagicMock()
    mock_db.bank_feed_runs.insert_one = AsyncMock()
    mock_db.bank_feed_runs.update_one = AsyncMock()
    mock_db.bank_transactions.find_one = AsyncMock(return_value=None)
    mock_db.bank_transactions.insert_one = AsyncMock()

    service = BankFeedService(
        db=mock_db,
        provider_name="csv_replay",
        provider_mode="sandbox",
    )

    result = await service.ingest(
        building_id="13195",
        account_id="trust-1",
        connection_id="demo-feed",
        fund_type="admin_fund",
        initiated_by="user-1",
    )

    assert result["provider_name"] == "csv_replay"
    assert result["provider_mode"] == "sandbox"
    inserted_run = mock_db.bank_feed_runs.insert_one.await_args.args[0]
    assert inserted_run["provider_name"] == "csv_replay"
    assert inserted_run["provider_mode"] == "sandbox"


@pytest.mark.asyncio
async def test_bank_feed_service_rejects_unconfigured_live_provider():
    mock_db = MagicMock()
    mock_db.bank_feed_runs.insert_one = AsyncMock()
    mock_db.bank_feed_runs.update_one = AsyncMock()
    mock_db.bank_transactions.find_one = AsyncMock(return_value=None)
    mock_db.bank_transactions.insert_one = AsyncMock()

    service = BankFeedService(
        db=mock_db,
        provider_name="basiq",
        provider_mode="sandbox",
    )

    with pytest.raises(NotImplementedError):
        await service.ingest(
            building_id="13195",
            account_id="trust-1",
            connection_id="demo-feed",
            fund_type="admin_fund",
            initiated_by="user-1",
        )
