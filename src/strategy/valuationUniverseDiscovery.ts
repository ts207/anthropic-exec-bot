import { fetchGammaEvent, parseGammaEvent, parseValuationLegs } from "./marketParser.ts";
import type { EventConfig, GammaEvent, StrategyConfig, ValuationLeg } from "./signalTypes.ts";

const GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events";
const VALUATION_EVENT_RE = /\bvaluation\b.*\bhit\b.*\bby\b/i;

export type DiscoveredValuationEvent = {
  eventSlug: string;
  title: string;
  company?: string;
  deadlineIso?: string;
  marketWindowStartIso?: string;
  npmCompanyId?: string;
  npmSourceUrl?: string;
  source: "configured_seed" | "gamma_crawl";
  configured: boolean;
  legCount: number;
  thresholdLegCount: number;
  directionCounts: Record<"UP" | "DOWN" | "UNKNOWN", number>;
  markets: Array<{
    marketSlug: string;
    question: string;
    threshold?: number;
    direction: "UP" | "DOWN" | "UNKNOWN";
    yesTokenId?: string;
    noTokenId?: string;
    active: boolean;
    closed: boolean;
    acceptingOrders: boolean;
    liquidity: number;
    ruleHash: string;
    parseStatus: string;
  }>;
};

export type ValuationDiscoveryReport = {
  ok: true;
  generatedAt: string;
  configuredSeedCount: number;
  gammaCrawlEnabled: boolean;
  gammaPagesScanned: number;
  discoveredEventCount: number;
  accessIssues: string[];
  events: DiscoveredValuationEvent[];
};

export async function discoverValuationUniverse(input: {
  config: StrategyConfig;
  crawlGamma?: boolean;
  maxPages?: number;
  pageSize?: number;
}): Promise<ValuationDiscoveryReport> {
  const configured = new Map(input.config.events.map((event) => [event.slug, event]));
  const events = new Map<string, { event: GammaEvent; source: DiscoveredValuationEvent["source"]; config?: EventConfig }>();
  const accessIssues: string[] = [];

  for (const eventConfig of input.config.events) {
    try {
      const event = await fetchGammaEvent(eventConfig.slug);
      if (isValuationEvent(event) || eventConfig.kind === "threshold") {
        events.set(event.slug, { event, source: "configured_seed", config: eventConfig });
      }
    } catch (error) {
      accessIssues.push(`configured_seed_fetch_failed:${eventConfig.slug}:${errorMessage(error)}`);
    }
  }

  let pagesScanned = 0;
  if (input.crawlGamma !== false) {
    const maxPages = Math.max(1, input.maxPages ?? 5);
    const pageSize = Math.max(20, Math.min(100, input.pageSize ?? 100));
    for (let page = 0; page < maxPages; page += 1) {
      try {
        const rawEvents = await fetchGammaEventsPage(page * pageSize, pageSize);
        pagesScanned += 1;
        if (!rawEvents.length) break;
        for (const raw of rawEvents) {
          const event = parseGammaEvent(raw);
          if (!isValuationEvent(event) || events.has(event.slug)) continue;
          events.set(event.slug, { event, source: "gamma_crawl", config: inferEventConfig(event) });
        }
      } catch (error) {
        accessIssues.push(`gamma_crawl_failed:page_${page}:${errorMessage(error)}`);
        break;
      }
    }
  }

  const discovered = [...events.values()]
    .map(({ event, source, config }) => discoveredEventRow(event, config ?? inferEventConfig(event), source, configured.has(event.slug)))
    .sort((left, right) => left.company === right.company
      ? left.eventSlug.localeCompare(right.eventSlug)
      : String(left.company ?? "").localeCompare(String(right.company ?? "")));

  return {
    ok: true,
    generatedAt: new Date().toISOString(),
    configuredSeedCount: input.config.events.length,
    gammaCrawlEnabled: input.crawlGamma !== false,
    gammaPagesScanned: pagesScanned,
    discoveredEventCount: discovered.length,
    accessIssues,
    events: discovered,
  };
}

