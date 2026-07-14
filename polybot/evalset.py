from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from polybot.core.types import Article

# Adversarial/regression eval harness for the classifiers. A case is one
# archived (or hand-written) article plus an EXPECTATION about the decision
# the full pipeline must reach -- "this rumor must not trade", "this denial
# must not exit the position", "this confirmed wire story must enter".
#
# Every case runs against a FRESH isolated bot (state from one case never
# leaks into the next), through the complete pipeline: keyword gate, screen
# tier, confirm passes, quote verification, source policy, execution policy,
# dry-run execution. Exit code 1 on any failure, so a prompt or model change
# that regresses trigger quality fails CI/pre-flight instead of failing live.
#
# Case JSONL schema (one object per line):
#   {
#     "name": "rumor_must_not_trade",
#     "held": "" | "yes" | "no" | "<outcome-name>",   # optional starting position
#     "article": { url, domain, title, published_at, fetched_at, raw_text, hash },
#     "expect_action": "ENTER_YES",        # exact action match, OR
#     "expect_action_in": ["NO_ACTION", "ALERT_ONLY"],  # any-of match, OR
#     "forbid_trade": true                  # decision must not be a trade action
#   }

TRADE_ACTIONS = {"ENTER_YES", "ENTER_NO", "TRIM_HELD", "EXIT_HELD", "TRIM_YES", "EXIT_YES_ONLY", "ROTATE_YES"}


def load_eval_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for index, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        raw = json.loads(line)  # a malformed case file should fail loudly, not silently shrink the suite
        if not isinstance(raw, dict) or not isinstance(raw.get("article"), dict):
            raise ValueError(f"case line {index + 1}: expected an object with an 'article' field")
        if not any(key in raw for key in ("expect_action", "expect_action_in", "forbid_trade")):
            raise ValueError(f"case line {index + 1}: no expectation (expect_action / expect_action_in / forbid_trade)")
        cases.append(raw)
    return cases


def eval_classifier_command(config_path: Path, cases_path: Path) -> int:
    from polybot.replay import _build_replay_bot

    text = config_path.read_text(encoding="utf-8")
    kind = "location" if ("\nevent:" in text or text.startswith("event:")) else "binary"
    cases = load_eval_cases(cases_path)
    if not cases:
        raise SystemExit(f"no eval cases found in {cases_path}")

    results: list[dict[str, Any]] = []
    failures = 0
    for case in cases:
        bot = _build_replay_bot(config_path, kind)  # fresh, isolated state per case
        held = str(case.get("held") or "")
        if held:
            bot.holdings.set_held(held, source="eval_case")
        article = _case_article(case["article"])
        decision = bot.process_article(article)
        ok, expected = _check(case, decision.action)
        if not ok:
            failures += 1
        results.append(
            {
                "name": str(case.get("name") or article.url),
                "ok": ok,
                "action": decision.action,
                "level": decision.level,
                "reason": decision.reason,
                "expected": expected,
            }
        )

    print(
        json.dumps(
            {
                "config": str(config_path),
                "kind": kind,
                "cases": len(results),
                "passed": len(results) - failures,
                "failed": failures,
                "results": results,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 1 if failures else 0


def _case_article(raw: dict[str, Any]) -> Article:
    defaults = {
        "url": "eval://case",
        "domain": "reuters.com",
        "title": "",
        "published_at": None,
        "fetched_at": "2026-01-01T00:00:00Z",
        "raw_text": "",
        "hash": "",
        "source_kind": "article",
    }
    fields = {**defaults, **{k: v for k, v in raw.items() if k in defaults}}
    if not fields["hash"]:
        fields["hash"] = f"eval:{abs(hash((fields['url'], fields['raw_text'])))}"
    if not fields["title"]:
        fields["title"] = str(fields["raw_text"])[:60]
    return Article(**fields)


def _check(case: dict[str, Any], action: str) -> tuple[bool, str]:
    if "expect_action" in case:
        expected = str(case["expect_action"])
        return action == expected, expected
    if "expect_action_in" in case:
        allowed = [str(item) for item in case["expect_action_in"]]
        return action in allowed, f"one of {allowed}"
    # forbid_trade
    return action not in TRADE_ACTIONS, "no trade action"


__all__ = ["eval_classifier_command", "load_eval_cases"]
