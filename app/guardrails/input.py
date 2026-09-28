"""External input normalisation: the boundary between the world and the prompt.

Scope: take untrusted text (Slack message, webhook payload, file content) and return a bounded,
labelled structure the orchestrator can safely place in BLOCK 2 (volatile) of the request.
It NEVER touches BLOCK 0/1 — external text in the cached prefix would both poison the cache
and promote untrusted content to instruction level.

Owner: platform. Pure functions, no I/O, tested per branch.

Threat model (from the prompt-injection-test skill's surface mapping):
- Direct surface: chat text the human typed. Attacker = the human (or whoever holds their
  keyboard/Slack session). Treated as data, never as instruction.
- Indirect surface (Wave 2+): tool outputs, fetched pages, file contents. Same treatment,
  plus origin tracking so the judge knows what came from where.
"""

from __future__ import annotations

from dataclasses import dataclass

# Hard cap on a single external input. Above this the worker truncates with an explicit marker
# instead of silently dropping tail content (silent truncation is how the important part of a
# message — usually at the end — disappears without anyone noticing).
MAX_INPUT_CHARS = 4000
TRUNCATION_MARKER = "\n\n[…mensagem cortada em {limit} caracteres pelo limite de entrada…]"


@dataclass(frozen=True)
class NormalisedInput:
    """External text, tamed. `source` travels with the task payload so every downstream layer
    (orchestrator, worker, judge) knows this came from outside the system."""

    text: str
    source: str  # 'slack' | 'webhook' | 'api' | ...
    truncated: bool
    channel_ref: str = ""  # e.g. slack channel id / thread ts — routing, not content


def normalise(text: str | None, *, source: str, channel_ref: str = "") -> NormalisedInput:
    """Strip control characters, enforce the cap, label the source. Never raises: garbage in
    produces an empty-but-labelled input, because a crash here would turn malformed Slack
    events into a denial of service on the intake path."""
    cleaned = "".join(
        ch for ch in (text or "") if ch == "\n" or ch == "\t" or not _is_control(ch)
    ).strip()
    if len(cleaned) > MAX_INPUT_CHARS:
        return NormalisedInput(
            text=cleaned[:MAX_INPUT_CHARS] + TRUNCATION_MARKER.format(limit=MAX_INPUT_CHARS),
            source=source,
            truncated=True,
            channel_ref=channel_ref,
        )
    return NormalisedInput(text=cleaned, source=source, truncated=False, channel_ref=channel_ref)


def _is_control(ch: str) -> bool:
    return ord(ch) < 32 or ord(ch) == 127
