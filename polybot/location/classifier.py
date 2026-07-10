from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Protocol

import requests

from .config import ClassifierConfig, LocationBotConfig
from .types import Article, LocationSignal


def _bool_field() -> dict[str, str]:
    return {"type": "boolean"}


_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source_is_trusted": _bool_field(),
        "qualifies_as_senior_round": _bool_field(),
        "round_status": {"type": "string", "enum": ["none", "rumor", "scheduled", "underway", "concluded", "technical_only", "unclear"]},
        "location_country_name": {"type": "string"},
        "confirmed_location": {"type": "string"},
        "evidence_strength": {"type": "string", "enum": ["confirmed_started", "confirmed_scheduled", "reported_indirect", "speculative", "denied"]},
        "would_resolve_held_location_yes": _bool_field(),
        "would_resolve_held_location_no": _bool_field(),
        "level": {"type": "string", "enum": ["0", "1", "2", "3", "4A", "4B"]},
        "quote_supporting_trigger": {"type": "string"},
        "source_tier": {"type": "string", "enum": ["wire", "mediator_government", "official_government", "state_media", "other"]},
        "headline_location": {"type": "string"},
        "technical_location": {"type": "string"},
        "future_expected_formal_location": {"type": "string"},
        "final_decision_announced": _bool_field(),
    },
    "required": [
        "source_is_trusted",
        "qualifies_as_senior_round",
        "round_status",
        "location_country_name",
        "confirmed_location",
        "evidence_strength",
        "would_resolve_held_location_yes",
        "would_resolve_held_location_no",
        "level",
        "quote_supporting_trigger",
        "source_tier",
        "headline_location",
        "technical_location",
        "future_expected_formal_location",
        "final_decision_announced",
    ],
    "additionalProperties": False,
}


class LocationClassifierProtocol(Protocol):
    # held_location is the LIVE holding: None means "use the config default",
    # empty string means explicitly flat (entry mode, or after an exit).
    def classify(self, article: Article, market_rule_text: str, held_location: str | None = None) -> LocationSignal:
        ...


class RuleBasedFixtureLocationClassifier:
    """Deterministic classifier for dry-runs and tests; production uses LLMLocationClassifier."""

    def __init__(self, config: LocationBotConfig):
        self.config = config

    def classify(self, article: Article, market_rule_text: str, held_location: str | None = None) -> LocationSignal:
        text = f"{article.title}\n{article.raw_text}".lower()
        held = self.config.event.held_location if held_location is None else held_location
        tracked_names = {o.name: o.label.lower() for o in self.config.outcomes}
        technical = any(
            term in text
            for term in (
                "technical talks",
                "technical negotiations",
                "technical meeting",
                "working group",
                "staff-level",
                "staff level",
                "implementation",
                "deconfliction",
                "monitoring",
            )
        )
        no_meeting = any(term in text for term in ("no meeting", "talks collapsed", "talks suspended indefinitely", "negotiations called off"))
        final_decision_announced = not any(
            term in text
            for term in (
                "final decision has not been announced",
                "final decision has yet to be announced",
                "final decision is yet to be announced",
                "venue has not been announced",
                "venue has yet to be announced",
                "not final",
            )
        )

        headline_location = _first_location(article.title.lower(), tracked_names)
        technical_location = "none"
        future_expected_formal_location = "none"
        confirmed = "none"

        # Technical/preparatory venue hints must not be promoted into
        # confirmed_location. This is the Dawn failure mode: the headline can
        # say Islamabad is frontrunner while the body reveals the July 11 item
        # is only technical talks.
        if technical:
            technical_location = _technical_location(text, tracked_names)

        future_expected_formal_location = _expected_formal_location(text, tracked_names)

        if no_meeting:
            confirmed = "no_meeting"
        elif not technical:
            for name, label in tracked_names.items():
                if label in text and any(
                    term in text
                    for term in (
                        "will meet in",
                        "to meet in",
                        "begin in",
                        "begins in",
                        "began in",
                        "scheduled in",
                        "underway in",
                        "will be held in",
                        "round will be held in",
                        "round begins in",
                    )
                ):
                    confirmed = name
                    break

        strength = "confirmed_scheduled" if confirmed not in {"none", "no_meeting"} else ("denied" if confirmed == "no_meeting" else "speculative")
        status = "scheduled" if confirmed not in {"none", "no_meeting"} else ("technical_only" if technical else ("none" if no_meeting else "unclear"))
        trusted = article.domain in {"reuters.com", "apnews.com", "afp.com", "aljazeera.com", "dawn.com"}
        would_yes = bool(held) and confirmed == held
        would_no = bool(held) and confirmed not in {"none", held}
        level = "4A" if confirmed != "none" and trusted else "1"
        quote = article.raw_text.split(".")[0].strip() if article.raw_text else ""
        return LocationSignal(
            source_is_trusted=trusted,
            qualifies_as_senior_round=confirmed not in {"none", "no_meeting"} and not technical,
            round_status=status,  # type: ignore[arg-type]
            location_country_name=confirmed if confirmed != "none" else (technical_location if technical_location != "none" else future_expected_formal_location),
            confirmed_location=confirmed,
            evidence_strength=strength,  # type: ignore[arg-type]
            would_resolve_held_location_yes=would_yes,
            would_resolve_held_location_no=would_no,
            level=level,  # type: ignore[arg-type]
            quote_supporting_trigger=quote,
            source_tier="wire" if article.domain in {"reuters.com", "apnews.com", "afp.com"} else "other",
            headline_location=headline_location,
            technical_location=technical_location,
            future_expected_formal_location=future_expected_formal_location,
            final_decision_announced=final_decision_announced,
        )


