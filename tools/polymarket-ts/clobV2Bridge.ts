import "dotenv/config";

import { webcrypto } from "node:crypto";
import {
  AssetType,
  ClobClient,
  OrderType,
  Side,
  SignatureTypeV2,
  type BalanceAllowanceResponse,
  type OpenOrder,
  type OrderBookSummary,
} from "@polymarket/clob-client-v2";
import { createWalletClient, http, type Hex } from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { polygon } from "viem/chains";

type TickSize = "0.1" | "0.01" | "0.001" | "0.0001";
type Action = "balance" | "book" | "open-orders" | "cancel-market-orders" | "fak";

const CONDITIONAL_DECIMALS = 1_000_000;
const BALANCE_POLL_ATTEMPTS = 20;
const BALANCE_POLL_INTERVAL_MS = 500;

if (!globalThis.crypto?.subtle) {
  Object.defineProperty(globalThis, "crypto", { value: webcrypto });
}

async function main(): Promise<void> {
  const args = parseArgs(process.argv.slice(2));
  const action = requiredArg(args, "action") as Action;
  const client = buildClient();

  if (action === "balance") {
    const yesTokenId = requiredArg(args, "yes-token-id");
    const noTokenId = requiredArg(args, "no-token-id");
    const [yes, no] = await Promise.all([
      conditionalBalance(client, yesTokenId),
      conditionalBalance(client, noTokenId),
    ]);
    return print({
      action,
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
    const book = await client.getOrderBook(tokenId);
    return print({
      action,
      token_id: tokenId,
      book: summarizeBook(book),
    });
  }

  if (action === "open-orders") {
    const conditionId = requiredArg(args, "condition-id");
    const orders = await client.getOpenOrders({ market: conditionId }, true);
    return print({
      action,
      condition_id: conditionId,
      open_orders: orders.map(summarizeOpenOrder),
      count: orders.length,
    });
  }

  if (action === "cancel-market-orders") {
    requireMutationAllowed(action);
    const conditionId = requiredArg(args, "condition-id");
    const response = await client.cancelMarketOrders({ market: conditionId });
    return print({
      action,
      condition_id: conditionId,
      response,
    });
  }

  if (action === "fak") {
    requireMutationAllowed(action);
    const tokenId = requiredArg(args, "token-id");
    const side = parseSide(requiredArg(args, "side"));
    const amount = positiveNumber(requiredArg(args, "amount"), "amount");
    const price = orderPrice(requiredArg(args, "price"), "price");
    const tickSize = parseTickSize(requiredArg(args, "tick-size"));
    const negRisk = parseBoolean(args.get("neg-risk") ?? "false");
    const before = await conditionalBalance(client, tokenId);
    const signedOrder = await client.createMarketOrder(
      {
        tokenID: tokenId,
        amount,
        side,
        price,
        orderType: OrderType.FAK,
      },
      { tickSize, negRisk },
    );
    const response = await client.postOrder(signedOrder, OrderType.FAK);
    const after = await pollBalanceChange(client, tokenId, before.shares, side);
    const filledShares = side === Side.BUY
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
      signed_order: summarizeSignedOrder(signedOrder),
      response,
    });
  }

  throw new Error(`unsupported action: ${action}`);
}

function buildClient(): ClobClient {
  const privateKey = requiredHex("PRIVATE_KEY");
  const depositWalletAddress = requiredHex("DEPOSIT_WALLET_ADDRESS");
  const chainId = Number(process.env.CHAIN_ID ?? "137");
  if (chainId !== 137) {
    throw new Error(`only Polygon mainnet is configured, got CHAIN_ID=${chainId}`);
  }
  const account = privateKeyToAccount(privateKey);
  const signer = createWalletClient({
    account,
    chain: polygon,
    transport: http(process.env.POLYGON_RPC_URL ?? process.env.RPC_URL),
  });
  return new ClobClient({
    host: process.env.CLOB_HOST ?? "https://clob.polymarket.com",
    chain: chainId,
    signer,
    creds: {
      key: requiredEnv("CLOB_API_KEY"),
      secret: requiredEnv("CLOB_SECRET"),
      passphrase: requiredEnv("CLOB_PASS_PHRASE"),
    },
    signatureType: SignatureTypeV2.POLY_1271,
    funderAddress: depositWalletAddress,
  });
}

