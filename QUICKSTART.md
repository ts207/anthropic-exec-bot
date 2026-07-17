# Quickstart

Everything runs through `make` (which wraps `bin/geo`, which wraps the
Python CLI). `make help` lists every target.

## 1. One-time setup

```bash
make setup          # venv + deps + creates .env from the template
$EDITOR .env        # fill in TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
make test           # 400+ tests should pass
```

**Classification runs on your Claude subscription by default** (provider
`claude_cli` in the config): install the Claude CLI and log in once —

```bash
npm install -g @anthropic-ai/claude-code
claude login
```

No `ANTHROPIC_API_KEY` needed. If you set one in `.env` anyway, it becomes
an automatic fallback when the CLI fails; set `classifier.provider:
anthropic` to use the metered API exclusively.

## 2. Paper mode (the default posture — cannot trade)

```bash
make paper          # the whole-universe autopilot, alert-only + dry-run
```

This discovers every geopolitical market on Polymarket, reads and grades
each market's resolution rules, builds news-source plans, scans both sides
of every outcome for edge, spawns one watcher bot per eligible market, and
sends you Telegram alerts for qualifying news, new eligible markets, new
executable edges, and group arbitrages. No order can leave: every bot is
dry-run and armed `alert_only`.

While it runs (from another terminal):

```bash
make status         # positions, heartbeats, ledger, drawdown headroom, top edges
make funnel         # where edge died across the universe
make calibration    # are the probability estimates beating the market?
```

Let this soak for at least 1–2 weeks. The funnel and calibration reports —
not a good day — are what justify going live.

## 3. Going live (deliberate, two changes + a flag)

1. In `configs/geopolitics/discovery.yaml` set:
   ```yaml
   fleet:
     position_mode: "live"
     auto_ack: true
   ```
2. Add the Polymarket wallet credentials to `.env`.
3. Run:
   ```bash
   make live I_UNDERSTAND_LIVE_TRADING=yes
   ```

Money is still bounded by the ledger caps in the config ($50/order,
$100/market, $300/region, $1000 total, 5 open positions) and the whole
fleet halts itself at $150 realized drawdown.

## 4. Kill switches

```bash
make halt           # MASTER KILL: all execution stops mid-cycle, everywhere
make watch-only     # keep watching + alerting, never trade
make arm            # back to live (per-market gates still apply)
```

## 5. Before changing anything (prompts, models, thresholds)

```bash
make eval BOT=configs/geopolitics/generated/<market>.yaml
    # adversarial regression cases; nonzero exit = the change regressed

make replay BOT=configs/geopolitics/generated/<market>.yaml ARTICLES=logs/binary_articles.jsonl
    # rerun the real article archive through the changed pipeline, isolated
```

## 6. Running it unattended

`make paper`/`make live` run in the foreground. For a real deployment
(survives reboots, hourly state backups), see
`docs/geopolitics/deployment.md` — units are in `deploy/`.

## Where things live

| What | Where |
|---|---|
| The one config | `configs/geopolitics/discovery.yaml` |
| Secrets | `.env` (never committed) |
| All state (ledger, holdings, journals) | `data/` — back it up (`make backup`) |
| Generated per-market bot configs | `configs/geopolitics/generated/` |
| Logs + article archive | `logs/` |
| Full risk analysis | `docs/geopolitics/risk-register.md` |
