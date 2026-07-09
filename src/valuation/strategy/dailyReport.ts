export type DailyReportInput = {
  generatedAt: string;
  sourceFreshness?: unknown;
  forecastAudit?: unknown;
  forecastPaper?: unknown;
  fixingWatch?: unknown;
  marketAudit?: unknown;
  curveAudit?: unknown;
  entryAudit?: unknown;
  ladderPaper?: unknown;
  discovery?: unknown;
};

export function buildDailyReport(input: DailyReportInput): Record<string, unknown> {
  const forecast = asRecord(input.forecastAudit);
  const paper = asRecord(input.forecastPaper);
  const fixing = asRecord(input.fixingWatch);
  const market = asRecord(input.marketAudit);
  const curve = asRecord(input.curveAudit);
  const entry = asRecord(input.entryAudit);
  const ladderPaper = asRecord(input.ladderPaper);
  const discovery = asRecord(input.discovery);
  const freshness = asRecord(asRecord(input.sourceFreshness).companies);
  const forecastRows = arrayOfRecords(forecast.rows);
  const watchlist = arrayOfRecords(forecast.watchlist);
  const nearBoundary = forecastRows.filter((row) => row.state === "NEAR_BOUNDARY");
  const paperSummary = asRecord(paper.summary);
  const fixingSummary = asRecord(fixing.summary);
  const marketSummary = asRecord(market.summary);
  const curveSummary = asRecord(curve.summary);
  const entrySummary = asRecord(entry.summary);
  const ladderPaperSummary = asRecord(ladderPaper.summary);
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
    ladderEntries: {
      summary: entrySummary,
      actionablePlans: arrayOfRecords(entry.actionablePlans).map((row) => ({
        company: row.company,
        marketSlug: row.marketSlug,
        threshold: row.threshold,
        entryMode: row.entryMode,
        direction: row.direction,
        yesTokenId: row.yesTokenId,
        noTokenId: row.noTokenId,
        sourceConfirmed: row.sourceConfirmed,
        distancePct: row.distancePct,
        yesAsk: row.yesAsk,
        yesBid: row.yesBid,
        modelFair: row.modelFair,
        passiveBidPrice: row.passiveBidPrice,
        paperEligible: row.paperEligible,
        liveEligible: row.liveEligible,
        activation: row.activation,
        ladderContext: row.ladderContext,
        blockers: row.blockers,
        reason: row.reason,
      })),
    },
    ladderPaper: {
      summary: ladderPaperSummary,
      baseSizeUsd: ladderPaper.baseSizeUsd ?? ladderPaper.sizeUsd ?? null,
      sizeMultipliers: ladderPaper.sizeMultipliers ?? null,
      opened: arrayOfRecords(ladderPaper.opened).map(ladderPaperOrderSummary),
      filled: arrayOfRecords(ladderPaper.filled).map(ladderPaperOrderSummary),
      blocked: arrayOfRecords(ladderPaper.blocked).map(ladderPaperBlockSummary),
      workingOrders: arrayOfRecords(ladderPaper.workingOrders).map(ladderPaperOrderSummary),
      filledOrders: arrayOfRecords(ladderPaper.filledOrders).map(ladderPaperOrderSummary),
      resolvedOrders: arrayOfRecords(ladderPaper.resolvedOrders).map(ladderPaperOrderSummary),
    },
    discovery: {
      discoveredEventCount: discovery.discoveredEventCount ?? 0,
      coverage: discovery.coverage ?? null,
      gammaPagesScanned: discovery.gammaPagesScanned ?? 0,
      gammaEventsScanned: discovery.gammaEventsScanned ?? 0,
      gammaCrawlExhausted: discovery.gammaCrawlExhausted ?? false,
      maxPagesReached: discovery.maxPagesReached ?? false,
      accessIssues: discovery.accessIssues ?? [],
    },
    liveReadiness: {
      forecastLive: false,
      sourceConfirmedLive: Number(entrySummary.liveEligibleCount ?? 0) > 0,
      reason: "paper_trade_sample_size_insufficient",
    },
    summary: {
      watchlistCount: watchlist.length,
      nearBoundaryCount: nearBoundary.length,
      paperOpenTrades: Number(paperSummary.openTrades ?? 0),
      newlyCrossedCount: Number(fixingSummary.newCrossCount ?? 0),
      strictStaleCrossedCount: Number(marketSummary.strictCrossedLegCount ?? 0),
      hardCurveViolationCount: Number(curveSummary.hardMonotonicityCount ?? 0),
      passiveBidPlanCount: Number(entrySummary.nearBoundaryPassiveBidCount ?? 0) + Number(entrySummary.farOptionalityBidCount ?? 0) + Number(entrySummary.curveRepairBidCount ?? 0),
      sourceConfirmedTakerPlanCount: Number(entrySummary.strictSourceConfirmedTakerCount ?? 0),
      ladderPaperOpenedThisRun: Number(ladderPaperSummary.openedThisRun ?? 0),
      ladderPaperFilledThisRun: Number(ladderPaperSummary.filledThisRun ?? 0),
    },
  };
}

function ladderPaperOrderSummary(row: Record<string, unknown>): Record<string, unknown> {
  return {
    company: row.company,
    eventSlug: row.eventSlug,
    marketSlug: row.marketSlug,
    pairedMarketSlug: row.pairedMarketSlug,
    yesTokenId: row.yesTokenId,
    noTokenId: row.noTokenId,
    pairedNoTokenId: row.pairedNoTokenId,
    threshold: row.threshold,
    pairedThreshold: row.pairedThreshold,
    deadline: row.deadline,
    entryMode: row.entryMode,
    sourceConfirmed: row.sourceConfirmed,
    passiveBidPrice: row.passiveBidPrice,
    modelFair: row.modelFair,
    requiredEdge: row.requiredEdge,
    sizeUsd: row.sizeUsd,
    status: row.status,
    filledAt: row.filledAt,
    fillPrice: row.fillPrice,
    currentMarkPrice: row.currentMarkPrice,
    finalResolution: row.finalResolution,
    hypotheticalPnl: row.hypotheticalPnl,
    cancelReason: row.cancelReason,
    reason: row.reason,
  };
}

function ladderPaperBlockSummary(row: Record<string, unknown>): Record<string, unknown> {
  return {
    company: row.company,
    eventSlug: row.eventSlug,
    marketSlug: row.marketSlug,
    deadline: row.deadline,
    entryMode: row.entryMode,
    reason: row.reason,
    sizeUsd: row.sizeUsd,
    usedUsd: row.usedUsd,
    capUsd: row.capUsd,
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
