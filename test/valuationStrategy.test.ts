import assert from "node:assert/strict";
import { mkdtemp } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import test from "node:test";
import { normalizeConfig } from "../src/strategy/valuationConfig.ts";
import { parseNpmEvidence, withEligibleMax } from "../src/strategy/npmValuationSource.ts";
import { parseGammaEvent, parseThreshold, parseValuationLegs } from "../src/strategy/marketParser.ts";
import { decideThresholdLeg } from "../src/strategy/valuationDecision.ts";
import { curveMonotonicityCandidates } from "../src/strategy/curveArbitrage.ts";
import { calendarDominanceCandidates } from "../src/strategy/calendarArbitrage.ts";
import { rankingAlertCandidates } from "../src/strategy/rankingSimulator.ts";
import type { BookQuote, CurvePoint, EventConfig, StrategyConfig, ValuationLeg } from "../src/strategy/signalTypes.ts";
import { liveBlockers } from "../src/valuationStrategy.ts";
import { betaProbeMetadata, validatePostedProbeForCandidate } from "../src/strategy/probeValidation.ts";
import { probePath, writeJson } from "../src/strategy/stateStore.ts";
import { buildMarketAuditRow, monotonicityAudits } from "../src/strategy/marketAudit.ts";
import { updateFixingWatch } from "../src/strategy/fixingWatch.ts";
import { buildNpmBarrierForecasts, buildSourceFreshnessSnapshot, monteCarloTouchProbability, pCrossTomorrow, sourceFreshnessMap, tapeStats } from "../src/strategy/npmBarrierForecast.ts";
import { isPaperOpenTrigger, updateForecastPaperTrades } from "../src/strategy/forecastPaper.ts";
import { expectedNpmUpdateAt, phaseForNow } from "../src/strategy/automationSchedule.ts";
import { runAutomationCycle } from "../src/strategy/valuationAutomation.ts";

test("config loader applies safe low-risk defaults", () => {
  const config = testConfig();
  assert.equal(config.mode, "alert_only");
  assert.equal(config.pollMs, 30_000);
  assert.equal(config.signalMultipliers.SOURCE_CONFIRMED_YES, 1);
  assert.equal(config.signalMultipliers.RANKING_INCONSISTENCY_ALERT, 0);
});

test("threshold parser accepts B/T/M suffixes and rejects malformed dollar amount", () => {
  assert.equal(parseThreshold("Will OpenAI hit $900B by July 31?")?.value, 900_000_000_000);
  assert.equal(parseThreshold("Will Anthropic hit HIGH $1.15T by July 31?")?.value, 1_150_000_000_000);
  assert.equal(parseThreshold("Will Perplexity hit $20B by July 31?")?.value, 20_000_000_000);
  assert.equal(parseThreshold("Will Anthropic hit LOW $1.0T by July 31?")?.value, 1_000_000_000_000);
  assert.equal(parseThreshold("Will Anthropic hit LOW $800 by July 31?"), null);
});

test("Gamma fixture parser keeps LOW label but relies on rule threshold language", () => {
  const eventConfig = thresholdEvent("Anthropic");
  const event = parseGammaEvent({
    slug: "anthropic-event",
    title: "Will Anthropic's valuation hit by July 31?",
    description: "This market resolves Yes if NPM Price reports Anthropic reaches or exceeds the listed amount.",
    markets: [
      marketFixture("Will Anthropic's valuation hit LOW $900B by July 31?", "low-900b"),
      marketFixture("Will Anthropic's valuation hit LOW $800 by July 31?", "low-800"),
    ],
  });
  const legs = parseValuationLegs(event, eventConfig);
  assert.equal(legs[0]?.label, "LOW");
  assert.equal(legs[0]?.parseStatus, "ok");
  assert.equal(legs[0]?.threshold, 900_000_000_000);
  assert.equal(legs[1]?.parseStatus, "malformed_threshold");
});

