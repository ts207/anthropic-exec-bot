from __future__ import annotations

import json
from pathlib import Path

from polybot.replay import load_replay_articles, replay_articles_command


def _binary_yaml(tmp_path: Path) -> Path:
    path = tmp_path / "binary.yaml"
    path.write_text(
        f"""
market:
  slug: "test-slug"
  question: "Will the qualifying event happen by September 30, 2026?"
  deadline_date: "2026-09-30"
  held_side: ""
  resolution_rules: |
    Resolves YES if the qualifying event happens by the deadline.
entry:
  enabled: true
  side: "YES"
  usd_budget: 100.0
classifier:
  provider: rule_based
data_dir: {tmp_path / 'data'}
logs_dir: {tmp_path / 'logs'}
""",
        encoding="utf-8",
    )
    return path


def _article_line(url: str, text: str, domain: str = "reuters.com") -> str:
    return json.dumps(
        {
            "url": url,
            "domain": domain,
            "title": text[:60],
            "published_at": None,
            "fetched_at": "2026-07-10T00:00:00Z",
            "raw_text": text,
            "hash": url,
            "source_kind": "article",
        }
    )


def test_load_replay_articles_skips_junk_and_respects_limit(tmp_path) -> None:
    path = tmp_path / "articles.jsonl"
    path.write_text(
        "\n".join(
            [
                _article_line("https://reuters.com/a", "first story text"),
                "not json",
                json.dumps({"url": "https://reuters.com/empty", "raw_text": ""}),
                _article_line("https://reuters.com/b", "second story text"),
            ]
        ),
        encoding="utf-8",
    )
    articles = load_replay_articles(path)
    assert [a.url for a in articles] == ["https://reuters.com/a", "https://reuters.com/b"]
    assert len(load_replay_articles(path, limit=1)) == 1


def test_replay_runs_full_decision_pipeline_in_isolation(tmp_path, capsys) -> None:
    config_path = _binary_yaml(tmp_path)
    articles_path = tmp_path / "articles.jsonl"
    articles_path.write_text(
        "\n".join(
            [
                _article_line("https://example.com/sports", "Unrelated sports story about the cup final."),
                _article_line(
                    "https://reuters.com/talks",
                    "US and Iran senior talks scheduled: the round will be held in Doha next week.",
                ),
            ]
        ),
        encoding="utf-8",
    )

    assert replay_articles_command(config_path, articles_path) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["kind"] == "binary"
    assert summary["replayed"] == 2
    assert summary["actions"].get("ENTER_YES") == 1
    assert summary["actions"].get("NO_ACTION") == 1  # unrelated story, no trigger
    assert summary["final_state"] == "ENTERED"
    assert summary["final_held"] == "yes"
    # Isolation: replay state lives under data/replay, production dirs untouched.
    assert (tmp_path / "data" / "replay").exists()
    assert not (tmp_path / "data" / "dry_run").exists()

    # Re-running wipes the previous replay state: same result, no bleed-over
    # from the prior run's ENTERED terminal state or holdings.
    assert replay_articles_command(config_path, articles_path) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["actions"].get("ENTER_YES") == 1
    assert summary["final_held"] == "yes"


# ---- eval harness ----


def _case_line(name: str, text: str, expectation: dict, held: str = "") -> str:
    case = {"name": name, "article": {"url": f"https://reuters.com/{name}", "raw_text": text}, **expectation}
    if held:
        case["held"] = held
    return json.dumps(case)


def test_eval_classifier_passes_and_isolates_cases(tmp_path, capsys) -> None:
    from polybot.evalset import eval_classifier_command

    config_path = _binary_yaml(tmp_path)
    cases_path = tmp_path / "cases.jsonl"
    cases_path.write_text(
        "\n".join(
            [
                "# comment lines are ignored",
                _case_line("unrelated_must_not_trade", "Unrelated sports story about the cup final.", {"forbid_trade": True}),
                _case_line(
                    "confirmed_should_enter",
                    "US and Iran senior talks scheduled: the round will be held in Doha next week.",
                    {"expect_action": "ENTER_YES"},
                ),
                # Runs AFTER the entering case: a fresh bot per case means this
                # flat-entry case must still enter (no held state bleed-over).
                _case_line(
                    "still_flat_for_next_case",
                    "US and Iran senior talks scheduled: the round will be held in Doha next week.",
                    {"expect_action_in": ["ENTER_YES"]},
                ),
                _case_line(
                    "held_yes_cancellation_must_exit",
                    "Officials say the talks are cancelled and the round will not happen.",
                    {"expect_action": "EXIT_HELD"},
                    held="yes",
                ),
            ]
        ),
        encoding="utf-8",
    )
    assert eval_classifier_command(config_path, cases_path) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["cases"] == 4 and report["failed"] == 0


def test_eval_classifier_fails_ci_on_regression(tmp_path, capsys) -> None:
    from polybot.evalset import eval_classifier_command

    config_path = _binary_yaml(tmp_path)
    cases_path = tmp_path / "cases.jsonl"
    # The fixture classifier WILL enter on this text; expecting no-trade makes
    # the case fail -- which must surface as a nonzero exit code.
    cases_path.write_text(
        _case_line("regression", "US and Iran senior talks scheduled: the round will be held in Doha next week.", {"forbid_trade": True}),
        encoding="utf-8",
    )
    assert eval_classifier_command(config_path, cases_path) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["failed"] == 1
    assert report["results"][0]["ok"] is False


def test_eval_case_file_validates_loudly(tmp_path) -> None:
    import pytest

    from polybot.evalset import load_eval_cases

    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"name": "no-expectation", "article": {"url": "u", "raw_text": "t"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="no expectation"):
        load_eval_cases(bad)


def test_shipped_adversarial_corpus_parses() -> None:
    from polybot.evalset import load_eval_cases

    cases = load_eval_cases(Path("configs/geopolitics/eval-cases/binary-adversarial.jsonl"))
    assert len(cases) >= 8
    assert all("article" in case for case in cases)
