"""Goals & KPI checks: money parsing, progress math, month bounds, run summaries.

All pure: the same inputs always give the same numbers, which is what makes KPI rows
auditable. DB-touching service calls ride the live container like the rest of Wave 1.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import re

import pytest

from app import agents as agents_svc
from app.agents import AgentError

MIGRATION_008 = (
    pathlib.Path(__file__).resolve().parents[1] / "supabase" / "migrations" / "008_goals_kpis.sql"
).read_text(encoding="utf-8")


def test_008_creates_three_tables():
    for table in ("kpi_defs", "kpi_values", "goals"):
        assert f"CREATE TABLE IF NOT EXISTS public.{table}" in MIGRATION_008, table


def test_008_rls_forced_and_anon_denied():
    """RLS rides the same format() loop as 001, so assert the array + templates."""
    assert "ARRAY['kpi_defs','kpi_values','goals']" in MIGRATION_008
    for statement in (
        "ENABLE ROW LEVEL SECURITY",
        "FORCE ROW LEVEL SECURITY",
        "REVOKE ALL ON public.%I FROM anon",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON public.%I TO authenticated",
        "GRANT ALL ON public.%I TO service_role",
    ):
        assert statement in MIGRATION_008, statement
    assert "app_private.row_visible(org_id, team_id)" in MIGRATION_008


def test_008_one_goal_per_scope_month():
    """A second goal for the same team+month must upsert, never duplicate."""
    assert "goals_uniq" in MIGRATION_008
    assert "kpi_values_uniq" in MIGRATION_008


def test_008_is_rerunnable():
    assert "CREATE TABLE IF NOT EXISTS" in MIGRATION_008
    assert "CREATE UNIQUE INDEX IF NOT EXISTS" in MIGRATION_008
    assert "DROP POLICY IF EXISTS" in MIGRATION_008
    assert not re.search(r"^\s*CREATE TYPE\b", MIGRATION_008, re.MULTILINE)


def test_parse_brl_accepts_both_formats():
    assert agents_svc.parse_brl("50.000,00") == 50000.0
    assert agents_svc.parse_brl("1234,56") == 1234.56
    assert agents_svc.parse_brl("1234.56") == 1234.56
    assert agents_svc.parse_brl("R$ 12,30") == 12.3


def test_parse_brl_rejects_garbage_and_negatives():
    with pytest.raises(AgentError):
        agents_svc.parse_brl("doze")
    with pytest.raises(AgentError):
        agents_svc.parse_brl("-5,00")


def test_goal_progress_math():
    assert agents_svc.goal_progress(100, 25)["pct"] == 0.25
    assert agents_svc.goal_progress(100, 150)["remaining"] == 0
    assert agents_svc.goal_progress(0, 50)["pct"] is None


def test_month_bounds_valid_and_invalid():
    start, end = agents_svc.month_bounds("2026-10")
    assert start.startswith("2026-10-01") and end.startswith("2026-11-01")
    for bad in ("2026-13", "outubro", "2026-1", ""):
        with pytest.raises(AgentError):
            agents_svc.month_bounds(bad)


def test_summarize_runs_empty_is_none_not_zero():
    summary = agents_svc.summarize_runs([])
    assert summary["runs"] == 0
    assert summary["success_rate"] is None
    summary = agents_svc.summarize_runs([
        {"success": True, "cost_brl": 1.5},
        {"success": False, "cost_brl": 0.5},
        {"success": None, "cost_brl": 0},
    ])
    assert summary["runs"] == 3
    assert summary["success_rate"] == pytest.approx(1 / 3)
    assert summary["cost_brl"] == 2.0


def test_current_month_format():
    now = dt.datetime(2026, 10, 5, tzinfo=dt.UTC)
    assert agents_svc.current_month(now) == "2026-10"