test("NPM parser stores latest tape and eligible-window max valuation", () => {
  const evidence = parseNpmEvidence({
    company: { name: "Anthropic" },
    latest_tape_d: { date: "2026-07-03", implied_valuation: 1_080_000_000_000, price: 42 },
    tape_d_prices: [
      { date: "2026-06-28", implied_valuation: 1_200_000_000_000 },
      { date: "2026-07-01", implied_valuation: 1_105_000_000_000 },
      { date: "2026-07-03", implied_valuation: 1_080_000_000_000 },
    ],
  }, { name: "Anthropic", npmCompanyId: "company-a" });
  const windowed = withEligibleMax(evidence, "2026-06-29T00:00:00Z", "2026-08-01T03:59:59Z");
  assert.equal(windowed.latestValuation, 1_080_000_000_000);
  assert.equal(windowed.maxEligibleValuation, 1_105_000_000_000);
  assert.equal(windowed.maxEligibleDate, "2026-07-01");
});

test("source-confirmed crossed threshold creates BUY YES candidate", () => {
  const config = normalizeConfig({
    mode: "live",
    events: [thresholdEvent("Anthropic")],
    companies: [{ name: "Anthropic", npmCompanyId: "company-a" }],
  });
  const leg = legFixture({ threshold: 1_100_000_000_000 });
  const evidence = parseNpmEvidence({
    latest_tape_d: { date: "2026-07-01", implied_valuation: 1_101_000_000_000 },
  }, { name: "Anthropic", npmCompanyId: "company-a" });
  const candidate = decideThresholdLeg(leg, evidence, quoteFixture(0.81), config);
  assert.equal(candidate.signalType, "SOURCE_CONFIRMED_YES");
  assert.equal(candidate.status, "candidate");
  assert.equal(candidate.liveAllowed, true);
  assert.equal(candidate.orderUsd > 0, true);
  assert.equal(candidate.distancePct !== undefined && candidate.distancePct < 0.001, true);
  assert.equal(candidate.confidenceScore, 10);
  assert.equal(candidate.edgeScore > 0, true);
  assert.deepEqual(candidate.orderTemplate, {
    tokenId: "yes-token",
    side: "BUY",
    outcome: "YES",
    orderType: "FAK",
    amountUsd: candidate.orderUsd,
    maxPrice: 0.95,
    posted: false,
  });
});

test("non-crossed threshold no-actions unless drift edge exists", () => {
  const config = testConfig();
  const leg = legFixture({ threshold: 1_100_000_000_000 });
  const evidence = parseNpmEvidence({
    latest_tape_d: { date: "2026-07-01", implied_valuation: 1_050_000_000_000 },
  }, { name: "Anthropic", npmCompanyId: "company-a" });
  const candidate = decideThresholdLeg(leg, evidence, quoteFixture(0.5), config);
  assert.equal(candidate.signalType, "NO_ACTION");
  assert.equal(candidate.liveAllowed, false);
});

test("closed and malformed legs do not trade", () => {
  const config = testConfig();
  const closed = decideThresholdLeg(
    { ...legFixture({ threshold: 1_100_000_000_000 }), closed: true },
    undefined,
    quoteFixture(0.5),
    config,
  );
  const malformed = decideThresholdLeg(
    { ...legFixture({ threshold: undefined }), parseStatus: "malformed_threshold" },
    undefined,
    quoteFixture(0.5),
    config,
  );
  assert.equal(closed.status, "skip");
  assert.equal(malformed.signalType, "STALE_SOURCE_ALERT");
  assert.equal(malformed.liveAllowed, false);
});

test("hard monotonicity violation buys underpriced lower threshold YES", () => {
  const config = testConfig();
  const lower = legFixture({ threshold: 900_000_000_000, marketSlug: "lower" });
  const higher = legFixture({ threshold: 950_000_000_000, marketSlug: "higher" });
  const quotes = new Map([
    ["lower", quoteFixture(0.52)],
    ["higher", quoteFixture(0.64)],
  ]);
  const candidates = curveMonotonicityCandidates([
    { leg: lower, yesAsk: 0.52 },
    { leg: higher, yesAsk: 0.64 },
  ], quotes, config);
  assert.equal(candidates.length, 1);
  assert.equal(candidates[0]?.signalType, "CURVE_MONOTONICITY_YES");
  assert.equal(candidates[0]?.marketSlug, "lower");
});

