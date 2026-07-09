import type { BookQuote } from "./signalTypes.ts";

const CLOB_HOST = "https://clob.polymarket.com";

export async function fetchBookQuote(tokenId: string, host = CLOB_HOST): Promise<BookQuote> {
  const url = `${host.replace(/\/$/, "")}/book?token_id=${encodeURIComponent(tokenId)}`;
  const response = await fetch(url, { headers: { accept: "application/json" } });
  if (!response.ok) throw new Error(`orderbook fetch failed for ${tokenId}: ${response.status} ${response.statusText}`);
  const raw = await response.json() as unknown;
  return parseBookQuote(raw, tokenId);
}

export function parseBookQuote(raw: unknown, tokenId: string, fetchedAt = new Date().toISOString()): BookQuote {
  const record = asRecord(raw);
  const bids = parseLevels(record.bids);
  const asks = parseLevels(record.asks);
  const bestBid = bids.length ? Math.max(...bids.map((level) => level.price)) : null;
  const bestAsk = asks.length ? Math.min(...asks.map((level) => level.price)) : null;
  const spread = bestAsk !== null && bestBid !== null ? Math.max(0, bestAsk - bestBid) : null;
  const visibleAskDepth = asks.reduce((sum, level) => sum + level.price * level.size, 0);
  return {
    tokenId,
    bestBid,
    bestAsk,
    spread,
    liquidity: visibleAskDepth,
    fetchedAt,
    bids,
    asks,
  };
}

function parseLevels(value: unknown): Array<{ price: number; size: number }> {
  return Array.isArray(value)
    ? value.flatMap((item) => {
      const record = asRecord(item);
      const price = Number(record.price);
      const size = Number(record.size);
      return Number.isFinite(price) && Number.isFinite(size) ? [{ price, size }] : [];
    })
    : [];
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
