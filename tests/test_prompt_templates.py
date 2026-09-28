"""Prompt template gallery invariants.

The gallery is what prefills an agent's system prompt, so a broken entry ships broken agents.
These checks run against the seed file, not the database — the gallery is versioned content,
and content deserves the same cheap guards as schema.
"""

from __future__ import annotations

import json
import pathlib

SEED = (
    pathlib.Path(__file__).resolve().parents[1] / "supabase" / "seeds" / "prompt_templates.json"
)
DATA = json.loads(SEED.read_text(encoding="utf-8"))

VALID_KINDS = {"orchestrator", "manager", "worker", "reviewer"}


def test_seed_is_valid_with_version_and_source():
    """Versioned and sourced: an unversioned seed cannot be reasoned about, and an unsourced
    one cannot be audited back to the AIOS original it was adapted from."""
    assert isinstance(DATA["version"], int) and DATA["version"] >= 1
    assert "aios" in DATA["source"].lower()
    assert isinstance(DATA["templates"], list) and len(DATA["templates"]) >= 1


def test_template_keys_are_unique():
    keys = [t["key"] for t in DATA["templates"]]
    assert len(keys) == len(set(keys)), "duplicate template key"


def test_every_template_has_required_fields():
    for template in DATA["templates"]:
        for field in ("key", "kind", "name", "description", "content"):
            assert template.get(field), f"{template.get('key')}: missing {field}"


def test_template_kinds_are_valid_org_roles():
    """A template for a kind that does not exist in agents.kind can never be auto-loaded —
    it would sit in the gallery unreachable."""
    for template in DATA["templates"]:
        assert template["kind"] in VALID_KINDS, template["key"]


def test_kind_defaults_cover_every_kind_and_resolve():
    """Every org role gets a prefill on kind select; every default must point at a real key.
    A dangling default fails silently in the browser (fetch 404, empty textarea, no error)."""
    keys = {t["key"] for t in DATA["templates"]}
    assert set(DATA["kind_defaults"]) == VALID_KINDS
    for kind, key in DATA["kind_defaults"].items():
        assert key in keys, f"kind default {kind} -> missing template {key}"


def test_every_template_starts_with_role_tag():
    """The TAG convention (#ROLE first) is what makes prompts cacheable as a stable prefix
    and reviewable at a glance. A template without it is a blob, not a starting point."""
    for template in DATA["templates"]:
        assert template["content"].lstrip().startswith("#ROLE"), template["key"]


def test_templates_reference_no_missing_systems():
    """Ported from AIOS: ARVO ledger, HubSpot/Pipedrive and PendingAction do not exist here.
    A template that tells the agent to call them teaches hallucination. Placeholders in
    [BRACKETS] are the honest form — the owner fills what exists."""
    forbidden = ("ARVO", "HubSpot", "Pipedrive", "PendingAction", "sql_query", "rag_search",
                 "python_sandbox", "storage_save")
    for template in DATA["templates"]:
        for token in forbidden:
            assert token not in template["content"], f"{template['key']}: references {token}"


def test_loader_serves_gallery_and_single_template():
    from app.prompt_templates import get_template, list_templates

    gallery = list_templates()
    assert gallery["version"] == DATA["version"]
    assert len(gallery["templates"]) == len(DATA["templates"])
    # Gallery metadata carries no content: the form fetches one at a time.
    assert all("content" not in t for t in gallery["templates"])

    sdr = get_template("sdr")
    assert sdr is not None and "BANT" in sdr["content"]
    assert get_template("no-such-template") is None