test("calendar dominance violation buys later deadline YES", () => {
  const config = testConfig();
  const july = legFixture({ threshold: 900_000_000_000, marketSlug: "july", deadlineIso: "2026-08-01T03:59:59Z" });
  const dec = legFixture({ threshold: 900_000_000_000, marketSlug: "dec", deadlineIso: "2027-01-01T04:59:59Z" });
  const quotes = new Map([
    ["july", quoteFixture(0.7)],
    ["dec", quoteFixture(0.58)],
  ]);
  const candidates = calendarDominanceCandidates([
    { leg: july, yesAsk: 0.7 },
    { leg: dec, yesAsk: 0.58 },
  ], quotes, config);
  assert.equal(candidates.length, 1);
  assert.equal(candidates[0]?.signalType, "CALENDAR_DOMINANCE_YES");
  assert.equal(candidates[0]?.marketSlug, "dec");
});

test("ranking market inconsistency is alert-only", () => {
  const config = testConfig();
  const leg = rankingLegFixture("Anthropic", 1);
  const evidence = parseNpmEvidence({
    latest_tape_d: { date: "2026-07-01", implied_valuation: 1_200_000_000_000 },
  }, { name: "Anthropic", npmCompanyId: "company-a" });
  const candidates = rankingAlertCandidates([leg], new Map([["Anthropic", evidence]]), new Map([[leg.marketSlug, quoteFixture(0.4)]]), config);
  assert.equal(candidates.length, 1);
  assert.equal(candidates[0]?.signalType, "RANKING_INCONSISTENCY_ALERT");
  assert.equal(candidates[0]?.liveAllowed, false);
});

test("live blocker audit explains why candidate is not live-eligible", async () => {
  const config = testConfig();
  const leg = legFixture({ threshold: 1_100_000_000_000 });
  const evidence = parseNpmEvidence({
    latest_tape_d: { date: "2026-07-01", implied_valuation: 1_101_000_000_000 },
  }, { name: "Anthropic", npmCompanyId: "company-a" });
  const candidate = decideThresholdLeg(leg, evidence, quoteFixture(0.81), config);
  const blockers = await liveBlockers(candidate, config, "test-config-hash");
  assert.equal(blockers.includes("operator_mode_alert_only"), true);
  assert.equal(blockers.includes("missing_live_config_ack"), true);
  assert.equal(blockers.includes("missing_posted_probe_success"), true);
  assert.equal(blockers.includes("posting_env_not_armed"), true);
});

test("posted probe validation rejects malformed probe files", async () => {
  const stateDir = await mkdtemp(join(tmpdir(), "valuation-probe-test-"));
  const config = normalizeConfig({
    mode: "live",
    stateDir,
    events: [thresholdEvent("Anthropic")],
    companies: [{ name: "Anthropic", npmCompanyId: "company-a" }],
  });
  const candidate = decideThresholdLeg(
    legFixture({ threshold: 1_100_000_000_000 }),
    parseNpmEvidence({ latest_tape_d: { date: "2026-07-01", implied_valuation: 1_101_000_000_000 } }, { name: "Anthropic", npmCompanyId: "company-a" }),
    quoteFixture(0.81),
    config,
  );
  await writeJson(probePath(config, candidate.marketSlug), {
    ok: true,
    marketSlug: candidate.marketSlug,
    tokenId: "wrong-token",
    timestamp: new Date().toISOString(),
  });
  const validation = await validatePostedProbeForCandidate(config, candidate);
  assert.equal(validation.ok, false);
  assert.equal(validation.blockers.includes("probe_token_mismatch"), true);
  assert.equal(validation.blockers.includes("probe_side_mismatch"), true);
  assert.equal(validation.blockers.includes("probe_order_type_mismatch"), true);
  assert.equal(validation.blockers.includes("probe_sdk_mismatch"), true);
});

test("posted probe validation accepts current beta BUY FAK probe metadata", async () => {
  const stateDir = await mkdtemp(join(tmpdir(), "valuation-probe-test-"));
  const config = normalizeConfig({
    mode: "live",
    stateDir,
    events: [thresholdEvent("Anthropic")],
    companies: [{ name: "Anthropic", npmCompanyId: "company-a" }],
  });
  const candidate = decideThresholdLeg(
    legFixture({ threshold: 1_100_000_000_000 }),
    parseNpmEvidence({ latest_tape_d: { date: "2026-07-01", implied_valuation: 1_101_000_000_000 } }, { name: "Anthropic", npmCompanyId: "company-a" }),
    quoteFixture(0.81),
    config,
  );
  await writeJson(probePath(config, candidate.marketSlug), {
    ok: true,
    marketSlug: candidate.marketSlug,
    tokenId: candidate.yesTokenId,
    timestamp: new Date().toISOString(),
    ...betaProbeMetadata(),
  });
  const validation = await validatePostedProbeForCandidate(config, candidate);
  assert.equal(validation.ok, true);
  assert.deepEqual(validation.blockers, []);
});

