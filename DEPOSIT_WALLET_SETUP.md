# Deposit Wallet Setup

The bot must use Polymarket deposit-wallet order flow for live trading:

- CLOB order `signatureType = POLY_1271`
- CLOB order funder = deposit wallet address
- pUSD held in the deposit wallet, not the EOA
- pUSD approvals submitted from the deposit wallet through the relayer

Required `.env` fields:

```bash
RELAYER_URL=
BUILDER_API_KEY=
BUILDER_SECRET=
BUILDER_PASS_PHRASE=

CLOB_API_KEY=
CLOB_SECRET=
CLOB_PASS_PHRASE=

DEPOSIT_WALLET_ADDRESS=
```

Setup order:

```bash
npm run deposit-wallet -- derive
npm run deposit-wallet -- deploy
```

Copy the printed `depositWalletAddress` into `.env` as `DEPOSIT_WALLET_ADDRESS`.
Transfer pUSD from the EOA to that deposit wallet address.

Then run:

```bash
npm run deposit-wallet -- approve
npm run deposit-wallet -- sync
```

`sync` should show nonzero `onchain.pUsd`, high `exchangeV2Allowance`, and CLOB
balance allowance for the deposit wallet.

Before retrying a live bot run after the previous rejected order, remove the stale
lock only after deposit-wallet setup is complete:

```bash
mv logs/traded.lock logs/traded.failed-eoa.lock
```
