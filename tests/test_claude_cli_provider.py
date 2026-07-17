from __future__ import annotations

import json

import pytest

from test_binary_bot import _config as _binary_config, _verification, article

from polybot.core.claude_cli import extract_claude_cli_result, is_claude_cli_provider


# ---- envelope extraction ----


def _envelope(**overrides) -> str:
    payload = {"type": "result", "is_error": False, "result": '{"ok": true}', "total_cost_usd": 0.01}
    payload.update(overrides)
    return json.dumps(payload)


def test_extract_prefers_structured_output() -> None:
    text, usage = extract_claude_cli_result(_envelope(structured_output={"qualifies_under_rules": True}))
    assert json.loads(text) == {"qualifies_under_rules": True}
    assert usage == {"total_cost_usd": 0.01}


def test_extract_falls_back_to_result_text() -> None:
    text, _usage = extract_claude_cli_result(_envelope())
    assert text == '{"ok": true}'


def test_extract_handles_stream_event_list() -> None:
    stream = json.dumps([{"type": "system"}, {"type": "result", "result": '{"a": 1}'}])
    text, _usage = extract_claude_cli_result(stream)
    assert text == '{"a": 1}'


def test_extract_raises_on_error_envelope() -> None:
    with pytest.raises(RuntimeError, match="reported an error"):
        extract_claude_cli_result(_envelope(is_error=True))


def test_extract_raises_on_junk_and_empty() -> None:
    with pytest.raises(RuntimeError, match="JSON envelope"):
        extract_claude_cli_result("not json at all")
    with pytest.raises(RuntimeError, match="no result text"):
        extract_claude_cli_result(_envelope(result=""))


def test_provider_aliases() -> None:
    assert is_claude_cli_provider("claude_cli")
    assert is_claude_cli_provider("Claude-CLI")
    assert is_claude_cli_provider("claude_code_cli")
    assert not is_claude_cli_provider("anthropic")


# ---- binary classifier via CLI ----


def _cli_signal_json() -> str:
    return json.dumps(
        {
            "source_is_trusted": True,
            "source_tier": "wire",
            "qualifies_under_rules": True,
            "event_status": "scheduled",
            "evidence_strength": "confirmed_scheduled",
            "before_deadline": True,
            "resolves_no": False,
            "level": "4A",
            "quote_supporting_trigger": "The round will begin next week.",
            "final_decision_announced": True,
        }
    )


def test_binary_classifier_claude_cli_provider(monkeypatch) -> None:
    from dataclasses import replace

    from polybot.binary.classifier import LLMBinaryClassifier

    config = _binary_config()
    classifier_config = replace(config.classifier, provider="claude_cli", model="claude-opus-4-8")
    calls: list[str] = []

    def cli_runner(prompt: str) -> str:
        calls.append(prompt)
        return _envelope(structured_output=json.loads(_cli_signal_json()), usage={"input_tokens": 900})

    classifier = LLMBinaryClassifier(classifier_config, config, cli_runner=cli_runner)
    signal = classifier.classify(article("The round will be held next week."), "test rules", held_side="")
    assert signal.qualifies_under_rules is True
    assert signal.level == "4A"
    assert classifier.last_usage and classifier.last_usage.get("usage") == {"input_tokens": 900}
    # The untrusted-article fencing and rules made it into the CLI prompt.
    assert "<<<ARTICLE" in calls[0] and "test rules" in calls[0]


def test_binary_classifier_cli_failure_without_key_fails_closed(monkeypatch) -> None:
    from dataclasses import replace

    from polybot.binary.classifier import LLMBinaryClassifier

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    config = _binary_config()
    classifier_config = replace(config.classifier, provider="claude_cli")

    def broken_runner(prompt: str) -> str:
        raise RuntimeError("claude CLI exited 1: not logged in")

    classifier = LLMBinaryClassifier(classifier_config, config, cli_runner=broken_runner)
    with pytest.raises(RuntimeError, match="not logged in"):
        classifier.classify(article("The round will be held next week."), "test rules", held_side="")