test("monotonicity audit distinguishes bid-backed hard violation from ask-only noise", () => {
  const config = testConfig();
  const lower = legFixture({
    threshold: 900_000_000_000,
    marketSlug: "lower",
    ruleHash: "lower-specific-rule-hash",
    ruleFamilyHash: "same-rule-family",
  });
  const higher = legFixture({
    threshold: 950_000_000_000,
    marketSlug: "higher",
    ruleHash: "higher-specific-rule-hash",
    ruleFamilyHash: "same-rule-family",
  });
  const hard = monotonicityAudits([
    { leg: lower, yesAsk: 0.52 },
    { leg: higher, yesAsk: 0.7 },
  ], new Map([
    ["lower", quoteFixture(0.52, 0.5)],
    ["higher", quoteFixture(0.7, 0.62)],
  ]), config, new Date("2026-07-05T00:00:01Z"));
  assert.equal(hard[0]?.violationTier, "HARD_CROSS_MARKET_BID_VIOLATION");
  assert.equal(hard[0]?.tradeableBuyOnly, true);

  const soft = monotonicityAudits([
    { leg: lower, yesAsk: 0.52 },
    { leg: higher, yesAsk: 0.7 },
  ], new Map([
    ["lower", quoteFixture(0.52, 0.5)],
    ["higher", quoteFixture(0.7, 0.45)],
  ]), config, new Date("2026-07-05T00:00:01Z"));
  assert.equal(soft[0]?.violationTier, "SOFT_ASK_ONLY_VIOLATION");
  assert.equal(soft[0]?.tradeableBuyOnly, false);
});

test("market audit newly-crossed state ignores pre-window tape points", () => {
  const config = testConfig();
  const leg = legFixture({ threshold: 1_100_000_000_000 });
  const evidence = withEligibleMax(parseNpmEvidence({
    latest_tape_d: { date: "2026-07-01", implied_valuation: 1_101_000_000_000 },
    tape_d_prices: [
      { date: "2026-06-28", implied_valuation: 1_200_000_000_000 },
      { date: "2026-06-30", implied_valuation: 1_090_000_000_000 },
      { date: "2026-07-01", implied_valuation: 1_101_000_000_000 },
    ],
  }, { name: "Anthropic", npmCompanyId: "company-a" }), "2026-06-29T00:00:00Z", "2026-08-01T03:59:59Z");
  const row = buildMarketAuditRow({
    leg,
    evidence,
    quote: quoteFixture(0.81, 0.78),
    config,
    now: new Date("2026-07-02T00:00:00Z"),
  });
  assert.equal(row.previousMaxEligibleValuation, 1_090_000_000_000);
  assert.equal(row.state, "NEWLY_CROSSED");
});

test("market audit row classifies source-confirmed stale crossed leg", () => {
  const config = testConfig();
  const leg = legFixture({ threshold: 1_100_000_000_000 });
  const evidence = withEligibleMax(parseNpmEvidence({
    latest_tape_d: { date: "2026-07-01", implied_valuation: 1_101_000_000_000 },
    tape_d_prices: [
      { date: "2026-06-30", implied_valuation: 1_090_000_000_000 },
      { date: "2026-07-01", implied_valuation: 1_101_000_000_000 },
    ],
  }, { name: "Anthropic", npmCompanyId: "company-a" }), "2026-06-29T00:00:00Z", "2026-08-01T03:59:59Z");
  const row = buildMarketAuditRow({
    leg,
    evidence,
    quote: quoteFixture(0.81, 0.78),
    config,
    now: new Date("2026-07-02T00:00:00Z"),
  });
  assert.equal(row.state, "NEWLY_CROSSED");
  assert.equal(row.crossedQuality, "SOURCE_CONFIRMED_AND_STALE");
  assert.equal(row.depthUnderCap > 0, true);
  assert.equal(row.tradeBand === "tradeable" || row.tradeBand === "maybe", true);
});

