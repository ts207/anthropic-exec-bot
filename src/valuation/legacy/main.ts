import { existsSync } from "node:fs";
import { join } from "node:path";
import { decide } from "./decision.ts";
import { loadConfig } from "./config.ts";
import { appendJsonl } from "../logging.ts";
import { watchNpmUntilValuation } from "./npmWatcher.ts";
import { getFreshBestAsk, subscribeOrderbook } from "./orderbook.ts";
import {
  fetchEventBySlug,
  getClobMarketDelayInfo,
  initializeClobClient,
  prepareFakBuyOrders,
  selectThresholdMarket,
  startWarmConnectionLoop,
  submitPreparedFakBuyOrder,
} from "./polymarket.ts";

async function main(): Promise<void> {
  const config = loadConfig();
  const decisionsLog = join(config.logsDir, "decisions.jsonl");
  const ordersLog = join(config.logsDir, "orders.jsonl");

  if (existsSync(config.tradeLockPath)) {
    throw new Error(`already traded: lock file exists at ${config.tradeLockPath}`);
  }

  const event = await fetchEventBySlug(config.eventSlug);
  const market = selectThresholdMarket(event, config.targetMarketText);
  const client = await initializeClobClient(config);
  const clobDelayInfo = await getClobMarketDelayInfo(
    client,
    market.conditionId,
    config.clobHost,
  );
  const preparedOrders = await prepareFakBuyOrders({ config, market, client });
  const orderbook = subscribeOrderbook(market);
  const stopWarmConnectionLoop = startWarmConnectionLoop(client, market);

  console.log(
    JSON.stringify(
      {
        market,
        clobDelayInfo,
        preparedOrders: preparedOrders.dryRun
          ? preparedOrders
          : {
              dryRun: false,
              yesPlan: preparedOrders.yesPlan,
              noPlan: preparedOrders.noPlan,
            },
      },
      null,
      2,
    ),
  );

  try {
    const valuation = await watchNpmUntilValuation({
      url: config.npmUrl,
      apiUrl: config.npmApiUrl,
      pollMs: config.pollMs,
      profileDir: config.npmProfileDir,
    });
    const decision = decide(valuation.valuation);
    let quote;
    try {
      quote = getFreshBestAsk(
        orderbook.cache,
        decision.side,
        config.orderbookMaxAgeMs,
      );
    } catch (error) {
      const reason = error instanceof Error ? error.message : String(error);
      const noTrade = {
        trade: false,
        skipped: "STALE_ORDERBOOK",
        staleOrderbookReason: reason,
        decision,
        valuation,
        orderbook: orderbook.cache,
        orderbookMaxAgeMs: config.orderbookMaxAgeMs,
      };
      await appendJsonl(decisionsLog, {
        asOf: valuation.asOf,
        valuation: valuation.valuation,
        raw: valuation.raw,
        side: decision.side,
        reason: decision.reason,
        dryRun: config.dryRun,
        staleOrderbookReason: reason,
        trade: false,
        skipped: "STALE_ORDERBOOK",
        orderbook: orderbook.cache,
        orderbookMaxAgeMs: config.orderbookMaxAgeMs,
      });
      console.log(JSON.stringify(noTrade, null, 2));
      return;
    }

    const orderResponse = await submitPreparedFakBuyOrder({
      config,
      market,
      side: decision.side,
      client,
      preparedOrders,
      bestAsk: quote.bestAsk,
    });

    await appendJsonl(ordersLog, {
      side: decision.side,
      bestAsk: quote.bestAsk,
      quote,
      orderResponse,
    });

    await appendJsonl(decisionsLog, {
      asOf: valuation.asOf,
      valuation: valuation.valuation,
      raw: valuation.raw,
      side: decision.side,
      reason: decision.reason,
      bestAsk: quote.bestAsk,
      quote,
      dryRun: config.dryRun,
      trade: orderResponse.accepted,
      orderResponse,
    });

    console.log(
      JSON.stringify(
        {
          trade: orderResponse.accepted,
          dryRun: config.dryRun,
          decision,
          valuation,
          bestAsk: quote.bestAsk,
          quote,
          orderbook: orderbook.cache,
          orderResponse,
        },
        null,
        2,
      ),
    );
  } finally {
    stopWarmConnectionLoop();
    orderbook.close();
  }
}

main().catch((error: unknown) => {
  console.error(error instanceof Error ? error.stack : error);
  process.exitCode = 1;
});