class LLMLocationClassifier:
    def __init__(
        self,
        config: ClassifierConfig,
        bot_config: LocationBotConfig,
        anthropic_client: object | None = None,
        cli_runner: Callable[[str], str] | None = None,
    ):
        self.config = config
        self.bot_config = bot_config
        self._anthropic_client = anthropic_client
        self._cli_runner = cli_runner
        self.last_usage: dict[str, Any] | None = None

    def classify(self, article: Article, market_rule_text: str, held_location: str | None = None) -> LocationSignal:
        self.last_usage = None
        provider = self.config.provider.lower()
        prompt = _prompt(article, market_rule_text, self.bot_config, held_location=held_location)
        if provider == "openai":
            raw = self._openai(prompt)
        elif provider == "anthropic":
            raw = self._anthropic(prompt)
        elif provider in {"codex_cli", "codex-cli", "codex"}:
            raw = self._cli_with_anthropic_fallback(lambda: self._codex_cli(prompt), "codex CLI", prompt)
        elif provider in {"claude_cli", "claude-cli", "claude_code_cli"}:
            raw = self._cli_with_anthropic_fallback(lambda: self._claude_cli(prompt), "claude CLI", prompt)
        else:
            raise RuntimeError(f"unsupported classifier provider: {self.config.provider}")
        return LocationSignal.from_dict(_json_object(raw))

    def _openai(self, prompt: str) -> str:
        key = os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY or LLM_API_KEY is required")
        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": self.config.model, "temperature": self.config.temperature, "input": prompt},
            timeout=45,
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and isinstance(data.get("output_text"), str):
            return data["output_text"]
        chunks: list[str] = []
        for item in data.get("output", []) if isinstance(data, dict) else []:
            for content in item.get("content", []) if isinstance(item, dict) else []:
                if isinstance(content, dict) and isinstance(content.get("text"), str):
                    chunks.append(content["text"])
        return "\n".join(chunks)

    def _anthropic(self, prompt: str, *, model: str | None = None) -> str:
        response = self._anthropic_client_or_build().messages.create(
            model=model or self.config.model,
            max_tokens=8192,
            thinking={"type": "adaptive"},
            cache_control={"type": "ephemeral"},
            output_config={"format": {"type": "json_schema", "schema": _OUTPUT_SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
        self.last_usage = _usage_dict(getattr(response, "usage", None))
        if response.stop_reason == "refusal":
            raise RuntimeError("anthropic classifier refused the request; no trade")
        text = "".join(block.text for block in response.content if block.type == "text")
        if not text.strip():
            raise RuntimeError(f"anthropic classifier returned no text (stop_reason={response.stop_reason})")
        return text

    def _cli_with_anthropic_fallback(self, runner: Callable[[], str], label: str, prompt: str) -> str:
        try:
            return runner()
        except Exception as exc:
            if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("LLM_API_KEY")):
                raise
            fallback_model = os.getenv("ANTHROPIC_CLASSIFIER_FALLBACK_MODEL") or "claude-sonnet-4-6"
            raw = self._anthropic(prompt, model=fallback_model)
            usage = dict(self.last_usage or {})
            usage["fallback_from"] = label
            usage["fallback_error"] = str(exc)[:500]
            usage["fallback_model"] = fallback_model
            self.last_usage = usage
            return raw

    def _anthropic_client_or_build(self) -> Any:
        if self._anthropic_client is None:
            import anthropic

            key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("LLM_API_KEY")
            if not key:
                raise RuntimeError("ANTHROPIC_API_KEY or LLM_API_KEY is required")
            self._anthropic_client = anthropic.Anthropic(api_key=key, timeout=60.0, max_retries=2)
        return self._anthropic_client

    def _claude_cli(self, prompt: str) -> str:
        # See polybot/iran/classifier.py's _claude_cli for the full rationale:
        # --safe-mode keeps subscription (OAuth) auth while skipping CLAUDE.md/
        # skills/plugins/MCP/auto-memory context, --model pins cost, --tools ""
        # disables tool-use overhead, and ANTHROPIC_API_KEY/LLM_API_KEY are
        # stripped from the subprocess env so it can't silently bill metered.
        stdout = self._cli_runner(prompt) if self._cli_runner is not None else self._run_claude_cli(prompt)
        return self._extract_claude_cli_result_text(stdout)

    def _codex_cli(self, prompt: str) -> str:
        stdout = self._cli_runner(prompt) if self._cli_runner is not None else self._run_codex_cli(prompt)
        return self._extract_codex_cli_result_text(stdout)

    def _run_codex_cli(self, prompt: str) -> str:
        binary = _cli_binary(self.config.cli_binary, provider_default="codex")
        timeout = self.config.cli_timeout_seconds
        with tempfile.TemporaryDirectory(prefix="location-codex-classifier-") as tmp:
            schema_path = Path(tmp) / "schema.json"
            output_path = Path(tmp) / "last-message.json"
            schema_path.write_text(json.dumps(_OUTPUT_SCHEMA), encoding="utf-8")
            try:
                completed = subprocess.run(
                    [
                        binary,
                        "exec",
                        "--ephemeral",
                        "--ignore-rules",
                        "--skip-git-repo-check",
                        "--sandbox",
                        "read-only",
                        # NOTE: --ask-for-approval is not a valid flag under `codex
                        # exec` (only under the top-level `codex` command) as of
                        # codex-cli 0.142.5 -- it errors with "unexpected argument"
                        # and previously made every real call fail over to the
                        # Anthropic API fallback. Non-interactive approval is
                        # instead controlled by the project-local
                        # .codex/config.toml's approval_policy = "never", which
                        # `codex exec` does read.
                        "--model",
                        self.config.model,
                        "--output-schema",
                        str(schema_path),
                        "--output-last-message",
                        str(output_path),
                        "-",
                    ],
                    input=prompt,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
            except FileNotFoundError as exc:
                raise RuntimeError(f"codex CLI binary {binary!r} not found; install Codex CLI and run `codex login`") from exc
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"codex CLI timed out after {timeout}s") from exc
            if completed.returncode != 0:
                raise RuntimeError(f"codex CLI exited {completed.returncode}: {completed.stderr.strip()[:500]}")
            if output_path.exists():
                return output_path.read_text(encoding="utf-8")
            return completed.stdout

    def _run_claude_cli(self, prompt: str) -> str:
        binary = _cli_binary(self.config.cli_binary, provider_default="claude")
        timeout = self.config.cli_timeout_seconds
        env = {key: value for key, value in os.environ.items() if key not in {"ANTHROPIC_API_KEY", "LLM_API_KEY"}}
        try:
            completed = subprocess.run(
                [
                    binary,
                    "-p",
                    "--safe-mode",
                    "--model",
                    self.config.model,
                    "--tools",
                    "",
                    "--no-session-persistence",
                    "--max-budget-usd",
                    "0.50",
                    "--output-format",
                    "json",
                    "--json-schema",
                    json.dumps(_OUTPUT_SCHEMA),
                    "--dangerously-skip-permissions",
                ],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"claude CLI binary {binary!r} not found; install it "
                "(npm install -g @anthropic-ai/claude-code) and run `claude login`"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"claude CLI timed out after {timeout}s") from exc
        if completed.returncode != 0:
            raise RuntimeError(f"claude CLI exited {completed.returncode}: {completed.stderr.strip()[:500]}")
        return completed.stdout

    def _extract_codex_cli_result_text(self, stdout: str) -> str:
        text = stdout.strip()
        if not text:
            raise RuntimeError("codex CLI returned no result text")
        try:
            parsed: Any = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(parsed, dict):
            self.last_usage = {"provider": "codex_cli"}
            return json.dumps(parsed)
        raise RuntimeError(f"unexpected codex CLI output shape: {stdout[:300]!r}")

    def _extract_claude_cli_result_text(self, stdout: str) -> str:
        try:
            wrapper: Any = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"claude CLI did not return a JSON envelope: {stdout[:300]!r}") from exc
        if isinstance(wrapper, list):
            result_events = [item for item in wrapper if isinstance(item, dict) and item.get("type") == "result"]
            wrapper = result_events[-1] if result_events else (wrapper[-1] if wrapper else {})
        if not isinstance(wrapper, dict):
            raise RuntimeError(f"unexpected claude CLI output shape: {stdout[:300]!r}")
        if wrapper.get("is_error"):
            raise RuntimeError(f"claude CLI reported an error: {stdout[:500]!r}")
        usage_fields = {key: wrapper[key] for key in ("total_cost_usd", "num_turns", "duration_ms", "usage") if key in wrapper}
        self.last_usage = usage_fields or None
        structured = wrapper.get("structured_output")
        if isinstance(structured, dict):
            return json.dumps(structured)
        result_text = wrapper.get("result")
        if not isinstance(result_text, str) or not result_text.strip():
            raise RuntimeError(f"claude CLI returned no result text: {stdout[:300]!r}")
        return result_text


def _first_location(text: str, tracked_names: dict[str, str]) -> str:
    for name, label in tracked_names.items():
        aliases = {label, name.replace("_", " ")}
        if name == "pakistan":
            aliases.add("islamabad")
        elif name == "qatar":
            aliases.add("doha")
        elif name == "switzerland":
            aliases.update({"geneva", "burgenstock", "bürgenstock"})
        elif name == "oman":
            aliases.add("muscat")
        if any(alias and alias in text for alias in aliases):
            return name
    return "none"


def _technical_location(text: str, tracked_names: dict[str, str]) -> str:
    technical_terms = (
        "technical talks",
        "technical negotiations",
        "technical meeting",
        "working group",
        "staff-level",
        "staff level",
        "implementation",
        "deconfliction",
        "monitoring",
    )
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
    for sentence in sentences:
        if not any(term in sentence for term in technical_terms):
            continue
        location = _first_location(sentence, tracked_names)
        if location != "none":
            return location
    return "none"


def _expected_formal_location(text: str, tracked_names: dict[str, str]) -> str:
    formal_terms = ("high-level", "high level", "senior-level", "senior level", "formal", "direct talks", "next round")
    expectation_terms = ("expected", "slated", "set to", "scheduled", "will take place", "will be held", "to be held")
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
    for sentence in sentences:
        if not any(term in sentence for term in formal_terms):
            continue
        if not any(term in sentence for term in expectation_terms):
            continue
        location = _first_location(sentence, tracked_names)
        if location != "none":
            return location
    return "none"


def build_location_classifier(config: LocationBotConfig) -> LocationClassifierProtocol:
    if config.classifier.provider == "rule_based":
        return RuleBasedFixtureLocationClassifier(config)
    return LLMLocationClassifier(config.classifier, config)


def run_location_classifier_passes(
    classifier: LocationClassifierProtocol, article: Article, market_rule_text: str, passes: int
) -> list[LocationSignal]:
    """Run the classifier `passes` times (mirrors polybot.iran.classifier's
    run_classifier_passes). A single-element list is the normal case; callers
    that set classifier.require_pass_agreement request >1 to gate live trades
    on multi-pass agreement (see polybot.location.decision.classify_agreement)."""
    return [classifier.classify(article, market_rule_text) for _ in range(max(1, passes))]


def _usage_dict(usage: Any) -> dict[str, Any] | None:
    if usage is None:
        return None
    if isinstance(usage, dict):
        return dict(usage)
    fields = {}
    for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "server_tool_use", "service_tier"):
        if hasattr(usage, key):
            fields[key] = getattr(usage, key)
    return fields or None