test("fixing watch records first-seen and later replay observations", () => {
  const row = marketAuditRowFixture({
    state: "NEWLY_CROSSED",
    maxEligibleValuation: 1_101_000_000_000,
    previousMaxEligibleValuation: 1_090_000_000_000,
    yesAsk: 0.81,
  });
  const first = updateFixingWatch(
    [row],
    { generatedAt: "2026-07-01T00:00:00Z", rows: [{ ...row, maxEligibleValuation: 1_090_000_000_000 }] },
    { version: 1, updatedAt: "2026-07-01T00:00:00Z", crosses: {} },
    new Date("2026-07-02T00:00:00Z"),
  );
  assert.equal(first.newCrosses.length, 1);
  assert.equal(first.newCrosses[0]?.observations[0]?.label, "first_seen");
  assert.equal(first.newCrosses[0]?.observations[0]?.fakUnderCapWouldFill, true);

  const later = updateFixingWatch(
    [{ ...row, yesAsk: 0.96, settlementEdge: 0.04, crossedQuality: "SOURCE_CONFIRMED_BUT_ALREADY_PRICED", tradeBand: "alert" }],
    first.snapshot,
    first.state,
    new Date("2026-07-02T00:00:31Z"),
  );
  const tracked = later.state.crosses[first.newCrosses[0]?.key ?? ""];
  assert.equal(tracked?.observations.some((obs) => obs.label === "plus_5s"), true);
  assert.equal(tracked?.observations.some((obs) => obs.label === "plus_30s"), true);
  assert.equal(later.missedEdgeReport[0]?.repricedByLatest, 0.1499999999999999);
});

test("fixing watch baselines first run unless replay is explicit", () => {
  const row = marketAuditRowFixture({ state: "NEWLY_CROSSED" });
  const baseline = updateFixingWatch(
    [row],
    null,
    { version: 1, updatedAt: "2026-07-01T00:00:00Z", crosses: {} },
    new Date("2026-07-02T00:00:00Z"),
  );
  assert.equal(baseline.newCrosses.length, 0);

  const replay = updateFixingWatch(
    [row],
    null,
    { version: 1, updatedAt: "2026-07-01T00:00:00Z", crosses: {} },
    new Date("2026-07-02T00:00:00Z"),
    { replayExisting: true },
  );
  assert.equal(replay.newCrosses.length, 1);
});

test("barrier forecast computes NPM tape stats and crossing probability", () => {
  const evidence = parseNpmEvidence({
    latest_tape_d: { date: "2026-07-04", implied_valuation: 995 },
    tape_d_prices: [
      { date: "2026-07-01", implied_valuation: 950 },
      { date: "2026-07-02", implied_valuation: 970 },
      { date: "2026-07-03", implied_valuation: 985 },
      { date: "2026-07-04", implied_valuation: 995 },
    ],
  }, { name: "Anthropic", npmCompanyId: "company-a" });
  const stats = tapeStats(evidence);
  assert.equal(stats.returnCount, 3);
  assert.equal(stats.recent3DayAvgDailyDrift > 0, true);
  assert.equal(pCrossTomorrow(995, 1000, stats.meanDailyLogReturn, stats.dailyVol) > 0.5, true);
  assert.equal(monteCarloTouchProbability({
    latestValuation: 995,
    threshold: 1000,
    mu: stats.meanDailyLogReturn,
    sigma: stats.dailyVol,
    days: 10,
    paths: 500,
    seed: "forecast-test",
  }) > 0.5, true);
});