def test_binary_classifier_cli_failure_falls_back_to_api(monkeypatch) -> None:
    from dataclasses import replace

    from polybot.binary.classifier import LLMBinaryClassifier

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    class _Block:
        type = "text"
        text = _cli_signal_json()

    class _Response:
        stop_reason = "end_turn"
        content = [_Block()]
        usage = {"input_tokens": 5}

    class _Messages:
        def create(self, **kwargs):
            _Response.model_used = kwargs.get("model")
            return _Response()

    class _Client:
        messages = _Messages()

    config = _binary_config()
    classifier_config = replace(config.classifier, provider="claude_cli")
    classifier = LLMBinaryClassifier(
        classifier_config, config, anthropic_client=_Client(), cli_runner=lambda prompt: (_ for _ in ()).throw(RuntimeError("cli down"))
    )
    signal = classifier.classify(article("The round will be held next week."), "test rules", held_side="")
    assert signal.qualifies_under_rules is True
    assert classifier.last_usage and classifier.last_usage["fallback_from"] == "claude CLI"


# ---- rule analyzer via CLI ----


def test_rule_analyzer_claude_cli_provider() -> None:
    from test_discovery import _analyzed_context, _binary_event

    from polybot.core.config import ClassifierConfig
    from polybot.discovery.context import LLMRuleAnalyzer

    analysis_json = {
        "counts": ["family:talks"],
        "does_not_count": [],
        "cancellation_behavior": "explicit",
        "ambiguous_terms": [],
        "discretionary": False,
        "parties": ["united_states", "iran"],
        "mediators": [],
        "locations": [],
        "keywords": ["talks"],
        "decisive_sources": ["wire"],
        "rule_clarity": 0.9,
        "evidence_observability": 0.8,
        "resolution_risk": 0.2,
        "automation_suitability": 0.9,
        "summary": "clean rules",
    }
    analyzer = LLMRuleAnalyzer(
        ClassifierConfig(provider="claude_cli", model="claude-opus-4-8"),
        cli_runner=lambda prompt: _envelope(structured_output=analysis_json),
    )
    context = _analyzed_context(_binary_event())
    analysis = analyzer.analyze(context)
    assert analysis.parties == ["united_states", "iran"]
    assert analysis.model == "claude_cli:claude-opus-4-8"


# ---- operator gate: CLI provider needs the binary, not the API key ----


def test_gate_blocks_live_when_cli_missing(monkeypatch, tmp_path) -> None:
    from polybot.core.operator import OperatorGate

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    from dataclasses import replace

    config = _binary_config(data_dir=tmp_path / "data")
    config = replace(config, classifier=replace(config.classifier, provider="claude_cli"))
    config_path = tmp_path / "bot.yaml"
    config_path.write_text("binary-config\n", encoding="utf-8")
    gate = OperatorGate(config_path, config)

    monkeypatch.setattr("polybot.core.claude_cli.claude_cli_available", lambda binary="claude": False)
    status = gate.status(live_requested=True)
    assert "claude_cli_not_installed" in status.blockers
    # The API key is NOT required for the CLI provider.
    assert "anthropic_not_configured" not in status.blockers

    monkeypatch.setattr("polybot.core.claude_cli.claude_cli_available", lambda binary="claude": True)
    status = gate.status(live_requested=True)
    assert "claude_cli_not_installed" not in status.blockers


# ---- emitted configs inherit the pipeline's provider ----


def test_emitted_config_inherits_claude_cli_provider(tmp_path) -> None:
    from test_discovery import _analyzed_context, _binary_event, _graded

    from polybot.binary.config import load_binary_config
    from polybot.discovery.emit import emit_bot_config
    from polybot.discovery.sources import build_source_plan

    context = _graded(_binary_event())
    plan = build_source_plan(context)
    out = tmp_path / "bot.yaml"
    emit_bot_config(context, plan, entry_usd=25.0, out_path=out, classifier_provider="claude_cli")
    loaded = load_binary_config(out)
    assert loaded.classifier.provider == "claude_cli"
    assert loaded.classifier.screen_model  # screen tier still configured