def _json_object(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError(f"classifier did not return JSON object: {raw[:200]!r}")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("classifier JSON must be an object")
    return parsed


def _prompt(article: Article, market_rule_text: str, config: LocationBotConfig, held_location: str | None = None) -> str:
    all_locations = list(config.outcomes)
    all_location_labels = ", ".join(f'"{o.name}" ({o.label})' for o in all_locations)
    rotation_targets = config.rotation_targets()
    rotation_target_labels = ", ".join(f'"{o.name}" ({o.label})' for o in rotation_targets) or "none"
    held_name = config.event.held_location if held_location is None else held_location
    held = config.outcome(held_name) if held_name else None
    entry_target_labels = ", ".join(f'"{o.name}" ({o.label})' for o in config.entry_targets()) or "none"
    schema_hint = {
        "source_is_trusted": True,
        "qualifies_as_senior_round": True,
        "round_status": "none | rumor | scheduled | underway | concluded | technical_only | unclear",
        "location_country_name": "free-text country name as reported, or empty string",
        "confirmed_location": f"one of the configured location keys: {all_location_labels}; or other_specific, no_meeting, none",
        "evidence_strength": "confirmed_started | confirmed_scheduled | reported_indirect | speculative | denied",
        "would_resolve_held_location_yes": True,
        "would_resolve_held_location_no": False,
        "level": "0 | 1 | 2 | 3 | 4A | 4B",
        "quote_supporting_trigger": "exact quote from article",
        "source_tier": "wire | mediator_government | official_government | state_media | other",
        "headline_location": "configured key named/implied by headline only, or other_specific, or none",
        "technical_location": "configured key for technical/preparatory venue, or other_specific, or none",
        "future_expected_formal_location": "configured key for expected future high-level/formal venue, or other_specific, or none",
        "final_decision_announced": True,
    }
    if held is not None:
        position_lines = (
            f"Held position: YES on \"{held.label}\" ({held.name}).\n"
            f"Configured location keys (use these exact keys in confirmed_location when they match, including the held location): {all_location_labels}.\n"
            f"Automatic rotation buy targets only: {rotation_target_labels}. A configured non-held location that is not listed here is sell-only.\n"
            f"If the article confirms the held venue {held.label}, set confirmed_location to \"{held.name}\"; do not use other_specific for the held venue.\n"
        )
    else:
        position_lines = (
            "Held position: NONE -- the bot is currently flat and holds no leg of this market.\n"
            f"Configured location keys (use these exact keys in confirmed_location when they match): {all_location_labels}.\n"
            f"Automatic entry buy targets only: {entry_target_labels}. Confirmation of any other venue is alert-only.\n"
            "With no held position, set would_resolve_held_location_yes and would_resolve_held_location_no to false.\n"
        )
    return (
        "Classify this news article for a categorical Polymarket location-prediction market.\n"
        f"Market question: {config.event.question}\n"
        f"Deadline: {config.event.deadline_date}\n"
        + position_lines +
        "Use \"other_specific\" if a real, different, named country is confirmed that is NOT one of the configured location keys above.\n"
        "Use \"no_meeting\" only if credible reporting indicates no qualifying round will occur by the deadline (this resolves every location NO).\n"
        "Use \"none\" if no location is confirmed or implied at all.\n"
        f"Market resolution rules:\n{market_rule_text}\n"
        + (
            f"Analyst context (the position holder's own background reasoning -- weigh it as prior context, "
            f"not as ground truth; still classify strictly from the article text and market rules above):\n"
            f"{config.event.analyst_context}\n"
            if config.event.analyst_context.strip()
            else ""
        )
        + "Never classify from the headline alone: the body controls the action. Extract headline venue separately in headline_location. "
        "Only a genuine, formal, senior-level, in-person (or indirect in-person via authorized mediators) round counts. "
        "Technical, staff-level, working-group, implementation, monitoring, preparatory, or deconfliction meetings do NOT qualify on their own. "
        "If a venue is tied only to technical/preparatory talks, put it in technical_location and keep confirmed_location as none. "
        "If the body says the final venue/decision has not been announced, set final_decision_announced=false and do not treat a frontrunner/expected venue as confirmed. "
        "If the body separately says high-level/direct/formal talks are expected later in another venue, put that venue in future_expected_formal_location; do not rotate away from the held venue on the technical venue. "
        "Brief greetings, chance encounters, or photo ops do NOT count.\n"
        "A merely 'scheduled' round can still shift location before it begins -- treat 'scheduled' as weaker evidence than 'underway'/'concluded' "
        "unless the source is a wire service or an official government statement giving a specific confirmed venue and date.\n"
        f"Schema: {json.dumps(schema_hint, sort_keys=True)}\n"
        "Return only strict JSON matching this shape, no prose.\n"
        f"Article domain: {article.domain}\nTitle: {article.title}\nArticle text:\n{_bounded_article_text(article.raw_text)}\n"
    )


def _bounded_article_text(text: str, limit: int = 12000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[article text truncated]"


def _cli_binary(configured: str, *, provider_default: str) -> str:
    if provider_default == "codex":
        return os.getenv("CODEX_CLI_BINARY") or ("codex" if configured == "claude" else configured)
    return configured or provider_default