test("barrier forecast creates alert-only near-boundary candidate", () => {
  const config = testConfig();
  const leg = legFixture({ threshold: 1_000, marketSlug: "forecast-market" });
  const evidence = withEligibleMax(parseNpmEvidence({
    latest_tape_d: { date: "2026-07-04", implied_valuation: 995 },
    tape_d_prices: [
      { date: "2026-07-01", implied_valuation: 950 },
      { date: "2026-07-02", implied_valuation: 970 },
      { date: "2026-07-03", implied_valuation: 985 },
      { date: "2026-07-04", implied_valuation: 995 },
    ],
  }, { name: "Anthropic", npmCompanyId: "company-a" }), "2026-06-29T00:00:00Z", "2026-08-01T03:59:59Z");
  const quote = quoteFixture(0.35, 0.33);
  const sourceFreshness = buildSourceFreshnessSnapshot({
    evidenceByCompany: new Map([["Anthropic", evidence]]),
    now: new Date("2026-07-04T12:00:00Z"),
  });
  const row = marketAuditRowFixture({
    marketSlug: "forecast-market",
    threshold: 1_000,
    state: "NEAR_BOUNDARY",
    latestValuation: 995,
    latestDate: "2026-07-04",
    maxEligibleValuation: 995,
    maxEligibleDate: "2026-07-04",
    yesAsk: 0.35,
    yesBid: 0.33,
    depthUnderCap: 250,
  });
  const forecasts = buildNpmBarrierForecasts({
    legs: [leg],
    evidenceByCompany: new Map([["Anthropic", evidence]]),
    quotes: new Map([["forecast-market", quote]]),
    marketRows: [row],
    sourceFreshnessByCompany: sourceFreshnessMap(sourceFreshness),
    config,
    now: new Date("2026-07-04T12:00:00Z"),
    simulations: 800,
  });
  assert.equal(forecasts[0]?.signalType, "NPM_MULTI_DAY_BARRIER_FORECAST_YES");
  assert.equal(forecasts[0]?.liveEligible, false);
  assert.equal((forecasts[0]?.edge ?? 0) >= 0.12, true);
});

test("barrier forecast blocks stale source data", () => {
  const config = testConfig();
  const leg = legFixture({ threshold: 1_000, marketSlug: "forecast-market" });
  const evidence = withEligibleMax(parseNpmEvidence({
    latest_tape_d: { date: "2026-07-01", implied_valuation: 995 },
    tape_d_prices: [
      { date: "2026-06-29", implied_valuation: 950 },
      { date: "2026-06-30", implied_valuation: 970 },
      { date: "2026-07-01", implied_valuation: 995 },
    ],
  }, { name: "Anthropic", npmCompanyId: "company-a" }), "2026-06-29T00:00:00Z", "2026-08-01T03:59:59Z");
  const previousFreshness = buildSourceFreshnessSnapshot({
    evidenceByCompany: new Map([["Anthropic", evidence]]),
    now: new Date("2026-07-02T12:00:00Z"),
  });
  const sourceFreshness = buildSourceFreshnessSnapshot({
    evidenceByCompany: new Map([["Anthropic", evidence]]),
    previous: previousFreshness,
    now: new Date("2026-07-06T12:00:00Z"),
  });
  const forecasts = buildNpmBarrierForecasts({
    legs: [leg],
    evidenceByCompany: new Map([["Anthropic", evidence]]),
    quotes: new Map([["forecast-market", quoteFixture(0.35, 0.33)]]),
    marketRows: [marketAuditRowFixture({
      marketSlug: "forecast-market",
      threshold: 1_000,
      state: "NEAR_BOUNDARY",
      depthUnderCap: 250,
    })],
    sourceFreshnessByCompany: sourceFreshnessMap(sourceFreshness),
    config,
    now: new Date("2026-07-06T12:00:00Z"),
    simulations: 200,
  });
  assert.equal(forecasts[0]?.signalType, "NO_FORECAST_EDGE");
  assert.equal(forecasts[0]?.reason, "stale_endpoint_blocked");
  assert.equal(forecasts[0]?.freshnessState, "STALE_ENDPOINT");
});

test("forecast paper opens loose paper-only watchlist trigger", () => {
  const row = forecastRowFixture({
    signalType: "NO_FORECAST_EDGE",
    distancePct: 0.02,
    modelFairPrice: 0.62,
    pTouchByDeadline: 0.62,
    yesAsk: 0.55,
    edge: 0.07,
    confidenceScore: 0.55,
  });
  assert.equal(isPaperOpenTrigger(row), true);
  const update = updateForecastPaperTrades({
    previous: { version: 1, updatedAt: "2026-07-06T00:00:00Z", trades: [] },
    forecasts: [row],
    now: new Date("2026-07-06T17:00:00Z"),
    sizeUsd: 1,
  });
  assert.equal(update.opened.length, 1);
  assert.equal(update.opened[0]?.paperTrigger, "paper_watchlist");
  assert.equal(update.opened[0]?.entryPrice, 0.55);
  assert.equal(update.metrics.totalTrades, 1);
});

