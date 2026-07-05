import "dotenv/config";

import {
  createSecureClient,
  OrderSide,
  OrderType,
  type SecureClient,
} from "@polymarket/client";
import {
  fetchBalanceAllowance,
  type PrepareMarketOrderRequest,
} from "@polymarket/client/actions";
import { privateKey } from "@polymarket/client/viem";
import { AssetType, type ApiKeyCreds } from "@polymarket/bindings/clob";
import { http, type Hex } from "viem";
import { polygon } from "viem/chains";

type Action = "account" | "balance" | "book" | "open-orders" | "cancel-market-orders" | "fak";

const CONDITIONAL_DECIMALS = 1_000_000;
const BALANCE_POLL_ATTEMPTS = 20;
const BALANCE_POLL_INTERVAL_MS = 500;

async function main(): Promise<void> {
  const args = parseArgs(process.argv.slice(2));
  const action = requiredArg(args, "action") as Action;
  const client = await buildClient();
  const configuredWallet = process.env.DEPOSIT_WALLET_ADDRESS?.trim();

  if (action === "account") {
    return print({
      action,
      account: client.account,
      configured_deposit_wallet: configuredWallet || null,
      configured_wallet_matches_sdk: configuredWallet ? sameAddress(configuredWallet, client.account.wallet) : null,
    });
  }

  if (action === "balance") {
    requireConfiguredWalletMatches(client, configuredWallet);
    const yesTokenId = requiredArg(args, "yes-token-id");
    const noTokenId = requiredArg(args, "no-token-id");
    const [yes, no] = await Promise.all([
      conditionalBalance(client, yesTokenId),
      conditionalBalance(client, noTokenId),
    ]);
    return print({
      action,
      account: client.account,
      live_position: {
        yes_token_id: yesTokenId,
        no_token_id: noTokenId,
        yes_shares: yes.shares,
        no_shares: no.shares,
      },
      raw: { yes, no },
    });
  }

  if (action === "book") {
    const tokenId = requiredArg(args, "token-id");
    const book = await client.fetchOrderBook({ tokenId });
    return print({ action, token_id: tokenId, book: summarizeBook(book) });
  }

  if (action === "open-orders") {
    requireConfiguredWalletMatches(client, configuredWallet);
    const conditionId = requiredArg(args, "condition-id");
    const firstPage = await client.listOpenOrders({ market: conditionId }).firstPage();
    return print({
      action,
      condition_id: conditionId,
      open_orders: firstPage.items.map(summarizeOpenOrder),
      count: firstPage.items.length,
      has_more: firstPage.hasMore,
    });
  }

  if (action === "cancel-market-orders") {
    requireConfiguredWalletMatches(client, configuredWallet);
    requireMutationAllowed(action);
    const conditionId = requiredArg(args, "condition-id");
    const response = await client.cancelMarketOrders({ market: conditionId });
    return print({ action, condition_id: conditionId, response });
  }

  if (action === "fak") {
    requireConfiguredWalletMatches(client, configuredWallet);
    requireMutationAllowed(action);
    const tokenId = requiredArg(args, "token-id");
    const side = parseSide(requiredArg(args, "side"));
    const amount = positiveNumber(requiredArg(args, "amount"), "amount");
    const price = orderPrice(requiredArg(args, "price"), "price");
    const before = await conditionalBalance(client, tokenId);
    const request: PrepareMarketOrderRequest = side === OrderSide.SELL
      ? { tokenId, side, shares: amount, minPrice: price, orderType: OrderType.FAK as OrderType.FAK }
      : { tokenId, side, amount, maxPrice: price, orderType: OrderType.FAK as OrderType.FAK };
    let response: unknown;
    try {
      response = await client.placeMarketOrder(request);
    } catch (error) {
      // A FAK with nothing to match is killed with zero fill; the SDK surfaces
      // that as a rejection. Report it as an ordinary zero-fill result so the
      // executor's partial-fill handling applies instead of EXECUTION_ERROR.
      if (error instanceof Error && /no orders found to match/i.test(error.message)) {
        return print({
          action,
          live: true,
          token_id: tokenId,
          side,
          amount,
          price,
          order_type: OrderType.FAK,
          balance_before: before.shares,
          balance_after: before.shares,
          filled_shares: 0,
          response: { rejected: true, reason: error.message },
        });
      }
      throw error;
    }
    const after = await pollBalanceChange(client, tokenId, before.shares, side);
    const filledShares = side === OrderSide.BUY
      ? Math.max(0, after.shares - before.shares)
      : Math.max(0, before.shares - after.shares);
    return print({
      action,
      live: true,
      token_id: tokenId,
      side,
      amount,
      price,
      order_type: OrderType.FAK,
      balance_before: before.shares,
      balance_after: after.shares,
      filled_shares: filledShares,
      response,
    });
  }

  throw new Error(`unsupported action: ${action}`);
}

async function buildClient(): Promise<SecureClient> {
  const chainId = Number(process.env.CHAIN_ID ?? "137");
  if (chainId !== 137) throw new Error(`only Polygon mainnet is configured, got CHAIN_ID=${chainId}`);
  return createSecureClient({
    signer: privateKey(requiredHex("PRIVATE_KEY"), {
      chain: polygon,
      transport: http(process.env.POLYGON_RPC_URL ?? process.env.RPC_URL),
    }),
    credentials: {
      key: requiredEnv("CLOB_API_KEY"),
      secret: requiredEnv("CLOB_SECRET"),
      passphrase: requiredEnv("CLOB_PASS_PHRASE"),
    } as ApiKeyCreds,
  });
}

