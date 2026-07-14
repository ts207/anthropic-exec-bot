# Deployment

The fleet is a long-running process managing real money. "A terminal on a
laptop" is not a deployment: a reboot silently stops all trading **and all
position defense**. This is the minimum production setup.

## Layout

```
/opt/anthropic-exec-bot        # the checkout (git clone + .venv)
/etc/polybot/env               # secrets, mode 0600, owner polybot
/opt/anthropic-exec-bot/backups# hourly data snapshots (or POLYBOT_BACKUP_DIR)
```

Create a dedicated `polybot` user; nothing here needs root at runtime.

## Secrets

`/etc/polybot/env` (never in the repo, never in the unit file):

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
ANTHROPIC_API_KEY=...
# plus the wallet/CLOB credentials the live adapter reads
```

```bash
sudo install -m 0600 -o polybot /dev/null /etc/polybot/env  # then edit
```

## The fleet service

```bash
sudo cp deploy/polybot-fleet.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now polybot-fleet
journalctl -u polybot-fleet -f          # follow the supervisor
```

`Restart=always` is safe by construction: the operator gate, per-market
process locks, and execution journals mean a restarted supervisor *resumes
management* — it never re-fires trades. The arming sequence (ack, position
modes) is unchanged; the service just keeps the loop alive.

## Backups

`data/` is the financial record — ledger, holdings, journals, calibration
logs, operator acks. The wallet recovers *positions* after a disk loss;
nothing recovers realized-P&L history, calibration data, or ack state.

```bash
sudo cp deploy/polybot-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now polybot-backup.timer
```

Hourly `polybot-data-<stamp>.tar.gz` snapshots, most recent 14 kept
(`POLYBOT_BACKUP_KEEP`). Restore = stop the service, untar over the
checkout, start the service (wallet reconciliation then re-verifies every
position against the chain on the first live cycle).

## Operating it

```bash
# the 3am view: positions, heartbeats, ledger, drawdown headroom, last scan
python -m polybot.geopolitics fleet-status --config configs/geopolitics/discovery.yaml

# master switches
python -m polybot.geopolitics set-fleet-mode off         # halt everything mid-cycle
python -m polybot.geopolitics set-fleet-mode alert_only  # watch, never trade
python -m polybot.geopolitics set-fleet-mode live        # defer to per-market modes

# before arming any prompt/model/config change
python -m polybot.geopolitics eval-classifier --config <bot.yaml> \
    --cases configs/geopolitics/eval-cases/binary-adversarial.jsonl
python -m polybot.geopolitics replay --config <bot.yaml> --articles logs/binary_articles.jsonl
```

The drawdown halt, per-domain feed backoff, hung-bot restarts, and ledger
reconcile all run inside the fleet loop — no cron needed beyond the backup
timer.