test("forecast paper updates after fixing and scores resolved touch", () => {
  const opened = updateForecastPaperTrades({
    previous: { version: 1, updatedAt: "2026-07-06T00:00:00Z", trades: [] },
    forecasts: [forecastRowFixture({
      signalType: "NPM_MULTI_DAY_BARRIER_FORECAST_YES",
      latestDate: "2026-07-06",
      latestValuation: 990,
      maxEligibleValuation: 990,
      distancePct: 0.01,
      modelFairPrice: 0.72,
      pTouchByDeadline: 0.72,
      yesAsk: 0.55,
      edge: 0.17,
      confidenceScore: 0.74,
    })],
    now: new Date("2026-07-06T17:00:00Z"),
    sizeUsd: 1,
  });
  const updated = updateForecastPaperTrades({
    previous: opened.state,
    forecasts: [forecastRowFixture({
      latestDate: "2026-07-07",
      latestValuation: 1_005,
      maxEligibleValuation: 1_005,
      distancePct: -0.005,
      yesAsk: 0.93,
    })],
    now: new Date("2026-07-07T17:00:00Z"),
    sizeUsd: 1,
  });
  assert.equal(updated.updated.length, 1);
  assert.equal(updated.updated[0]?.status, "resolved");
  assert.equal(updated.updated[0]?.thresholdTouched, true);
  assert.equal(updated.updated[0]?.finalResolution, true);
  assert.equal(updated.updated[0]?.brierScore, (0.72 - 1) ** 2);
  assert.equal(updated.metrics.resolvedTrades, 1);
  assert.equal(updated.metrics.totalHypotheticalPnl, 0.8182);
});

test("automation schedule resolves expected NPM fixing phases", () => {
  const expected = new Date("2026-07-06T17:00:00Z");
  assert.equal(phaseForNow(new Date("2026-07-06T16:15:00Z"), expected), "PRE_FIXING_PREP");
  assert.equal(phaseForNow(new Date("2026-07-06T16:55:00Z"), expected), "FIXING_WINDOW");
  assert.equal(phaseForNow(new Date("2026-07-06T17:30:00Z"), expected), "POST_FIXING_REVIEW");
  assert.equal(phaseForNow(new Date("2026-07-06T20:00:00Z"), expected), "LOW_FREQUENCY_MONITOR");
  assert.equal(expectedNpmUpdateAt(new Date("2026-07-06T18:30:00Z")).toISOString(), "2026-07-07T17:00:00.000Z");
});

test("automation dry run lists phase tasks without execution", async () => {
  const cycle = await runAutomationCycle({
    now: new Date("2026-07-06T17:00:00Z"),
    dryRun: true,
    runTask: async () => {
      throw new Error("should not run");
    },
  });
  assert.equal(cycle.phase, "FIXING_WINDOW");
  assert.deepEqual(cycle.tasks, ["fixing-watch", "market-audit-strict", "forecast-paper"]);
  assert.equal(cycle.results.every((result) => result.dryRun === true), true);
});

function testConfig(): StrategyConfig {
  return normalizeConfig({
    events: [thresholdEvent("Anthropic")],
    companies: [{ name: "Anthropic", npmCompanyId: "company-a" }],
  });
}

function thresholdEvent(companyName: string): EventConfig {
  return {
    slug: "test-event",
    kind: "threshold",
    companyName,
    deadlineIso: "2026-08-01T03:59:59Z",
    marketWindowStartIso: "2026-06-29T00:00:00Z",
  };
}

function marketFixture(question: string, slug: string): Record<string, unknown> {
  return {
    question,
    slug,
    outcomes: "[\"Yes\",\"No\"]",
    clobTokenIds: "[\"yes-token\",\"no-token\"]",
    active: true,
    closed: false,
    acceptingOrders: true,
    liquidityNum: 500,
  };
}

function legFixture(overrides: Partial<ValuationLeg>): ValuationLeg {
  return {
    eventSlug: "event",
    marketSlug: overrides.marketSlug ?? "market",
    question: "Will Anthropic's valuation hit $1.1T by July 31?",
    eventKind: "threshold",
    company: "Anthropic",
    deadlineIso: overrides.deadlineIso ?? "2026-08-01T03:59:59Z",
    marketWindowStartIso: "2026-06-29T00:00:00Z",
    threshold: overrides.threshold,
    thresholdText: "$1.1T",
    yesTokenId: "yes-token",
    noTokenId: "no-token",
    active: true,
    closed: false,
    acceptingOrders: true,
    liquidity: 500,
    ruleText: "reaches or exceeds the listed amount",
    ruleHash: "rule-hash",
    ruleFamilyHash: "rule-family-hash",
    parseStatus: "ok",
    ...overrides,
  };
}

