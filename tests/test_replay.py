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
