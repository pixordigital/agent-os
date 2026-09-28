"""Guardrails: deterministic input handling and output gates.

Scope: what can be decided WITHOUT a model. LLM-as-judge lives in verification (Wave 6);
everything here is a pure function with a test. Owner: platform.

Deliberately NOT here: keyword blocklists. They rot, they false-positive on legitimate work
("ignore" appears in normal sentences), and they create the illusion of a defense. The
defenses that actually hold are structural: source tagging, length caps, system-prompt
isolation, and stop conditions enforced by the worker — plus red-teaming per the
prompt-injection-test skill before the claim-gate wave.
"""