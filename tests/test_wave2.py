"""Wave 2 unit tests: pure functions only (dispatch, input guard, OpenRouter parsing,
Slack builders, socket normalisation). DB-touching worker logic is verified live against the
container, same as Wave 1 — a mocked pool would test the mock, not the state machine."""

from __future__ import annotations

import httpx
import pytest

from app.channels import slack_api, slack_socket
from app.guardrails.input import MAX_INPUT_CHARS, normalise
from app.llm import openrouter
from app.orchestrator.dispatch import route_event


# ------------------------------------------------------------------ dispatch


def _teams():
    return [
        {"id": "t-dev", "key": "dev", "name": "Dev"},
        {"id": "t-com", "key": "comercial", "name": "Comercial"},
    ]


def _agents():
    return [
        {"id": "a-mgr", "key": "mgr-dev", "kind": "manager", "status": "active",
         "team_id": "t-dev"},
        {"id": "a-wrk", "key": "dev-backend", "kind": "worker", "status": "active",
         "team_id": "t-dev"},
    ]


def test_direct_mention_routes_to_agent_and_team():
    route = route_event(
        teams=_teams(), agents=_agents(), text="@dev-backend revise o PR",
        mentioned_agent_key="dev-backend", source="slack",
    )
    assert route.agent_id == "a-wrk"
    assert route.team_id == "t-dev"
    assert route.task_type == "direct"


def test_unknown_mention_is_explicitly_unrouted():
    route = route_event(
        teams=_teams(), agents=_agents(), text="@fantasma faça isso",
        mentioned_agent_key="fantasma", source="slack",
    )
    assert route.agent_id is None and route.team_id is None
    assert route.task_type == "direct"  # the intent is clear; the target is not


def test_team_channel_owns_its_messages():
    route = route_event(
        teams=_teams(), agents=_agents(), text="alguém pega isso?",
        channel_key="team-comercial", source="slack",
    )
    assert route.team_id == "t-com" and route.agent_id is None


def test_keyword_routes_to_team_and_its_manager():
    route = route_event(
        teams=_teams(), agents=_agents(),
        text="o deploy do comercial quebrou", source="slack",
    )
    assert route.team_id == "t-com"
    assert route.agent_id is None  # no comercial manager exists: manager triages


def test_money_talk_suggests_high_risk():
    route = route_event(
        teams=_teams(), agents=_agents(), text="aprovar desconto de R$ 5000?",
        source="slack",
    )
    assert route.risk == "alto"


def test_routing_is_deterministic():
    from typing import Any

    kwargs: dict[str, Any] = dict(
        teams=_teams(), agents=_agents(), text="qual o status do dev?",
        source="slack",
    )
    assert route_event(**kwargs) == route_event(**kwargs)


# ------------------------------------------------------------------ input guard


def test_external_text_is_labelled_never_instruction():
    out = normalise("faça isso", source="slack", channel_ref="C1")
    assert out.source == "slack" and not out.truncated


def test_long_input_truncates_with_marker_not_silently():
    out = normalise("x" * (MAX_INPUT_CHARS + 100), source="slack")
    assert out.truncated and "cortada" in out.text
    assert len(out.text) > MAX_INPUT_CHARS  # marker is honest overhead


def test_control_characters_stripped_newlines_kept():
    out = normalise("a\x00b\x1fc\nd", source="slack")
    assert out.text == "abc\nd"


def test_garbage_in_never_crashes_intake():
    assert normalise(None, source="slack").text == ""
    assert normalise("   ", source="slack").text == ""


# ------------------------------------------------------------------ openrouter parsing


def _mock_client(payload: dict, status: int = 200) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_openrouter_success_parses_usage_and_cost():
    client = _mock_client({
        "id": "req-1", "model": "m",
        "choices": [{"message": {"content": "  ok  "}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                  "prompt_tokens_details": {"cached_tokens": 60}, "cost": 0.002},
    })
    result = await openrouter.chat(
        api_key="k", model="m", messages=[{"role": "user", "content": "hi"}],
        client=client,
    )
    assert result.ok and result.text == "ok"
    assert (result.tokens_in, result.tokens_out) == (100, 20)
    assert result.cached_tokens == 60
    assert result.cost_usd == 0.002
    assert result.provider_request_id == "req-1"


@pytest.mark.asyncio
async def test_openrouter_missing_cost_stays_none_not_zero():
    """cost_usd=None means 'not reported'. Writing 0.0 would claim a free call."""
    client = _mock_client({
        "id": "req-2", "choices": [{"message": {"content": "hi"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    })
    result = await openrouter.chat(
        api_key="k", model="m", messages=[], client=client)
    assert result.ok and result.cost_usd is None and result.cost_brl == 0.0
    assert result.cached_tokens == 0


@pytest.mark.asyncio
async def test_openrouter_failures_are_typed_for_the_retry_matrix():
    for status, marker in ((429, "rate_limited"), (502, "provider_5xx"),
                           (401, "provider_401")):
        client = _mock_client({"error": "x"}, status=status)
        result = await openrouter.chat(
            api_key="k", model="m", messages=[], client=client)
        assert not result.ok and result.error.startswith(marker), (status, result.error)


@pytest.mark.asyncio
async def test_openrouter_missing_key_never_calls():
    called = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(True)
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await openrouter.chat(
        api_key="", model="m", messages=[], client=client)
    assert not result.ok and called == []


# ------------------------------------------------------------------ slack builders


def test_hitl_card_buttons_carry_the_decision():
    blocks = slack_api.hitl_card(
        title="Aprovar desconto?", summary="Cliente X pede 10%",
        context="task 123 · gerente dev",
        approve_value="appr-1:approved", reject_value="appr-1:rejected",
    )
    actions = next(b for b in blocks if b["type"] == "actions")
    values = [e["value"] for e in actions["elements"]]
    assert values == ["appr-1:approved", "appr-1:rejected"]
    assert len(blocks) <= 50  # Slack hard limit; truncate, never 400


def test_slack_calls_refuse_without_token():
    import asyncio

    async def go() -> None:
        with pytest.raises(slack_api.SlackNotConfigured):
            await slack_api.post_message(token="", channel="#x", text="hi")

    asyncio.run(go())


# ------------------------------------------------------------------ socket normalisation


def test_socket_ignores_own_echoes_and_non_text():
    assert slack_socket.normalise_event({
        "type": "events_api", "envelope_id": "e1",
        "event": {"type": "message", "subtype": "bot_message", "text": "hi",
                  "channel": "C1", "ts": "1"},
    }) is None
    assert slack_socket.normalise_event({
        "type": "events_api", "envelope_id": "e2",
        "event": {"type": "reaction_added"},
    }) is None
    assert slack_socket.normalise_event({"type": "other"}) is None


def test_socket_normalises_human_text_with_routing_refs():
    out = slack_socket.normalise_event({
        "type": "events_api", "envelope_id": "e3",
        "event": {"type": "app_mention", "text": "<@BOT> status?", "user": "U1",
                  "channel": "C1", "ts": "10"},
    })
    assert out is not None
    assert out["event_id"] == "e3"  # feeds idempotency_key downstream
    assert out["channel_id"] == "C1" and out["user_id"] == "U1"


def test_socket_token_validation_checks_prefixes_not_just_presence():
    with pytest.raises(slack_socket.SlackSocketNotConfigured):
        slack_socket.require_tokens(app_token="xoxb-wrong", bot_token="xoxb-ok")
    app, bot = slack_socket.require_tokens(app_token="xapp-ok", bot_token="xoxb-ok")
    assert (app, bot) == ("xapp-ok", "xoxb-ok")