async function conditionalBalance(client: ClobClient, tokenId: string): Promise<{ raw: string; shares: number; allowances: Record<string, string> }> {
  const result: BalanceAllowanceResponse = await client.getBalanceAllowance({
    asset_type: AssetType.CONDITIONAL,
    token_id: tokenId,
  });
  return {
    raw: result.balance,
    shares: Number(result.balance) / CONDITIONAL_DECIMALS,
    allowances: result.allowances,
  };
}

async function pollBalanceChange(client: ClobClient, tokenId: string, before: number, side: Side): Promise<{ raw: string; shares: number; allowances: Record<string, string> }> {
  let latest = await conditionalBalance(client, tokenId);
  for (let attempt = 0; attempt < BALANCE_POLL_ATTEMPTS; attempt += 1) {
    if (side === Side.BUY && latest.shares > before) return latest;
    if (side === Side.SELL && latest.shares < before) return latest;
    await sleep(BALANCE_POLL_INTERVAL_MS);
    latest = await conditionalBalance(client, tokenId);
  }
  return latest;
}

function summarizeBook(book: OrderBookSummary): Record<string, unknown> {
  return {
    best_bid: bestBid(book.bids),
    best_ask: bestAsk(book.asks),
    tick_size: book.tick_size,
    min_order_size: book.min_order_size,
    neg_risk: book.neg_risk,
    hash: book.hash,
  };
}

function summarizeOpenOrder(order: OpenOrder): Record<string, string | number> {
  return {
    id: order.id,
    status: order.status,
    market: order.market,
    asset_id: order.asset_id,
    side: order.side,
    original_size: order.original_size,
    size_matched: order.size_matched,
    price: order.price,
    order_type: order.order_type,
  };
}

function summarizeSignedOrder(order: unknown): Record<string, unknown> {
  if (!order || typeof order !== "object") return { created: false };
  const record = order as Record<string, unknown>;
  return {
    created: true,
    maker: record.maker,
    signer: record.signer,
    token_id: record.tokenId,
    side: record.side,
    signature_type: record.signatureType,
    maker_amount: record.makerAmount,
    taker_amount: record.takerAmount,
    has_signature: typeof record.signature === "string" && record.signature.length > 0,
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

function parseSide(value: string): Side {
  const normalized = value.toUpperCase();
  if (normalized === Side.BUY) return Side.BUY;
  if (normalized === Side.SELL) return Side.SELL;
  throw new Error("--side must be BUY or SELL");
}

function parseTickSize(value: string): TickSize {
  if (value === "0.1" || value === "0.01" || value === "0.001" || value === "0.0001") return value;
  throw new Error("--tick-size must be one of 0.1, 0.01, 0.001, 0.0001");
}

function parseBoolean(value: string): boolean {
  if (value === "1" || value.toLowerCase() === "true") return true;
  if (value === "0" || value.toLowerCase() === "false") return false;
  throw new Error("boolean value must be true/false or 1/0");
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

function bestBid(levels: Array<{ price: string }>): number | null {
  const prices = levels.map((level) => Number(level.price)).filter(Number.isFinite);
  return prices.length ? Math.max(...prices) : null;
}

function bestAsk(levels: Array<{ price: string }>): number | null {
  const prices = levels.map((level) => Number(level.price)).filter(Number.isFinite);
  return prices.length ? Math.min(...prices) : null;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
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

function print(value: unknown): void {
  console.log(JSON.stringify(value, null, 2));
}

main().catch((error: unknown) => {
  console.error(error instanceof Error ? error.stack : error);
  process.exitCode = 1;
});