function rankingLegFixture(company: string, ranking: 1 | 2 | 3): ValuationLeg {
  return {
    ...legFixture({ threshold: undefined, marketSlug: `${company}-ranking` }),
    eventKind: "ranking",
    company,
    label: "RANKING",
    ranking,
    parseStatus: "ok",
  };
}

function quoteFixture(bestAsk: number, bestBid = Math.max(0, bestAsk - 0.03)): BookQuote {
  return {
    tokenId: "yes-token",
    bestBid,
    bestAsk,
    spread: Math.max(0, bestAsk - bestBid),
    liquidity: 500,
    fetchedAt: "2026-07-05T00:00:00Z",
    bids: [{ price: bestBid, size: 100 }],
    asks: [
      { price: bestAsk, size: 100 },
      { price: Math.min(0.99, bestAsk + 0.02), size: 200 },
    ],
  };
}

function marketAuditRowFixture(overrides: Partial<ReturnType<typeof buildMarketAuditRow>> = {}): ReturnType<typeof buildMarketAuditRow> {
  return {
    company: "Anthropic",
    eventSlug: "event",
    marketSlug: "market",
    threshold: 1_100_000_000_000,
    deadline: "2026-08-01T03:59:59Z",
    label: "HIGH",
    state: "NEWLY_CROSSED",
    crossedQuality: "SOURCE_CONFIRMED_AND_STALE",
    latestValuation: 1_101_000_000_000,
    latestDate: "2026-07-01",
    maxEligibleValuation: 1_101_000_000_000,
    maxEligibleDate: "2026-07-01",
    previousMaxEligibleValuation: 1_090_000_000_000,
    sourceDateAgeHours: 24,
    yesAsk: 0.81,
    yesBid: 0.78,
    settlementEdge: 0.19,
    distancePct: 0.001,
    depthUnderCap: 100,
    bookAgeMs: 1_000,
    ruleConfidence: 10,
    tradeScore: 100,
    tradeBand: "tradeable",
    liveBlockers: [],
    reason: "source_confirmed_and_stale",
    ...overrides,
  };
}

function forecastRowFixture(overrides: Partial<ReturnType<typeof buildNpmBarrierForecasts>[number]> = {}): ReturnType<typeof buildNpmBarrierForecasts>[number] {
  return {
    company: "OpenAI",
    eventSlug: "event",
    marketSlug: "forecast-market",
    threshold: 1_000,
    deadline: "2026-08-01T03:59:59Z",
    state: "UNCROSSED",
    latestValuation: 980,
    latestDate: "2026-07-06",
    maxEligibleValuation: 980,
    maxEligibleDate: "2026-07-06",
    distancePct: 0.02,
    daysRemaining: 25,
    sourceDateAgeHours: 12,
    freshnessState: "MISSED_EXPECTED_UPDATE",
    expectedNextUpdateAt: "2026-07-07T12:00:00Z",
    lastSuccessfulFetchAt: "2026-07-06T17:00:00Z",
    endpointChangedSinceLastFetch: false,
    tapeAdvancedSinceLastFetch: false,
    carryForwardLikely: false,
    dailyDrift: 0.002,
    medianDailyDrift: 0.002,
    recent3DayAvgDailyDrift: 0.002,
    recent7DayAvgDailyDrift: 0.002,
    dailyVol: 0.005,
    maxDailyMove: 0.01,
    pCrossTomorrow: 0.2,
    pTouchByDeadline: 0.62,
    yesAsk: 0.55,
    yesBid: 0.52,
    modelFairPrice: 0.62,
    edge: 0.07,
    confidenceScore: 0.55,
    depthUnderCap: 250,
    signalType: "NO_FORECAST_EDGE",
    liveEligible: false,
    reason: "forecast_edge_below_minimum",
    needed: ["modelFairPrice - yesAsk must be >= 0.12"],
    paperTrade: {
      forecastTime: "2026-07-06T17:00:00Z",
      entryPrice: 0.55,
      nextNpmFixingResult: null,
      thresholdTouched: null,
      marketPriceAfterFixing: null,
      finalResolution: null,
      hypotheticalPnl: null,
    },
    ...overrides,
  };
}
