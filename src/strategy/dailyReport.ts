export type DailyReportInput = {
  generatedAt: string;
  sourceFreshness?: unknown;
  forecastAudit?: unknown;
  forecastPaper?: unknown;
  fixingWatch?: unknown;
  marketAudit?: unknown;
  curveAudit?: unknown;
};

export function buildDailyReport(input: DailyReportInput): Record<string, unknown> {
  const forecast = asRecord(input.forecastAudit);
  const paper = asRecord(input.forecastPaper);
  const fixing = asRecord(input.fixingWatch);
  const market = asRecord(input.marketAudit);
  const curve = asRecord(input.curveAudit);
  const freshness = asRecord(asRecord(input.sourceFreshness).companies);
  const forecastRows = arrayOfRecords(forecast.rows);
  const watchlist = arrayOfRecords(forecast.watchlist);
  const nearBoundary = forecastRows.filter((row) => row.state === "NEAR_BOUNDARY");
  const paperSummary = asRecord(paper.summary);
  const fixingSummary = asRecord(fixing.summary);
  const marketSummary = asRecord(market.summary);
  const curveSummary = asRecord(curve.summary);
  return {
    generatedAt: input.generatedAt,
    sourceFreshness: Object.fromEntries(Object.entries(freshness).map(([company, value]) => [
      company,
      {
        freshnessState: asRecord(value).freshnessState,
        latestTapeDate: asRecord(value).latestTapeDate,
        expectedNextUpdateAt: asRecord(value).expectedNextUpdateAt,
      },
    ])),
    latestValuations: latestValuations(forecastRows),
    watchlist: watchlist.map((row) => ({
      company: row.company,
      marketSlug: row.marketSlug,
      threshold: row.threshold,
      state: row.state,
      distancePct: row.distancePct,
      yesAsk: row.yesAsk,
      modelFairPrice: row.modelFairPrice,
      edge: row.edge,
      needed: row.needed,
      freshnessState: row.freshnessState,
    })),
    nearBoundary: nearBoundary.map((row) => ({
      company: row.company,
      marketSlug: row.marketSlug,
      threshold: row.threshold,
      distancePct: row.distancePct,
      yesAsk: row.yesAsk,
      modelFairPrice: row.modelFairPrice,
      edge: row.edge,
    })),
    paperTrades: {
      openedThisRun: paperSummary.openedThisRun ?? 0,
      updatedThisRun: paperSummary.updatedThisRun ?? 0,
      openTrades: paperSummary.openTrades ?? 0,
      resolvedTrades: paperSummary.resolvedTrades ?? 0,
      totalTrades: paperSummary.totalTrades ?? 0,
      totalHypotheticalPnl: paperSummary.totalHypotheticalPnl ?? 0,
      proofBeforeLive: paperSummary.proofBeforeLive ?? null,
    },
    newlyCrossedBarriers: {
      newCrossCount: fixingSummary.newCrossCount ?? 0,
      trackedCrossCount: fixingSummary.trackedCrossCount ?? 0,
      newCrosses: arrayOfRecords(fixing.newCrosses).map((row) => ({
        company: row.company,
        marketSlug: row.marketSlug,
        threshold: row.threshold,
        sourceDate: row.sourceDate,
        firstSeenAt: row.firstSeenAt,
      })),
    },
    strictStaleCrossedLegs: {
      count: marketSummary.strictCrossedLegCount ?? 0,
      rows: arrayOfRecords(market.rows),
    },
    hardCurveViolations: {
      count: curveSummary.hardMonotonicityCount ?? 0,
      rows: arrayOfRecords(curve.monotonicityViolations),
    },
    liveReadiness: {
      forecastLive: false,
      sourceConfirmedLive: false,
      reason: "paper_trade_sample_size_insufficient",
    },
    summary: {
      watchlistCount: watchlist.length,
      nearBoundaryCount: nearBoundary.length,
      paperOpenTrades: Number(paperSummary.openTrades ?? 0),
      newlyCrossedCount: Number(fixingSummary.newCrossCount ?? 0),
      strictStaleCrossedCount: Number(marketSummary.strictCrossedLegCount ?? 0),
      hardCurveViolationCount: Number(curveSummary.hardMonotonicityCount ?? 0),
    },
  };
}

function latestValuations(rows: Record<string, unknown>[]): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const row of rows) {
    const company = typeof row.company === "string" ? row.company : undefined;
    if (!company || result[company]) continue;
    result[company] = {
      latestValuation: row.latestValuation,
      latestDate: row.latestDate,
      maxEligibleValuation: row.maxEligibleValuation,
      maxEligibleDate: row.maxEligibleDate,
    };
  }
  return result;
}

function arrayOfRecords(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? value.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object" && !Array.isArray(item)))
    : [];
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
