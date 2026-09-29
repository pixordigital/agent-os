"""Channel tests: owner command parser, Evolution normalise, intake handler.

Parser and normalise are pure (no stubs needed). Intake touches agents_svc, so its
functions are monkeypatched with fakes — asserting routing decisions and replies,
not the database.
"""

from __future__ import annotations

import pytest

from app import agents as agents_svc
from app.agents import AgentError
from app.channels import commands, evolution
from app.channels import intake as intake_mod

TEAMS = [
    {"id": "t-dev", "key": "dev", "name": "Dev"},
    {"id": "t-com", "key": "comercial", "name": "Comercial"},
]
AGENTS = [
    {"id": "a-1", "key": "sdr", "name": "SDR", "team_id": "t-com", "status": "active"},
]


def parse(text):
    return commands.parse_command(text=text, teams=TEAMS, agents=AGENTS)


def op(text):
    result = parse(text)
    assert result is not None
    return result


def test_help_and_status():
    assert op("ajuda").action == "help"
    assert op("status do time?").action == "status"


def test_create_agent_with_team():
    got = op("criar agente closer no time comercial")
    assert got.action == "create_agent"
    assert got.params["team_id"] == "t-com"
    assert got.params["name"]


def test_create_agent_needs_name():
    assert op("criar agente no time dev").action == "need_clarification"


def test_create_team():
    got = op("criar time Suporte")
    assert got.action == "create_team"
    assert got.params["name"] == "Suporte"


def test_pause_resolves_agent():
    got = op("pausar o sdr")
    assert got.action == "pause" and got.params["agent_id"] == "a-1"
    assert op("pausar o fantasma").action == "need_clarification"


def test_delete_and_resume():
    assert op("excluir sdr").params["agent_id"] == "a-1"
    assert op("ativar SDR").action == "resume"


def test_meta_value_and_team():
    got = op("meta 50.000 comercial")
    assert got.action == "set_goal"
    assert got.params["target_brl"] == 50000.0
    assert got.params["team_id"] == "t-com"
    assert op("meta pra ontem").action == "need_clarification"


def test_plain_work_is_not_a_command():
    assert parse("revisar o pipeline de vendas do comercial") is None
    assert parse("bom dia") is None


def evo_msg(**over):
    base = {"event": "messages.upsert",
            "data": {"messages": [{
                "key": {"remoteJid": "5511999990001@s.whatsapp.net",
                        "fromMe": False, "id": "ABC123"},
                "pushName": "Chefe",
                "message": {"conversation": "status do time?"},
                "messageTimestamp": "1700000000"}]}}
    base["data"]["messages"][0].update(over)
    return base


def test_evolution_valid_message():
    out = evolution.normalise_event(evo_msg())
    assert out is not None
    assert out["source"] == "whatsapp"
    assert out["channel_ref"] == "5511999990001"
    assert out["event_id"] == "wa-ABC123"
    assert out["text"] == "status do time?"


def test_evolution_extended_text():
    msg = evo_msg()
    msg["data"]["messages"][0]["message"] = {
        "extendedTextMessage": {"text": "  pausar sdr  "}}
    out = evolution.normalise_event(msg)
    assert out is not None
    assert out["text"] == "pausar sdr"


def test_evolution_skips_noise():
    assert evolution.normalise_event({"event": "qrcode.updated"}) is None
    assert evolution.normalise_event({"event": "messages.upsert", "data": {}}) is None
    own = evo_msg()
    own["data"]["messages"][0]["key"]["fromMe"] = True
    assert evolution.normalise_event(own) is None
    group = evo_msg()
    group["data"]["messages"][0]["key"]["remoteJid"] = "123@g.us"
    assert evolution.normalise_event(group) is None
    broadcast = evo_msg()
    broadcast["data"]["messages"][0]["key"]["remoteJid"] = "status@broadcast"
    assert evolution.normalise_event(broadcast) is None
    empty = evo_msg()
    empty["data"]["messages"][0]["message"] = {"conversation": "   "}
    assert evolution.normalise_event(empty) is None


def test_evolution_requires_settings():
    with pytest.raises(evolution.EvolutionNotConfigured):
        evolution.require_settings({"base_url": "", "api_key": "k", "instance": "i"})


async def _run_intake(monkeypatch, event, **stubs):
    for name, fn in stubs.items():
        monkeypatch.setattr(agents_svc, name, fn)
    replies = []
    answer = await intake_mod.handle_event(
        event, org_id="org-1", teams=TEAMS, agents=AGENTS,
        reply=lambda t: replies.append(t) or _noop(), enqueue=None)
    assert answer is not None
    return answer, replies


async def _noop():
    return None


def _event(text, **over):
    base = {"event_id": "e1", "type": "message", "text": text, "user_id": "u",
            "channel_id": "c", "source": "slack", "channel_ref": "c"}
    base.update(over)
    return base


async def test_intake_status_command(monkeypatch):
    async def list_agents(org_id):
        return AGENTS
    async def list_teams(org_id):
        return TEAMS
    answer, replies = await _run_intake(
        monkeypatch, _event("status"), list_agents=list_agents, list_teams=list_teams)
    assert "1 agente" in answer and replies == [answer]


async def test_intake_unknown_agent_clarifies(monkeypatch):
    answer, _ = await _run_intake(monkeypatch, _event("pausar o fantasma"))
    assert "Não encontrei" in answer


async def test_intake_routes_work_to_task(monkeypatch):
    async def create_task(org_id, **kw):
        assert kw["idempotency_key"] == "e1"
        return {"id": "task-12345678"}
    answer, _ = await _run_intake(
        monkeypatch, _event("revisar o contrato do comercial"), create_task=create_task)
    assert "task-123" in answer and "sem worker" in answer


async def test_intake_unrouted_answers(monkeypatch):
    answer, _ = await _run_intake(monkeypatch, _event("bom dia"))
    assert "não roteei" in answer


async def test_intake_agent_error_becomes_reply(monkeypatch):
    async def boom(org_id):
        raise AgentError("Quebrou de propósito.")
    answer, replies = await _run_intake(
        monkeypatch, _event("status"), list_agents=boom)
    assert answer == "Quebrou de propósito." and replies == [answer]