async function fetchGammaEventsPage(offset: number, limit: number): Promise<unknown[]> {
  const url = `${GAMMA_EVENTS_URL}?active=true&closed=false&offset=${offset}&limit=${limit}`;
  const response = await fetch(url, {
    headers: {
      accept: "application/json",
      "user-agent": "Mozilla/5.0 valuation-ladder-bot",
    },
  });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  const raw = await response.json() as unknown;
  return Array.isArray(raw) ? raw : [];
}

function discoveredEventRow(
  event: GammaEvent,
  config: EventConfig,
  source: DiscoveredValuationEvent["source"],
  configured: boolean,
): DiscoveredValuationEvent {
  const legs = parseValuationLegs(event, config);
  const thresholdLegs = legs.filter((leg) => leg.threshold !== undefined);
  const directionCounts = thresholdLegs.reduce<Record<"UP" | "DOWN" | "UNKNOWN", number>>((counts, leg) => {
    counts[directionFromLeg(leg)] += 1;
    return counts;
  }, { UP: 0, DOWN: 0, UNKNOWN: 0 });
  return {
    eventSlug: event.slug,
    title: event.title,
    company: config.companyName,
    deadlineIso: config.deadlineIso,
    marketWindowStartIso: config.marketWindowStartIso,
    npmCompanyId: extractNpmCompanyId(event),
    npmSourceUrl: event.resolutionSource,
    source,
    configured,
    legCount: legs.length,
    thresholdLegCount: thresholdLegs.length,
    directionCounts,
    markets: legs.map((leg) => ({
      marketSlug: leg.marketSlug,
      question: leg.question,
      threshold: leg.threshold,
      direction: directionFromLeg(leg),
      yesTokenId: leg.yesTokenId,
      noTokenId: leg.noTokenId,
      active: leg.active,
      closed: leg.closed,
      acceptingOrders: leg.acceptingOrders,
      liquidity: leg.liquidity,
      ruleHash: leg.ruleHash,
      parseStatus: leg.parseStatus,
    })),
  };
}

function inferEventConfig(event: GammaEvent): EventConfig {
  return {
    slug: event.slug,
    kind: "threshold",
    companyName: inferCompany(event.title),
    deadlineIso: inferDeadlineIso(event.title) ?? new Date(Date.now() + 30 * 86_400_000).toISOString(),
    marketWindowStartIso: undefined,
  };
}

function isValuationEvent(event: GammaEvent): boolean {
  const text = `${event.title}\n${event.description}`;
  return VALUATION_EVENT_RE.test(text);
}

function extractNpmCompanyId(event: GammaEvent): string | undefined {
  return event.resolutionSource?.match(/company-[a-f0-9-]+/)?.[0];
}

function inferCompany(title: string): string | undefined {
  const match = title.match(/Will\s+(.+?)(?:'s|’s)\s+valuation\s+hit/i);
  return match?.[1]?.trim();
}

function inferDeadlineIso(title: string): string | undefined {
  const match = title.match(/\bby\s+([A-Z][a-z]+)\s+([0-9]{1,2})(?:,\s*([0-9]{4}))?/);
  if (!match?.[1] || !match[2]) return undefined;
  const year = Number(match[3] ?? new Date().getUTCFullYear());
  const month = monthIndex(match[1]);
  const day = Number(match[2]);
  if (month === undefined || !Number.isFinite(day)) return undefined;
  return new Date(Date.UTC(year, month, day + 1, 3, 59, 59, 0)).toISOString();
}

function monthIndex(value: string): number | undefined {
  const index = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"].indexOf(value.toLowerCase());
  return index >= 0 ? index : undefined;
}

function directionFromLeg(leg: ValuationLeg): "UP" | "DOWN" | "UNKNOWN" {
  const text = `${leg.question}\n${leg.ruleText}`.toLowerCase();
  if (/[↓↘]|down|below|less than/.test(text)) {
    return /at or below|less than or equal|falls? to or below|below the listed amount/.test(text) ? "DOWN" : "UNKNOWN";
  }
  if (/reaches or exceeds|exceeds|surpass|hit/.test(text)) return "UP";
  return "UNKNOWN";
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message.replace(/\s+/g, " ") : String(error);
}