function requireConfiguredWalletMatches(client: SecureClient, configuredWallet: string | undefined): void {
  if (!configuredWallet) return;
  if (sameAddress(configuredWallet, client.account.wallet)) return;
  throw new Error(
    `polymarket beta SDK derived wallet ${client.account.wallet}, but DEPOSIT_WALLET_ADDRESS is ${configuredWallet}; refusing to query/trade the wrong wallet`,
  );
}

async function conditionalBalance(client: SecureClient, tokenId: string): Promise<{ raw: string; shares: number; allowances: Record<string, string> }> {
  const result = await fetchBalanceAllowance(client, { assetType: AssetType.CONDITIONAL, tokenId });
  const record = result as Record<string, unknown>;
  const raw = String(record.balance ?? record.available ?? record.availableBalance ?? "0");
  return {
    raw,
    shares: Number(raw) / CONDITIONAL_DECIMALS,
    allowances: asStringRecord(record.allowances),
  };
}

async function pollBalanceChange(client: SecureClient, tokenId: string, before: number, side: OrderSide): Promise<{ raw: string; shares: number; allowances: Record<string, string> }> {
  let latest = await conditionalBalance(client, tokenId);
  for (let attempt = 0; attempt < BALANCE_POLL_ATTEMPTS; attempt += 1) {
    if (side === OrderSide.BUY && latest.shares > before) return latest;
    if (side === OrderSide.SELL && latest.shares < before) return latest;
    await sleep(BALANCE_POLL_INTERVAL_MS);
    latest = await conditionalBalance(client, tokenId);
  }
  return latest;
}

function summarizeBook(book: unknown): Record<string, unknown> {
  const record = asRecord(book);
  return {
    best_bid: bestPrice(record.bids, "bid"),
    best_ask: bestPrice(record.asks, "ask"),
    tick_size: record.tickSize ?? record.tick_size,
    min_order_size: record.minOrderSize ?? record.min_order_size,
    neg_risk: record.negRisk ?? record.neg_risk,
    hash: record.hash,
  };
}

function summarizeOpenOrder(order: unknown): Record<string, unknown> {
  const record = asRecord(order);
  return {
    id: record.id,
    status: record.status,
    market: record.market,
    asset_id: record.assetId ?? record.asset_id,
    side: record.side,
    original_size: record.originalSize ?? record.original_size,
    size_matched: record.sizeMatched ?? record.size_matched,
    price: record.price,
    order_type: record.orderType ?? record.order_type,
  };
}

function parseArgs(argv: string[]): Map<string, string> {
  const values = new Map<string, string>();
  for (let i = 0; i < argv.length; i += 1) {
    const item = argv[i];
    if (!item?.startsWith("--")) throw new Error(`unexpected argument: ${item}`);
    const key = item.slice(2);
    const value = argv[i + 1];
    if (!value || value.startsWith("--")) throw new Error(`missing value for --${key}`);
    values.set(key, value);
    i += 1;
  }
  return values;
}

function requireMutationAllowed(action: Action): void {
  if (process.env.POLYBOT_TS_BRIDGE_ALLOW_POST !== "1") {
    throw new Error(`${action} requires POLYBOT_TS_BRIDGE_ALLOW_POST=1`);
  }
}

function requiredArg(args: Map<string, string>, name: string): string {
  const value = args.get(name);
  if (!value?.trim()) throw new Error(`--${name} is required`);
  return value;
}

function parseSide(value: string): OrderSide {
  const normalized = value.toUpperCase();
  if (normalized === OrderSide.BUY) return OrderSide.BUY;
  if (normalized === OrderSide.SELL) return OrderSide.SELL;
  throw new Error("--side must be BUY or SELL");
}

function orderPrice(value: string, name: string): number {
  const parsed = positiveNumber(value, name);
  if (parsed > 1) throw new Error(`${name} must be <= 1`);
  return parsed;
}

function positiveNumber(value: string, name: string): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed <= 0) throw new Error(`${name} must be a positive number`);
  return parsed;
}

function bestPrice(levels: unknown, side: "ask" | "bid"): number | null {
  const prices = Array.isArray(levels)
    ? levels.map((level) => Number(asRecord(level).price)).filter(Number.isFinite)
    : [];
  if (!prices.length) return null;
  return side === "ask" ? Math.min(...prices) : Math.max(...prices);
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? value as Record<string, unknown> : {};
}

function asStringRecord(value: unknown): Record<string, string> {
  const record = asRecord(value);
  return Object.fromEntries(Object.entries(record).map(([key, item]) => [key, String(item)]));
}

function sameAddress(left: string, right: string): boolean {
  return left.toLowerCase() === right.toLowerCase();
}

function requiredEnv(name: string): string {
  const value = process.env[name];
  if (!value?.trim()) throw new Error(`${name} is required`);
  return value.trim();
}

function requiredHex(name: string): Hex {
  const value = requiredEnv(name);
  if (!/^0x[0-9a-fA-F]+$/.test(value)) throw new Error(`${name} must be a hex string`);
  return value as Hex;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function print(value: unknown): void {
  console.log(JSON.stringify(value, null, 2));
}

main().catch((error: unknown) => {
  console.error(error instanceof Error ? error.stack : error);
  process.exitCode = 1;
});
