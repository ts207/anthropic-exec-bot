import type { SelectedMarket } from "./polymarket.ts";

export type SideBook = {
  tokenId: string;
  bestAsk: number | null;
  bestBid: number | null;
  updatedAt: string | null;
};

export type OrderbookCache = {
  yes: SideBook;
  no: SideBook;
};

export type OrderbookSubscription = {
  cache: OrderbookCache;
  close: () => void;
};

export type FreshBestAsk = {
  bestAsk: number;
  updatedAt: string;
  ageMs: number;
};

type WsLike = {
  send: (data: string) => void;
  close: () => void;
  addEventListener: (
    event: "open" | "message" | "error" | "close",
    listener: (event: Event | MessageEvent) => void,
  ) => void;
};

export function createOrderbookCache(market: SelectedMarket): OrderbookCache {
  return {
    yes: {
      tokenId: market.yesTokenId,
      bestAsk: null,
      bestBid: null,
      updatedAt: null,
    },
    no: {
      tokenId: market.noTokenId,
      bestAsk: null,
      bestBid: null,
      updatedAt: null,
    },
  };
}

export function subscribeOrderbook(market: SelectedMarket): OrderbookSubscription {
  const cache = createOrderbookCache(market);
  let stopped = false;
  let reconnectTimer: NodeJS.Timeout | null = null;
  let ws: WsLike | null = null;

  const connect = (): void => {
    if (stopped) return;

    ws = new WebSocket(
      "wss://ws-subscriptions-clob.polymarket.com/ws/market",
    ) as WsLike;

    ws.addEventListener("open", () => {
      ws?.send(
        JSON.stringify({
          type: "market",
          assets_ids: [market.yesTokenId, market.noTokenId],
          custom_feature_enabled: true,
        }),
      );
    });

    ws.addEventListener("message", (event) => {
      const data = (event as MessageEvent).data;
      if (typeof data !== "string") {
        return;
      }
      applyOrderbookMessage(cache, data);
    });

    ws.addEventListener("error", (event) => {
      invalidateOrderbookCache(cache);
      console.error("ORDERBOOK_WS_ERROR", event);
    });

    ws.addEventListener("close", () => {
      invalidateOrderbookCache(cache);
      if (!stopped) {
        reconnectTimer = setTimeout(connect, 250);
      }
    });
  };

  connect();

  return {
    cache,
    close: () => {
      stopped = true;
      if (reconnectTimer) {
        clearTimeout(reconnectTimer);
      }
      ws?.close();
    },
  };
}

export function applyOrderbookMessage(
  cache: OrderbookCache,
  rawMessage: string,
): void {
  const decoded = parseJson(rawMessage);
  const messages = Array.isArray(decoded) ? decoded : [decoded];

  for (const message of messages) {
    if (!message || typeof message !== "object") {
      continue;
    }
    applyOrderbookEvent(cache, message as Record<string, unknown>);
  }
}

export function getBestAsk(
  cache: OrderbookCache,
  side: "YES" | "NO",
): number | null {
  return side === "YES" ? cache.yes.bestAsk : cache.no.bestAsk;
}

export function getFreshBestAsk(
  cache: OrderbookCache,
  side: "YES" | "NO",
  maxAgeMs: number,
  nowMs = Date.now(),
): FreshBestAsk {
  const book = side === "YES" ? cache.yes : cache.no;
  if (book.bestAsk === null || book.updatedAt === null) {
    throw new Error(`stale orderbook: missing ${side} best ask`);
  }

  const updatedAtMs = Date.parse(book.updatedAt);
  if (!Number.isFinite(updatedAtMs)) {
    throw new Error(`stale orderbook: invalid ${side} updatedAt ${book.updatedAt}`);
  }

  const ageMs = nowMs - updatedAtMs;
  if (ageMs < 0 || ageMs > maxAgeMs) {
    throw new Error(
      `stale orderbook: ${side} quote age ${ageMs}ms exceeds ${maxAgeMs}ms`,
    );
  }

  return {
    bestAsk: book.bestAsk,
    updatedAt: book.updatedAt,
    ageMs,
  };
}

function applyOrderbookEvent(
  cache: OrderbookCache,
  event: Record<string, unknown>,
): void {
  const eventType = asString(event.event_type);

  if (eventType === "book") {
    const book = getSideBook(cache, event);
    if (!book) return;

    const asks = Array.isArray(event.asks) ? event.asks : [];
    const bids = Array.isArray(event.bids) ? event.bids : [];
    book.bestAsk = minPrice(asks);
    book.bestBid = maxPrice(bids);
    book.updatedAt = new Date().toISOString();
    return;
  }

  if (eventType === "best_bid_ask") {
    const book = getSideBook(cache, event);
    if (!book) return;

    book.bestAsk = parseOptionalNumber(event.best_ask);
    book.bestBid = parseOptionalNumber(event.best_bid);
    book.updatedAt = new Date().toISOString();
    return;
  }

  if (eventType === "price_change") {
    const changes = Array.isArray(event.price_changes)
      ? event.price_changes
      : [event];

    for (const change of changes) {
      if (!change || typeof change !== "object") continue;
      const changeRecord = change as Record<string, unknown>;
      const book = getSideBook(cache, changeRecord) ?? getSideBook(cache, event);
      if (!book) continue;

      const bestAsk = parseOptionalNumber(changeRecord.best_ask ?? event.best_ask);
      const bestBid = parseOptionalNumber(changeRecord.best_bid ?? event.best_bid);
      if (bestAsk !== null) book.bestAsk = bestAsk;
      if (bestBid !== null) book.bestBid = bestBid;
      book.updatedAt = new Date().toISOString();
    }
  }
}

function invalidateOrderbookCache(cache: OrderbookCache): void {
  for (const book of [cache.yes, cache.no]) {
    book.bestAsk = null;
    book.bestBid = null;
    book.updatedAt = null;
  }
}

function getSideBook(
  cache: OrderbookCache,
  event: Record<string, unknown>,
): SideBook | null {
  const tokenId =
    asString(event.asset_id) ??
    asString(event.assetId) ??
    asString(event.token_id) ??
    asString(event.tokenId);

  if (tokenId === cache.yes.tokenId) return cache.yes;
  if (tokenId === cache.no.tokenId) return cache.no;
  return null;
}

function minPrice(levels: unknown[]): number | null {
  const prices = levels
    .map((level) =>
      level && typeof level === "object"
        ? parseOptionalNumber((level as Record<string, unknown>).price)
        : null,
    )
    .filter((value): value is number => value !== null);
  return prices.length > 0 ? Math.min(...prices) : null;
}

function maxPrice(levels: unknown[]): number | null {
  const prices = levels
    .map((level) =>
      level && typeof level === "object"
        ? parseOptionalNumber((level as Record<string, unknown>).price)
        : null,
    )
    .filter((value): value is number => value !== null);
  return prices.length > 0 ? Math.max(...prices) : null;
}

function parseOptionalNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function asString(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

function parseJson(value: string): unknown {
  try {
    return JSON.parse(value);
  } catch {
    return null;
  }
}
