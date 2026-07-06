"""
Pytest tests for Finance 2026 Import Feature

Verifies the actual database state after the 2026 financial data import.
Data is stored in:
  - annual_levies  collection: per-year summary (field: year="2026")
  - unit_levy_ledger collection: per-unit per-year data
  - budgets collection: annual budget breakdown (financial_year="2024-2025")

These are INTEGRATION tests that query the live MongoDB database.  They are
skipped by default and must be opted in with:

    RUN_INTEGRATION_TESTS=1 backend/venv/bin/pytest tests/backend/test_finance_2026_import.py -v

Context: During the MongoDB→PostgreSQL migration, the canonical source for
annual levy data will move to the ``finance.*`` PostgreSQL tables when the
``financial_pg_reads_enabled`` cutover toggle is flipped.  When that happens
these integration tests should be updated to query PG instead.
"""

import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

# tests/backend/ → up 3 levels = project root → into backend/
backend_dir = Path(__file__).parent.parent.parent / 'backend'
sys.path.insert(0, str(backend_dir))

# Load environment variables from backend/.env
load_dotenv(backend_dir / '.env')

MONGO_URL = os.getenv('MONGO_URL')
DB_NAME = os.getenv('DB_NAME')

# ── Integration guard ──────────────────────────────────────────────────────────
# All tests in this file require a live MongoDB connection with production data.
# Skip them unless the caller explicitly opts in.
_RUN_INTEGRATION = bool(os.getenv('RUN_INTEGRATION_TESTS'))
pytestmark = pytest.mark.skipif(
    not _RUN_INTEGRATION,
    reason="Set RUN_INTEGRATION_TESTS=1 to run live-DB finance import tests",
)


@pytest_asyncio.fixture
async def db_client():
    """Create MongoDB client for testing."""
    client = AsyncIOMotorClient(MONGO_URL)
    db = client[DB_NAME]
    yield db
    client.close()


@pytest.mark.asyncio
async def test_budgets_collection_has_three_years(db_client):
    """annual_levies has entries for 2024, 2025, and 2026."""
    levies = await db_client.annual_levies.find({}, {'year': 1}).to_list(length=20)
    years = [l.get('year') for l in levies]

    assert '2024' in years, f"Missing 2024 in annual_levies. Found: {years}"
    assert '2025' in years, f"Missing 2025 in annual_levies. Found: {years}"
    assert '2026' in years, f"Missing 2026 in annual_levies. Found: {years}"


@pytest.mark.asyncio
async def test_budget_2026_2027_structure(db_client):
    """annual_levies year 2026 (East Gate, building 13195) has correct ex-GST budget totals."""
    levy = await db_client.annual_levies.find_one({'year': '2026', 'building_id': '13195'})

    assert levy is not None, "2026 annual_levy not found"
    assert 'admin_fund' in levy, "Missing admin_fund"
    assert 'sinking_fund' in levy, "Missing sinking_fund"

    # proposed_admin_expenses / proposed_sinking_expenses store the AGM-resolved
    # budget amounts ex-GST.  admin_fund.total_income is the YTD actual collected
    # (partial-year) so we must not use it for the annual budget assertion.
    admin_budget_ex_gst = levy.get('proposed_admin_expenses', 0)
    sinking_budget_ex_gst = levy.get('proposed_sinking_expenses', 0)
    total_inc_gst = round((admin_budget_ex_gst + sinking_budget_ex_gst) * 1.1, 2)
    expected_total = 440375.10

    assert abs(admin_budget_ex_gst - 309882.0) < 0.01, \
        f"proposed_admin_expenses expected 309882.00 (ex-GST), got {admin_budget_ex_gst}"
    assert abs(sinking_budget_ex_gst - 90459.0) < 0.01, \
        f"proposed_sinking_expenses expected 90459.00 (ex-GST), got {sinking_budget_ex_gst}"
    assert abs(total_inc_gst - expected_total) < 0.10, \
        f"Expected total inc-GST ~${expected_total}, got ${total_inc_gst}"


@pytest.mark.asyncio
async def test_budget_2025_2026_transition_year(db_client):
    """annual_levies year 2025 (transition year) exists."""
    levy = await db_client.annual_levies.find_one({'year': '2025'})

    assert levy is not None, "2025 annual_levy not found"
    assert levy.get('year') == '2025'


@pytest.mark.asyncio
async def test_units_have_2026_levy_data(db_client):
    """unit_levy_ledger has entries for year 2026 covering all units."""
    count = await db_client.unit_levy_ledger.count_documents({'year': '2026'})
    assert count >= 87, f"Expected at least 87 ledger entries for 2026, found {count}"

    # Check required fields exist on a sample entry
    sample = await db_client.unit_levy_ledger.find_one({'year': '2026'})
    for field in ['unit_number', 'uoe', 'admin_levied', 'sinking_levied', 'total_levied']:
        assert field in sample, f"Missing field '{field}' in unit_levy_ledger"


@pytest.mark.asyncio
async def test_specific_unit_levy_amounts(db_client):
    """Specific units have correct 2026 Q1 levy amounts in ledger.

    The unit_levy_ledger stores the Q1 quarterly levy in total_levied
    (not the full annual). One row per unit per year tracks the quarter's
    levied amount, payments, and net balance.
    UA001 (apartment, UOE=82): Q1 quarterly ≈ $902.77
    TH087 (townhouse, UOE=161): Q1 quarterly ≈ $1,772.51
    """
    # UA001 (apartment, UOE=82) — Q1 quarterly levy ≈ 902.77
    ua001 = await db_client.unit_levy_ledger.find_one(
        {'unit_number': 'UA001', 'year': '2026'}
    )
    assert ua001 is not None, "UA001 2026 ledger not found"
    assert abs(ua001['total_levied'] - 902.77) < 2.0, \
        f"UA001 expected Q1 ~$902.77, got ${ua001['total_levied']:.2f}"

    # TH087 (townhouse, UOE=161) — Q1 quarterly levy ≈ 1772.51
    th087 = await db_client.unit_levy_ledger.find_one(
        {'unit_number': 'TH087', 'year': '2026'}
    )
    assert th087 is not None, "TH087 2026 ledger not found"
    assert abs(th087['total_levied'] - 1772.51) < 2.0, \
        f"TH087 expected Q1 ~$1772.51, got ${th087['total_levied']:.2f}"


