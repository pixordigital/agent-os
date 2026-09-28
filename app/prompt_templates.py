"""Prompt template gallery: predefined, editable starting points for agent prompts.

Scope: read the versioned seed file and serve it. It does NOT store per-org custom templates
(Wave 5+, when agent specialisation needs them) and it does NOT write anything — editing
happens in the agent's own textarea, which is the editable part the owner asked for.

Owner: platform. Source of truth: supabase/seeds/prompt_templates.json, versioned in git.
"""

from __future__ import annotations

import json
import pathlib
from functools import lru_cache
from typing import Any

SEED_PATH = pathlib.Path(__file__).resolve().parents[1] / "supabase" / "seeds" / "prompt_templates.json"


@lru_cache
def load_templates() -> dict[str, Any]:
    """Load once per process. A file change needs a restart — acceptable for seed content that
    changes on deploy cadence, not at runtime. (Custom per-org templates would need a table;
    that is a deliberate later step, not an oversight.)"""
    return json.loads(SEED_PATH.read_text(encoding="utf-8"))


def list_templates() -> dict[str, Any]:
    """Gallery metadata + kind defaults. Content excluded: the form fetches one template at a
    time so the page does not ship every template on every load."""
    data = load_templates()
    return {
        "version": data["version"],
        "source": data["source"],
        "kind_defaults": data["kind_defaults"],
        "templates": [
            {k: t[k] for k in ("key", "kind", "name", "description")}
            for t in data["templates"]
        ],
    }


def get_template(key: str) -> dict[str, Any] | None:
    data = load_templates()
    for template in data["templates"]:
        if template["key"] == key:
            return template
    return None
