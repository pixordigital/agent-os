"""The test that protects money.

Prompt caching only pays off when the prefix is byte-identical between calls. When it drifts,
nothing breaks: no exception, no log line, just a bill. So the invariant gets a test, and the
same invariants are asserted at runtime in app/llm/prompt_prefix.py.
"""

from __future__ import annotations

import pytest

from app.llm.prompt_prefix import (
    NonCacheablePrefix,
    build_block0,
    build_prefix,
    canonical_json,
)

MISSION = "Responder e operar o time de comercial."
SOP = "Consulte a fonte antes de afirmar qualquer valor."
TOOLS = [
    {"name": "search_crm", "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}}},
    {"name": "create_task", "input_schema": {"type": "object", "properties": {"t": {"type": "string"}}}},
]
FEW_SHOTS = [
    {"input": "quero desconto", "output": {"tool": "search_crm"}},
    {"input": "cadastra followup", "output": {"tool": "create_task"}},
]
POLICIES = [
    {"action": "send_external_message", "decision": "REQUIRE_HITL", "version": 1},
    {"action": "internal_post", "decision": "ALLOW", "version": 1},
]
OUTPUT_SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}


def _kwargs(**overrides: object) -> dict:
    base = {
        "role_key": "crm_qualificador",
        "mission": MISSION,
        "sop": SOP,
        "policies": POLICIES,
        "tools": TOOLS,
        "few_shots": FEW_SHOTS,
        "output_schema": OUTPUT_SCHEMA,
    }
    base.update(overrides)
    return base


def test_prefix_is_byte_identical_across_builds():
    """The whole caching strategy rests on this. Two builds, same inputs, same bytes."""
    a = build_block0(**_kwargs())
    b = build_block0(**_kwargs())
    assert a == b
    assert a.encode() == b.encode()


def test_prefix_ignores_input_ordering():
    """Equal collections in a different order must produce equal bytes.

    `json.dumps` preserves insertion order, so an unsorted dict silently breaks the cache while
    looking completely correct in a test that compares values instead of bytes.
    """
    forward = build_block0(**_kwargs())
    reversed_order = build_block0(
        **_kwargs(tools=list(reversed(TOOLS)), policies=list(reversed(POLICIES)))
    )
    assert forward == reversed_order


def test_canonical_json_sorts_keys():
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


def test_prefix_rejects_uuid():
    """A uuid in the prefix is the classic silent cache killer: new value every call."""
    with pytest.raises(NonCacheablePrefix, match="uuid"):
        build_block0(**_kwargs(mission=f"{MISSION} run 3f2504e0-4f89-11d3-9a0c-0305e82c3301"))


def test_prefix_rejects_timestamp():
    with pytest.raises(NonCacheablePrefix, match="iso_timestamp|date|epoch"):
        build_block0(**_kwargs(mission=f"{MISSION} em 2026-09-27T22:00:00"))


def test_prefix_rejects_todays_date():
    with pytest.raises(NonCacheablePrefix, match="date"):
        build_block0(**_kwargs(sop=f"{SOP} vigente em 2026-09-27"))


def test_daily_block_does_not_invalidate_system_block():
    """A new day's plan must not change BLOCK 0.

    This is why the daily state lives in its own block: appending it to BLOCK 0 would discard
    the expensive system prefix every single morning.
    """
    monday = build_prefix(**_kwargs(), plan_day="## plan\nsegunda", backlog_state={"open": 3})
    tuesday = build_prefix(**_kwargs(), plan_day="## plan\nterca", backlog_state={"open": 5})
    assert monday.block0 == tuesday.block0
    assert monday.block1 != tuesday.block1


def test_only_system_block_is_marked_cacheable():
    """BLOCK 1 changes daily, so marking it would spend a cache write for a mostly-missed read."""
    prefix = build_prefix(**_kwargs(), plan_day="## plan\nsegunda", backlog_state={"open": 3})
    blocks = prefix.as_blocks()
    assert blocks[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert "cache_control" not in blocks[1]