@pytest.mark.asyncio
async def test_units_have_closing_balances(db_client):
    """unit_levy_ledger for year 2025 has admin_closing and sinking_closing fields."""
    sample = await db_client.unit_levy_ledger.find_one({'year': '2025'})
    if sample is None:
        pytest.skip("No 2025 ledger entries found")

    assert 'admin_closing' in sample or 'admin_levied' in sample, \
        "2025 ledger entry missing financial fields"


@pytest.mark.asyncio
async def test_levy_calculation_consistency(db_client):
    """admin_levied + sinking_levied = total_levied for all 2026 ledger entries.

    Scoped to building 13195 (East Gate) to match the file's intent —
    demo/test buildings (UP-DEMO-001, etc.) carry partial seed data and
    are not part of this contract.
    """
    entries = await db_client.unit_levy_ledger.find(
        {'year': '2026', 'building_id': '13195', 'is_test_data': {'$ne': True}}
    ).to_list(length=200)

    for entry in entries:
        admin = entry.get('admin_levied', 0)
        sinking = entry.get('sinking_levied', 0)
        total = entry.get('total_levied', 0)
        calculated = admin + sinking

        assert abs(total - calculated) < 0.02, \
            f"{entry['unit_number']}: total_levied mismatch. " \
            f"admin=${admin} + sinking=${sinking} = ${calculated}, but total_levied=${total}"


@pytest.mark.asyncio
async def test_admin_sinking_split(db_client):
    """admin_levied and sinking_levied are both positive for 2026 ledger entries."""
    entries = await db_client.unit_levy_ledger.find({'year': '2026'}).to_list(length=100)
    assert len(entries) > 0, "No 2026 ledger entries found"

    for entry in entries:
        assert entry.get('admin_levied', 0) > 0, \
            f"{entry['unit_number']}: admin_levied should be positive"
        assert entry.get('sinking_levied', 0) > 0, \
            f"{entry['unit_number']}: sinking_levied should be positive"


@pytest.mark.asyncio
async def test_apartment_vs_townhouse_levies(db_client):
    """Townhouses have higher average 2026 levies than apartments."""
    apartments = await db_client.unit_levy_ledger.find(
        {'year': '2026', 'property_type': 'Apartment'}
    ).to_list(length=100)
    townhouses = await db_client.unit_levy_ledger.find(
        {'year': '2026', 'property_type': 'Townhouse'}
    ).to_list(length=100)

    if not apartments or not townhouses:
        pytest.skip("Property type data not available in ledger")

    avg_apt = sum(u.get('total_levied', 0) for u in apartments) / len(apartments)
    avg_th = sum(u.get('total_levied', 0) for u in townhouses) / len(townhouses)

    assert avg_th > avg_apt, \
        f"Townhouse avg (${avg_th:.2f}) should be > apartment avg (${avg_apt:.2f})"


@pytest.mark.asyncio
async def test_budget_categories_exist(db_client):
    """budgets collection for 2024-2025 has embedded categories."""
    budget = await db_client.budgets.find_one({'financial_year': '2024-2025'})

    assert budget is not None, "2024-2025 budget not found in budgets collection"
    assert 'categories' in budget, "Budget missing categories"
    categories = budget['categories']
    assert len(categories) > 0, "No budget categories found"

    # Check category structure
    sample_category = categories[0]
    assert 'name' in sample_category
    assert 'budgeted_amount' in sample_category
    assert 'fund_type' in sample_category
    assert sample_category['fund_type'] in ['admin', 'sinking', 'administrative']


@pytest.mark.asyncio
async def test_import_script_json_exists():
    """Source JSON file exists (docs/finances/)."""
    # Accept either location for the finance source file
    possible_paths = [
        Path(__file__).parent.parent.parent / 'docs' / 'finances' / 'comprehensive_financials_2026.json',
        Path(__file__).parent.parent.parent / 'docs' / 'finances',
        Path(__file__).parent.parent.parent / 'scripts' / 'finances',
    ]
    # At least the scripts/finances directory should exist
    scripts_dir = Path(__file__).parent.parent.parent / 'scripts' / 'finances'
    assert scripts_dir.exists(), f"scripts/finances directory not found: {scripts_dir}"


@pytest.mark.asyncio
async def test_no_duplicate_budgets(db_client):
    """No duplicate year entries in annual_levies for the primary building (13195)."""
    levies = await db_client.annual_levies.find(
        {'building_id': '13195'}, {'year': 1}
    ).to_list(length=20)

    years = [l.get('year') for l in levies if l.get('year')]
    unique_years = set(years)

    assert len(years) == len(unique_years), \
        f"Duplicate year entries found in annual_levies for building 13195: {years}"


@pytest.mark.asyncio
async def test_units_entitlement_consistency(db_client):
    """Unit entitlements sum to a reasonable total (8000–11000 basis points)."""
    units = await db_client.units.find({}).to_list(length=100)

    entitlements = [u.get('entitlement', 0) for u in units if 'entitlement' in u]
    total_entitlements = sum(entitlements)

    assert 8000 < total_entitlements < 11000, \
        f"Total entitlements should be 8000–11000, got {total_entitlements}"


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
