"""Cacheable prompt prefix construction.

Scope: build BLOCK 0 (immutable) and BLOCK 1 (daily) of an agent request. It does NOT call any
provider — the transport lives in the `native` backend. Owner: platform.

WHY THIS FILE EXISTS

Anthropic-style prompt caching is billed as ~1.25x to write a prefix and ~0.1x to read it back.
A cache hit requires the prefix to be **byte-identical** to the previous call. So the entire cost
of the caching strategy rests on one property: the same logical prompt must serialise to the same
bytes, every time, forever.

That is a real engineering constraint with a real failure mode, and the failure is silent — you
do not get an error, you get a bill. So the invariants live here, in a pure function, with tests:

  1. No volatile content in BLOCK 0 or BLOCK 1: no uuid, no timestamp, no epoch, no today's date.
  2. JSON serialised with sorted keys and fixed separators. Two dicts that are `==` must produce
     identical strings.
  3. Collections with no semantic order (tool schemas) are sorted by a stable key. An unsorted
     `list` built from a `set` or a `dict` lookup is the classic accidental cache killer.
  4. Fixed block order and fixed separator, so the prefix is a pure function of its inputs.

`assert_cacheable` is the guard. It is cheap, it runs on every build, and it turns a silent
money leak into a loud failure at the point of the mistake.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

# A timestamp in the prefix invalidates the cache on the next call, so it must never be there.
# These patterns are checked on the serialised text, not on the inputs, because the goal is to
# catch the value that slipped in via a string somewhere deep in a tool schema.
VOLATILE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("uuid", re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")),
    ("iso_timestamp", re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:")),
    ("epoch", re.compile(r"\b1[6-9]\d{8}\b")),  # 2024-2030 as unix seconds
    ("date", re.compile(r"\b\d{4}-\d{2}-\d{2}\b")),
)


class NonCacheablePrefix(ValueError):
    """Raised when volatile content reaches a cacheable block.

    Named for the consequence, not the cause: the caller should read this as "this prompt will
    not hit cache", because that is the fact that matters.
    """


def canonical_json(value: Any) -> str:
    """Deterministic JSON: sorted keys, no insignificant whitespace.

    `json.dumps` already preserves insertion order, which is exactly the trap — two equal dicts
    built in a different order serialise differently and silently miss cache. Sorting keys is
    what makes "equal value" imply "equal bytes".
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sorted_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort tool schemas by name.

    Tools are a set in meaning and a list in transport. Sorting by `name` makes the byte
    sequence independent of the order they were registered in.
    """
    return sorted(tools, key=lambda t: str(t.get("name", "")))


def assert_cacheable(block: str) -> None:
    """Reject volatile content in a cacheable block.

    Runs on every build rather than only in tests, because the mistake is easy to reintroduce
    and a test only covers the case somebody thought of.
    """
    for name, pattern in VOLATILE_PATTERNS:
        if pattern.search(block):
            raise NonCacheablePrefix(
                f"cacheable block contains {name} ({pattern.pattern!r}); "
                "it will change between calls and the cache will never hit"
            )


@dataclass(frozen=True)
class PromptPrefix:
    """The two cacheable blocks of a request.

    `frozen=True` because a mutable prefix is a prefix someone will edit in place at runtime,
    which is the same bug wearing a different hat.
    """

    block0: str
    block1: str

    def as_blocks(self) -> list[dict[str, Any]]:
        """Anthropic-style content blocks.

        BLOCK 0 carries `cache_control`; BLOCK 1 does not. That asymmetry is deliberate: marking
        the volatile block would attempt to cache something that changes daily, wasting a write
        for a read that mostly misses.
        """
        return [
            {
                "type": "text",
                "text": self.block0,
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            },
            {"type": "text", "text": self.block1},
        ]


def build_block0(
    *,
    role_key: str,
    mission: str,
    sop: str,
    policies: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    few_shots: list[dict[str, Any]],
    output_schema: dict[str, Any],
) -> str:
    """Build the immutable block: everything that only changes when the agent's own definition
    changes.

    Reordering or reformatting any part of this changes every agent's cache simultaneously, so
    the layout is fixed deliberately: a version marker, then identity, then policy, then tools,
    then examples, then the output contract.
    """
    parts = [
        f"# agent:{role_key}",
        "## mission\n" + mission.strip(),
        "## sop\n" + sop.strip(),
        "## policies\n" + canonical_json(sorted(
            policies, key=lambda p: (str(p.get("action", "")), str(p.get("version", "")))
        )),
        "## tools\n" + canonical_json(_sorted_tools(tools)),
        "## examples\n" + canonical_json(few_shots),
        "## output\n" + canonical_json(output_schema),
    ]
    block = "\n\n".join(parts)
    assert_cacheable(block)
    return block


def build_block1(*, plan_day: str, backlog_state: dict[str, Any]) -> str:
    """Build the daily block: changes about once a day, still worth caching.

    Kept in a separate block rather than appended to BLOCK 0 precisely so that a daily change
    invalidates only this. Appending it to BLOCK 0 would throw away the expensive system prefix
    every morning.
    """
    block = "\n\n".join(
        [
            f"## plan\n{plan_day.strip()}",
            "## backlog\n" + canonical_json(backlog_state),
        ]
    )
    assert_cacheable(block)
    return block


def build_prefix(
    *,
    role_key: str,
    mission: str,
    sop: str,
    policies: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    few_shots: list[dict[str, Any]],
    output_schema: dict[str, Any],
    plan_day: str,
    backlog_state: dict[str, Any],
) -> PromptPrefix:
    return PromptPrefix(
        block0=build_block0(
            role_key=role_key,
            mission=mission,
            sop=sop,
            policies=policies,
            tools=tools,
            few_shots=few_shots,
            output_schema=output_schema,
        ),
        block1=build_block1(plan_day=plan_day, backlog_state=backlog_state),
    )
