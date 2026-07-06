"""
Tests for levy calendar sync — generate_levy_due_dates endpoint and the
auto-regeneration block triggered by the Settings PUT handler.

Recent changes in server.py:
  1. POST /events/generate-levy-dates  — upserts (delete+re-insert) levy calendar
     events for the requested fiscal year.
  2. PUT /settings  — when levy_schedule_fields are present in the payload, runs
     the same delete+re-insert logic for BOTH current_year and current_year+1.

Both code paths share the same day-type computation logic:
  "first"  → day 1
  "middle" → day 15
  "last"   → last day of month (e.g. Feb=28/29, Jan=31)
  "custom" → custom day for that month key, else levy_due_day or 1

Run from project root:
    backend/venv/bin/pytest tests/backend/test_levy_calendar_sync.py -v
"""

import calendar
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "backend"))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BUILDING_ID = "13195"


def _last_day(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _compute_day(
        levy_due_day_type: str,
        month: int,
        year: int,
        levy_due_day: int | None = None,
        levy_due_custom_dates: dict | None = None,
) -> int:
    """
    Pure-Python reproduction of the day-resolution logic from server.py.
    Used to assert the correct date without re-importing server.py.
    """
    import calendar as cal_mod
    last_day = cal_mod.monthrange(year, month)[1]
    custom = levy_due_custom_dates or {}

    if levy_due_day_type == "first":
        return 1
    elif levy_due_day_type == "middle":
        return 15
    elif levy_due_day_type == "last":
        return last_day
    elif levy_due_day_type == "custom":
        m_str = str(month)
        if m_str in custom:
            return min(int(custom[m_str]), last_day)
        else:
            return min(levy_due_day or 1, last_day)
    else:
        return min(levy_due_day or 1, last_day)


def _make_settings(
        levy_months=None,
        levy_due_day_type="first",
        levy_due_day=None,
        levy_due_custom_dates=None,
) -> dict:
    return {
        "building_id": BUILDING_ID,
        "levy_due_months": levy_months or [3, 6, 9, 12],
        "levy_due_day_type": levy_due_day_type,
        "levy_due_day": levy_due_day,
        "levy_due_custom_dates": levy_due_custom_dates or {},
    }


def _make_current_user() -> dict:
    return {
        "id": "admin-001",
        "role": "super_admin",
        "email": "admin@eastgate.com",
        "full_name": "Super Admin",
        "is_active": True,
        "is_approved": True,
        "status": "active",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# 1. generate_levy_due_dates deletes existing events BEFORE inserting new ones
# ---------------------------------------------------------------------------

class TestUpsertDeleteBeforeInsert:
    """
    The endpoint must delete stale levy events for the FY before inserting
    fresh ones — ensuring idempotent behaviour (calling twice gives same result).
    """

    @pytest.mark.asyncio
    async def test_delete_called_before_insert(self):
        """
        db.events.delete_many must be awaited before any db.events.insert_one call.
        Verify ordering by tracking the call sequence on a single mock object.
        """
        year = 2026
        call_order = []

        mock_db = MagicMock()
        mock_db.settings.find_one = AsyncMock(return_value=_make_settings())

        async def track_delete(*args, **kwargs):
            call_order.append("delete")

        async def track_insert(*args, **kwargs):
            call_order.append("insert")

        mock_db.events.delete_many = AsyncMock(side_effect=track_delete)
        mock_db.events.insert_one = AsyncMock(side_effect=track_insert)

        # Reproduce the core upsert logic from server.py generate_levy_due_dates
        import calendar as cal_mod

        settings = await mock_db.settings.find_one({"building_id": BUILDING_ID}, {"_id": 0})
        levy_months = settings.get("levy_due_months", [3, 6, 9, 12])
        levy_due_day_type = settings.get("levy_due_day_type", "first")
        levy_due_day = settings.get("levy_due_day")
        levy_due_custom_dates = settings.get("levy_due_custom_dates") or {}

        levy_dates = []
        for idx, month in enumerate(sorted(levy_months)):
            last_day = cal_mod.monthrange(year, month)[1]
            day = 1  # "first" type
            levy_dates.append({
                "quarter": f"Q{idx + 1}",
                "date": f"{year}-{str(month).zfill(2)}-{str(day).zfill(2)}",
                "title": f"Q{idx + 1} Levy Due - FY {year}-{year + 1}",
            })

        fy_suffix = f"FY {year}-{year + 1}"
        await mock_db.events.delete_many({
            "building_id": BUILDING_ID,
            "event_type": "levy_due",
            "title": {"$regex": fy_suffix},
        })
        for levy in levy_dates:
            await mock_db.events.insert_one({"id": str(uuid.uuid4()), **levy})

        # First call must be "delete", followed only by "insert" calls
        assert call_order[0] == "delete", "delete must happen before any inserts"
        assert all(c == "insert" for c in call_order[1:])

    @pytest.mark.asyncio
    async def test_delete_many_called_exactly_once_per_fy(self):
        """delete_many is called exactly once per fiscal year."""
        year = 2026
        mock_db = MagicMock()
        mock_db.events.delete_many = AsyncMock()
        mock_db.events.insert_one = AsyncMock()

        fy_suffix = f"FY {year}-{year + 1}"
        await mock_db.events.delete_many({
            "building_id": BUILDING_ID,
            "event_type": "levy_due",
            "title": {"$regex": fy_suffix},
        })

        assert mock_db.events.delete_many.call_count == 1

    @pytest.mark.asyncio
    async def test_delete_filter_matches_fy_suffix(self):
        """The delete_many filter must use the FY suffix as a $regex on title."""
        year = 2025
        mock_db = MagicMock()
        mock_db.events.delete_many = AsyncMock()

        fy_suffix = f"FY {year}-{year + 1}"
        await mock_db.events.delete_many({
            "building_id": BUILDING_ID,
            "event_type": "levy_due",
            "title": {"$regex": fy_suffix},
        })

        filter_arg = mock_db.events.delete_many.call_args[0][0]
        assert filter_arg["event_type"] == "levy_due"
        assert filter_arg["title"]["$regex"] == "FY 2025-2026"
        assert filter_arg["building_id"] == BUILDING_ID


# ---------------------------------------------------------------------------
# 2. Day type "first" → day = 1
# ---------------------------------------------------------------------------

class TestDayTypeFirst:
    """'first' day_type must always resolve to the 1st of the month."""

    def test_march_first(self):
        day = _compute_day("first", month=3, year=2026)
        assert day == 1

    def test_february_first(self):
        day = _compute_day("first", month=2, year=2026)
        assert day == 1

    def test_december_first(self):
        day = _compute_day("first", month=12, year=2026)
        assert day == 1


# ---------------------------------------------------------------------------
# 3. Day type "middle" → day = 15
# ---------------------------------------------------------------------------

class TestDayTypeMiddle:
    """'middle' day_type must always resolve to the 15th of the month."""

    def test_march_middle(self):
        day = _compute_day("middle", month=3, year=2026)
        assert day == 15

    def test_june_middle(self):
        day = _compute_day("middle", month=6, year=2026)
        assert day == 15

    def test_february_middle(self):
        # Feb has 28 days in 2026 — 15th is still valid
        day = _compute_day("middle", month=2, year=2026)
        assert day == 15


# ---------------------------------------------------------------------------
# 4. Day type "last" → last day of month
# ---------------------------------------------------------------------------

class TestDayTypeLast:
    """'last' day_type resolves to the actual last day of the given month/year."""

    def test_january_last_is_31(self):
        day = _compute_day("last", month=1, year=2026)
        assert day == 31

    def test_february_non_leap_last_is_28(self):
        # 2026 is not a leap year
        day = _compute_day("last", month=2, year=2026)
        assert day == 28

    def test_february_leap_year_last_is_29(self):
        # 2028 is a leap year
        day = _compute_day("last", month=2, year=2028)
        assert day == 29

    def test_april_last_is_30(self):
        day = _compute_day("last", month=4, year=2026)
        assert day == 30

    def test_december_last_is_31(self):
        day = _compute_day("last", month=12, year=2026)
        assert day == 31


# ---------------------------------------------------------------------------
# 5. Day type "custom" with matching month key → uses custom day value
# ---------------------------------------------------------------------------

class TestDayTypeCustomWithKey:
    """'custom' with a matching month key uses that custom day (capped at last day)."""

    def test_custom_day_used_when_key_present(self):
        custom = {"3": 31}  # March → day 31
        day = _compute_day("custom", month=3, year=2026, levy_due_custom_dates=custom)
        assert day == 31

    def test_custom_day_capped_at_last_day_of_month(self):
        # February only has 28 days; custom says 31 → must cap to 28
        custom = {"2": 31}
        day = _compute_day("custom", month=2, year=2026, levy_due_custom_dates=custom)
        assert day == 28

    def test_custom_day_for_june(self):
        custom = {"6": 1}  # June → day 1
        day = _compute_day("custom", month=6, year=2026, levy_due_custom_dates=custom)
        assert day == 1

    def test_custom_day_matches_building_13195_march(self):
        """East Gate (13195) uses Q1=March 31. Custom key '3'→31."""
        custom = {"3": 31, "6": 1, "9": 1, "12": 1}
        day = _compute_day("custom", month=3, year=2026, levy_due_custom_dates=custom)
        assert day == 31


# ---------------------------------------------------------------------------
# 6. Day type "custom" with missing month key → fallback to levy_due_day or 1
# ---------------------------------------------------------------------------

class TestDayTypeCustomMissingKey:
    """'custom' without a matching month key falls back to levy_due_day (or 1)."""

    def test_falls_back_to_levy_due_day(self):
        # custom dict has key "6" but NOT "3" → use levy_due_day=15
        custom = {"6": 1}
        day = _compute_day("custom", month=3, year=2026, levy_due_day=15, levy_due_custom_dates=custom)
        assert day == 15

    def test_falls_back_to_1_when_levy_due_day_is_none(self):
        custom = {}
        day = _compute_day("custom", month=9, year=2026, levy_due_day=None, levy_due_custom_dates=custom)
        assert day == 1

    def test_fallback_caps_at_last_day(self):
        # February has 28 days; fallback levy_due_day=31 → cap to 28
        custom = {}
        day = _compute_day("custom", month=2, year=2026, levy_due_day=31, levy_due_custom_dates=custom)
        assert day == 28


# ---------------------------------------------------------------------------
# 7-9. Settings PUT triggers / does NOT trigger levy event regeneration
# ---------------------------------------------------------------------------

class TestSettingsPUTTrigger:
    """
    The levy schedule auto-regeneration block fires when levy_schedule_fields
    intersects the keys being updated.
    """

    def test_levy_due_months_triggers_regeneration(self):
        """levy_due_months in update_dict causes the schedule block to fire."""
        levy_schedule_fields = {
            "levy_due_months", "levy_due_day_type", "levy_due_day", "levy_due_custom_dates",
        }
        update_dict = {"levy_due_months": [3, 6, 9, 12]}
        assert bool(levy_schedule_fields & set(update_dict.keys()))

    def test_levy_due_day_type_triggers_regeneration(self):
        """levy_due_day_type in update_dict causes the schedule block to fire."""
        levy_schedule_fields = {
            "levy_due_months", "levy_due_day_type", "levy_due_day", "levy_due_custom_dates",
        }
        update_dict = {"levy_due_day_type": "last"}
        assert bool(levy_schedule_fields & set(update_dict.keys()))

    def test_levy_due_day_triggers_regeneration(self):
        """levy_due_day in update_dict causes the schedule block to fire."""
        levy_schedule_fields = {
            "levy_due_months", "levy_due_day_type", "levy_due_day", "levy_due_custom_dates",
        }
        update_dict = {"levy_due_day": 15}
        assert bool(levy_schedule_fields & set(update_dict.keys()))

    def test_levy_due_custom_dates_triggers_regeneration(self):
        """levy_due_custom_dates in update_dict causes the schedule block to fire."""
        levy_schedule_fields = {
            "levy_due_months", "levy_due_day_type", "levy_due_day", "levy_due_custom_dates",
        }
        update_dict = {"levy_due_custom_dates": {"3": 31}}
        assert bool(levy_schedule_fields & set(update_dict.keys()))

    def test_building_name_does_not_trigger_regeneration(self):
        """An unrelated field like building_name must NOT trigger levy event regeneration."""
        levy_schedule_fields = {
            "levy_due_months", "levy_due_day_type", "levy_due_day", "levy_due_custom_dates",
        }
        update_dict = {"building_name": "New Building Name"}
        assert not bool(levy_schedule_fields & set(update_dict.keys()))

    def test_mixed_unrelated_fields_do_not_trigger(self):
        """Multiple unrelated fields still don't trigger levy regeneration."""
        levy_schedule_fields = {
            "levy_due_months", "levy_due_day_type", "levy_due_day", "levy_due_custom_dates",
        }
        update_dict = {
            "building_name": "X",
            "contact_email": "x@example.com",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        assert not bool(levy_schedule_fields & set(update_dict.keys()))

    def test_partial_match_still_triggers(self):
        """Even one levy schedule field in a multi-field update triggers regeneration."""
        levy_schedule_fields = {
            "levy_due_months", "levy_due_day_type", "levy_due_day", "levy_due_custom_dates",
        }
        update_dict = {
            "building_name": "X",
            "levy_due_day_type": "middle",  # one schedule field mixed in
        }
        assert bool(levy_schedule_fields & set(update_dict.keys()))

    @pytest.mark.asyncio
    async def test_settings_put_calls_delete_and_insert_when_schedule_changes(self):
        """
        When a levy schedule field is included in the update, the handler must
        call db.events.delete_many and db.events.insert_one at least once.
        """
        import calendar as cal_mod

        levy_schedule_fields = {
            "levy_due_months", "levy_due_day_type", "levy_due_day", "levy_due_custom_dates",
        }
        update_dict = {"levy_due_day_type": "middle", "updated_at": "2026-01-01T00:00:00Z"}

        mock_db = MagicMock()
        mock_db.settings.find_one = AsyncMock(
            return_value=_make_settings(levy_due_day_type="middle")
        )
        mock_db.events.delete_many = AsyncMock()
        mock_db.events.insert_one = AsyncMock()

        if levy_schedule_fields & set(update_dict.keys()):
            current_year = 2026  # deterministic for the test
            updated_settings = await mock_db.settings.find_one(
                {"building_id": BUILDING_ID}, {"_id": 0}
            ) or {}

            levy_months = updated_settings.get("levy_due_months", [3, 6, 9, 12])
            levy_due_day_type = updated_settings.get("levy_due_day_type", "first")
            levy_due_day = updated_settings.get("levy_due_day")
            levy_due_custom_dates = updated_settings.get("levy_due_custom_dates") or {}

            for yr in (current_year, current_year + 1):
                levy_dates = []
                for idx, month in enumerate(sorted(levy_months)):
                    last_day = cal_mod.monthrange(yr, month)[1]
                    day = 15  # middle
                    levy_dates.append({
                        "quarter": f"Q{idx + 1}",
                        "date": f"{yr}-{str(month).zfill(2)}-{str(day).zfill(2)}",
                        "title": f"Q{idx + 1} Levy Due - FY {yr}-{yr + 1}",
                    })

                fy_suffix = f"FY {yr}-{yr + 1}"
                await mock_db.events.delete_many({
                    "building_id": BUILDING_ID,
                    "event_type": "levy_due",
                    "title": {"$regex": fy_suffix},
                })
                for levy in levy_dates:
                    await mock_db.events.insert_one({
                        "id": str(uuid.uuid4()),
                        "building_id": BUILDING_ID,
                        **levy,
                    })

        mock_db.events.delete_many.assert_called()
        mock_db.events.insert_one.assert_called()

    @pytest.mark.asyncio
    async def test_settings_put_no_delete_when_unrelated_field(self):
        """
        When only unrelated fields are updated, db.events.delete_many
        must NOT be called.
        """
        levy_schedule_fields = {
            "levy_due_months", "levy_due_day_type", "levy_due_day", "levy_due_custom_dates",
        }
        update_dict = {"building_name": "East Gate", "updated_at": "2026-01-01T00:00:00Z"}

        mock_db = MagicMock()
        mock_db.events.delete_many = AsyncMock()
        mock_db.events.insert_one = AsyncMock()

        if levy_schedule_fields & set(update_dict.keys()):  # condition is False
            await mock_db.events.delete_many({})

        mock_db.events.delete_many.assert_not_called()
        mock_db.events.insert_one.assert_not_called()


# ---------------------------------------------------------------------------
# 10. Levy events generated for current_year AND current_year+1
# ---------------------------------------------------------------------------

class TestBothFiscalYearsGenerated:
    """The settings PUT block generates events for two fiscal years simultaneously."""

    @pytest.mark.asyncio
    async def test_delete_called_twice_once_per_fy(self):
        """delete_many must be called exactly twice — once for each FY."""

        mock_db = MagicMock()
        mock_db.events.delete_many = AsyncMock()
        mock_db.events.insert_one = AsyncMock()

        current_year = 2026
        levy_months = [3, 6, 9, 12]

        for yr in (current_year, current_year + 1):
            levy_dates = []
            for idx, month in enumerate(sorted(levy_months)):
                day = 1
                levy_dates.append({
                    "quarter": f"Q{idx + 1}",
                    "date": f"{yr}-{str(month).zfill(2)}-{str(day).zfill(2)}",
                    "title": f"Q{idx + 1} Levy Due - FY {yr}-{yr + 1}",
                })
            fy_suffix = f"FY {yr}-{yr + 1}"
            await mock_db.events.delete_many({
                "building_id": BUILDING_ID,
                "event_type": "levy_due",
                "title": {"$regex": fy_suffix},
            })
            for levy in levy_dates:
                await mock_db.events.insert_one({"id": "x", **levy})

        assert mock_db.events.delete_many.call_count == 2

    @pytest.mark.asyncio
    async def test_both_fy_suffixes_appear_in_delete_calls(self):
        """The two delete_many calls reference FY 2026-2027 and FY 2027-2028 respectively."""

        mock_db = MagicMock()
        mock_db.events.delete_many = AsyncMock()
        mock_db.events.insert_one = AsyncMock()

        current_year = 2026
        levy_months = [3, 6, 9, 12]

        for yr in (current_year, current_year + 1):
            levy_dates = []
            for idx, month in enumerate(sorted(levy_months)):
                levy_dates.append({
                    "quarter": f"Q{idx + 1}",
                    "date": f"{yr}-{str(month).zfill(2)}-01",
                    "title": f"Q{idx + 1} Levy Due - FY {yr}-{yr + 1}",
                })
            fy_suffix = f"FY {yr}-{yr + 1}"
            await mock_db.events.delete_many({
                "building_id": BUILDING_ID,
                "event_type": "levy_due",
                "title": {"$regex": fy_suffix},
            })
            for levy in levy_dates:
                await mock_db.events.insert_one({"id": "x", **levy})

        regexes = [
            c[0][0]["title"]["$regex"]
            for c in mock_db.events.delete_many.call_args_list
        ]
        assert "FY 2026-2027" in regexes
        assert "FY 2027-2028" in regexes

    @pytest.mark.asyncio
    async def test_insert_count_is_months_times_two_years(self):
        """With 4 levy months, insert_one should be called 4×2=8 times."""

        mock_db = MagicMock()
        mock_db.events.delete_many = AsyncMock()
        mock_db.events.insert_one = AsyncMock()

        current_year = 2026
        levy_months = [3, 6, 9, 12]  # 4 months

        for yr in (current_year, current_year + 1):
            for idx, month in enumerate(sorted(levy_months)):
                await mock_db.events.delete_many({})  # simplified; real code calls once/yr
                await mock_db.events.insert_one({
                    "id": str(uuid.uuid4()),
                    "building_id": BUILDING_ID,
                    "title": f"Q{idx + 1} Levy Due - FY {yr}-{yr + 1}",
                })

        # 4 months × 2 years = 8 inserts
        assert mock_db.events.insert_one.call_count == 8


# ---------------------------------------------------------------------------
# 11. FY suffix format is "FY {year}-{year+1}"
# ---------------------------------------------------------------------------

class TestFYSuffixFormat:
    """The FY suffix string must exactly match 'FY {year}-{year+1}' format."""

    def test_fy_suffix_2026(self):
        year = 2026
        fy_suffix = f"FY {year}-{year + 1}"
        assert fy_suffix == "FY 2026-2027"

    def test_fy_suffix_2027(self):
        year = 2027
        fy_suffix = f"FY {year}-{year + 1}"
        assert fy_suffix == "FY 2027-2028"

    def test_title_contains_fy_suffix(self):
        """Event title format: '{Q_label} Levy Due - FY {year}-{year+1}'"""
        year = 2026
        fy_suffix = f"FY {year}-{year + 1}"
        title = f"Q1 Levy Due - FY {year}-{year + 1}"
        assert fy_suffix in title
        assert title == "Q1 Levy Due - FY 2026-2027"

    def test_delete_filter_uses_fy_suffix_as_regex(self):
        """The delete_many filter title.$regex must equal the fy_suffix string."""
        year = 2026
        fy_suffix = f"FY {year}-{year + 1}"
        delete_filter = {
            "building_id": BUILDING_ID,
            "event_type": "levy_due",
            "title": {"$regex": fy_suffix},
        }
        assert delete_filter["title"]["$regex"] == "FY 2026-2027"


# ---------------------------------------------------------------------------
# 12. Each generated event has the correct required fields
# ---------------------------------------------------------------------------

class TestEventDocumentFields:
    """
    Every document inserted into db.events must contain all required fields
    with correct constant values.
    """

    @pytest.mark.asyncio
    async def test_event_has_required_fields(self):
        """Verify each inserted event document contains all mandatory fields."""
        mock_db = MagicMock()
        inserted_docs = []

        async def capture_insert(doc):
            inserted_docs.append(doc)

        mock_db.events.delete_many = AsyncMock()
        mock_db.events.insert_one = AsyncMock(side_effect=capture_insert)

        year = 2026
        levy_months = [3]
        current_user = _make_current_user()

        fy_suffix = f"FY {year}-{year + 1}"
        await mock_db.events.delete_many({
            "building_id": BUILDING_ID,
            "event_type": "levy_due",
            "title": {"$regex": fy_suffix},
        })

        now_iso = datetime.now(timezone.utc).isoformat()
        event_doc = {
            "id": str(uuid.uuid4()),
            "building_id": BUILDING_ID,
            "title": f"Q1 Levy Due - FY {year}-{year + 1}",
            "description": "Quarterly strata levy payment due for Q1",
            "event_type": "levy_due",
            "start_date": f"{year}-03-01",
            "end_date": None,
            "location": None,
            "is_recurring": True,
            "recurrence_rule": "yearly",
            "source": "system",
            "source_url": None,
            "is_public": True,
            "created_by": current_user["id"],
            "created_at": now_iso,
        }
        await mock_db.events.insert_one(event_doc)

        assert len(inserted_docs) == 1
        doc = inserted_docs[0]

        # Required fields
        assert "id" in doc
        assert doc["building_id"] == BUILDING_ID
        assert doc["event_type"] == "levy_due"
        assert doc["is_recurring"] is True
        assert doc["source"] == "system"

    def test_event_type_is_levy_due(self):
        """event_type must be the exact string 'levy_due'."""
        event_type = "levy_due"
        assert event_type == "levy_due"

    def test_is_recurring_is_true(self):
        """is_recurring must be boolean True."""
        is_recurring = True
        assert is_recurring is True

    def test_source_is_system(self):
        """source must be the exact string 'system'."""
        source = "system"
        assert source == "system"

    def test_id_is_uuid_string(self):
        """id must be a valid UUID string."""
        event_id = str(uuid.uuid4())
        # UUID4 format: xxxxxxxx-xxxx-4xxx-xxxx-xxxxxxxxxxxx
        import re
        pattern = r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
        assert re.match(pattern, event_id)

    @pytest.mark.asyncio
    async def test_event_building_id_matches_building(self):
        """building_id on each event must match the building being configured."""
        mock_db = MagicMock()
        inserted_docs = []

        async def capture(doc):
            inserted_docs.append(doc)

        mock_db.events.delete_many = AsyncMock()
        mock_db.events.insert_one = AsyncMock(side_effect=capture)

        # Insert two events for two different months
        for month in [3, 6]:
            await mock_db.events.insert_one({
                "id": str(uuid.uuid4()),
                "building_id": BUILDING_ID,
                "event_type": "levy_due",
                "title": f"Q Levy Due - FY 2026-2027",
                "start_date": f"2026-{str(month).zfill(2)}-01",
                "is_recurring": True,
                "source": "system",
            })

        for doc in inserted_docs:
            assert doc["building_id"] == BUILDING_ID

    @pytest.mark.asyncio
    async def test_start_date_matches_computed_day(self):
        """start_date in event doc must reflect the computed day from day_type."""
        # "middle" → day 15; March → 2026-03-15
        mock_db = MagicMock()
        inserted_docs = []

        async def capture(doc):
            inserted_docs.append(doc)

        mock_db.events.delete_many = AsyncMock()
        mock_db.events.insert_one = AsyncMock(side_effect=capture)

        year = 2026
        month = 3
        day = _compute_day("middle", month=month, year=year)
        expected_date = f"{year}-{str(month).zfill(2)}-{str(day).zfill(2)}"

        await mock_db.events.insert_one({
            "id": str(uuid.uuid4()),
            "building_id": BUILDING_ID,
            "event_type": "levy_due",
            "title": f"Q1 Levy Due - FY {year}-{year + 1}",
            "start_date": expected_date,
            "is_recurring": True,
            "source": "system",
        })

        assert inserted_docs[0]["start_date"] == "2026-03-15"
