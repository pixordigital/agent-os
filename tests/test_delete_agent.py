"""Delete-agent checks: missing agent 404s, and the delete is org-scoped.

The org-scoping assertion is the security property: PostgREST deletes by filter, so a
delete without the org predicate would cross tenants. The stub records the exact params.
"""

from __future__ import annotations

import pytest

from app import agents as agents_svc
from app.agents import AgentError
from app.db import db


class _Resp:
    def __init__(self, rows):
        self._rows = rows

    def raise_for_status(self):
        pass

    def json(self):
        return self._rows


class _Svc:
    def __init__(self, rows):
        self._rows = rows
        self.delete_params = None
        self.posts = []

    async def get(self, path, params=None):
        assert path == "/agents"
        return _Resp(self._rows)

    async def delete(self, path, params=None):
        assert path == "/agents"
        self.delete_params = params
        return _Resp([])

    async def post(self, path, json=None, headers=None):
        self.posts.append((path, json))
        return _Resp([{}])


_AGENT = {"id": "a-1", "key": "dev", "name": "Dev", "team_id": "t-1"}


async def test_delete_missing_agent_raises(monkeypatch):
    monkeypatch.setattr(type(db), "svc", _Svc([]))
    with pytest.raises(AgentError):
        await agents_svc.delete_agent("org-1", "ghost")


async def test_delete_is_org_scoped_and_returns_agent(monkeypatch):
    svc = _Svc([_AGENT])
    monkeypatch.setattr(type(db), "svc", svc)
    agent = await agents_svc.delete_agent("org-1", "a-1")
    assert agent == _AGENT
    assert svc.delete_params == {"org_id": "eq.org-1", "id": "eq.a-1"}


async def test_delete_emits_org_scoped_deleted_event(monkeypatch):
    svc = _Svc([_AGENT])
    monkeypatch.setattr(type(db), "svc", svc)
    await agents_svc.delete_agent("org-1", "a-1")
    paths = [p for p, _ in svc.posts]
    assert "/agent_events" in paths
    payload = dict(svc.posts).get("/agent_events", {})
    assert payload.get("kind") == "deleted"
    assert payload.get("agent_id") is None
    assert payload.get("org_id") == "org-1"
