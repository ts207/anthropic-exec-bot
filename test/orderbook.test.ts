import assert from "node:assert/strict";
import test from "node:test";
import {
  applyOrderbookMessage,
  createOrderbookCache,
  getBestAsk,
  getFreshBestAsk,
} from "../src/valuation/legacy/orderbook.ts";
import type { SelectedMarket } from "../src/valuation/legacy/polymarket.ts";

const market: SelectedMarket = {
  eventSlug: "will-anthropics-valuation-hit-by-june-30",
  selectedMarket: "$1.1T",
  question: "Will Anthropic hit $1.1T?",
  conditionId: "0xcondition",
  yesTokenId: "yes-token",
  noTokenId: "no-token",
  tickSize: "0.01",
  negRisk: false,
};

test("book snapshot sets best ask and bid for the matching token", () => {
  const cache = createOrderbookCache(market);

  applyOrderbookMessage(
    cache,
    JSON.stringify({
      event_type: "book",
      asset_id: "yes-token",
      asks: [{ price: "0.94" }, { price: "0.96" }],
      bids: [{ price: "0.91" }, { price: "0.90" }],
    }),
  );

  assert.equal(cache.yes.bestAsk, 0.94);
  assert.equal(cache.yes.bestBid, 0.91);
  assert.equal(getBestAsk(cache, "YES"), 0.94);
  assert.equal(getBestAsk(cache, "NO"), null);
});

test("best_bid_ask updates the matching side", () => {
  const cache = createOrderbookCache(market);

  applyOrderbookMessage(
    cache,
    JSON.stringify({
      event_type: "best_bid_ask",
      asset_id: "no-token",
      best_ask: "0.88",
      best_bid: "0.85",
    }),
  );

  assert.equal(cache.no.bestAsk, 0.88);
  assert.equal(cache.no.bestBid, 0.85);
});

test("price_change array can update both token books", () => {
  const cache = createOrderbookCache(market);

  applyOrderbookMessage(
    cache,
    JSON.stringify({
      event_type: "price_change",
      price_changes: [
        { asset_id: "yes-token", best_ask: "0.93", best_bid: "0.92" },
        { asset_id: "no-token", best_ask: "0.89", best_bid: "0.87" },
      ],
    }),
  );

  assert.equal(cache.yes.bestAsk, 0.93);
  assert.equal(cache.no.bestAsk, 0.89);
});

test("fresh best ask returns price and age within max age", () => {
  const cache = createOrderbookCache(market);
  const now = Date.parse("2026-07-01T17:00:01.000Z");
  cache.yes.bestAsk = 0.64;
  cache.yes.updatedAt = "2026-07-01T17:00:00.250Z";

  const quote = getFreshBestAsk(cache, "YES", 1000, now);

  assert.equal(quote.bestAsk, 0.64);
  assert.equal(quote.updatedAt, "2026-07-01T17:00:00.250Z");
  assert.equal(quote.ageMs, 750);
});

test("fresh best ask rejects missing quote", () => {
  const cache = createOrderbookCache(market);

  assert.throws(
    () => getFreshBestAsk(cache, "YES", 1000),
    /missing YES best ask/,
  );
});

test("fresh best ask rejects stale quote", () => {
  const cache = createOrderbookCache(market);
  const now = Date.parse("2026-07-01T17:00:01.000Z");
  cache.yes.bestAsk = 0.64;
  cache.yes.updatedAt = "2026-07-01T16:48:59.065Z";

  assert.throws(
    () => getFreshBestAsk(cache, "YES", 1000, now),
    /quote age .* exceeds 1000ms/,
  );
});
