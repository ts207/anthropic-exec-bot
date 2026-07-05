from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .book import BookCache
from .gamma import markets_for_event_slug


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="polybot")
    sub = parser.add_subparsers(dest="command", required=True)
    inspect_parser = sub.add_parser("inspect")
    inspect_parser.add_argument("slug")
    positions_parser = sub.add_parser("positions")
    positions_parser.add_argument("--config", required=True)
    inspect_position_parser = sub.add_parser("inspect-position")
    inspect_position_parser.add_argument("position_id")
    inspect_position_parser.add_argument("--config", required=True)
    inspect_iran_parser = sub.add_parser("inspect-iran")
    inspect_iran_parser.add_argument("--config", required=True)
    inspect_iran_position_parser = sub.add_parser("inspect-iran-position")
    inspect_iran_position_parser.add_argument("--config", required=True)
    preflight_iran_parser = sub.add_parser("preflight-iran")
    preflight_iran_parser.add_argument("--config", required=True)
    preflight_iran_parser.add_argument("--live", action="store_true")
    ack_iran_live_parser = sub.add_parser("ack-iran-live")
    ack_iran_live_parser.add_argument("--config", required=True)
    ack_iran_live_parser.add_argument("--note", default="")
    set_iran_mode_parser = sub.add_parser("set-iran-mode")
    set_iran_mode_parser.add_argument("--config", required=True)
    set_iran_mode_parser.add_argument("--mode", required=True, choices=["off", "alert_only", "dry_run", "live"])
    probe_iran_v2_parser = sub.add_parser("probe-iran-clob-v2")
    probe_iran_v2_parser.add_argument("--config", required=True)
    probe_iran_v2_parser.add_argument("--amount", type=float, default=5.0)
    probe_iran_v2_parser.add_argument("--price", type=float)
    probe_iran_v2_parser.add_argument("--post", action="store_true")
    smoke_iran_classifier_parser = sub.add_parser("smoke-iran-classifier")
    smoke_iran_classifier_parser.add_argument("--config", required=True)
    smoke_iran_classifier_parser.add_argument("--url")
    smoke_iran_classifier_parser.add_argument("--text")
    smoke_iran_classifier_parser.add_argument("--title", default="classifier smoke")
    smoke_iran_classifier_parser.add_argument("--domain", default="reuters.com")
    run_iran_parser = sub.add_parser("run-iran")
    run_iran_parser.add_argument("--config", required=True)
    run_iran_parser.add_argument("--live", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "inspect":
        return inspect_command(args.slug)
    if args.command == "positions":
        from .portfolio import print_portfolio_snapshot

        return print_portfolio_snapshot(Path(args.config))
    if args.command == "inspect-position":
        from .portfolio import print_portfolio_snapshot

        return print_portfolio_snapshot(Path(args.config), position_id=args.position_id)
    if args.command == "inspect-iran":
        from .iran.runner import inspect_iran_command

        return inspect_iran_command(Path(args.config))
    if args.command == "inspect-iran-position":
        from .iran.runner import inspect_iran_position_command

        return inspect_iran_position_command(Path(args.config))
    if args.command == "preflight-iran":
        from .iran.runner import preflight_iran_command

        return preflight_iran_command(Path(args.config), live_flag=args.live)
    if args.command == "ack-iran-live":
        from .iran.runner import ack_iran_live_command

        return ack_iran_live_command(Path(args.config), note=args.note)
    if args.command == "set-iran-mode":
        from .iran.runner import set_iran_mode_command

        return set_iran_mode_command(Path(args.config), mode=args.mode)
    if args.command == "probe-iran-clob-v2":
        from .iran.runner import probe_iran_clob_v2_command

        return probe_iran_clob_v2_command(Path(args.config), amount=args.amount, post=args.post, price=args.price)
    if args.command == "smoke-iran-classifier":
        from .iran.runner import smoke_iran_classifier_command

        return smoke_iran_classifier_command(Path(args.config), url=args.url, text=args.text, title=args.title, domain=args.domain)
    if args.command == "run-iran":
        from .iran.runner import run_iran_command

        return run_iran_command(Path(args.config), live_flag=args.live)
    return 2


def inspect_command(slug: str) -> int:
    markets = markets_for_event_slug(slug)
    print(json.dumps([market.__dict__ for market in markets], indent=2))
    for market in markets:
        book = BookCache(market.token_ids)
        for token_id in market.token_ids:
            try:
                book.rest_snapshot(token_id)
            except Exception as exc:
                print(f"book snapshot failed for {token_id}: {exc}", file=sys.stderr)
        print(json.dumps({token: book.snapshot_state(token) for token in market.token_ids}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
