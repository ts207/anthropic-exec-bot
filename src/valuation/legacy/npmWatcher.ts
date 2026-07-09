import { chromium, type BrowserContext, type Page } from "playwright";
import {
  parseNpmApiResponse,
  parseNpmValuationText,
  type ParsedNpmValuation,
} from "./npmParser.ts";

export type WatchOptions = {
  url: string;
  apiUrl: string | null;
  pollMs: number;
  profileDir: string;
  targetAsOf?: string;
};

export async function watchNpmUntilValuation(
  options: WatchOptions,
): Promise<ParsedNpmValuation> {
  if (options.apiUrl) {
    const apiResult = await readCurrentValuationFromApiResult(options.apiUrl);
    if (apiResult.fetched) {
      return await watchNpmApiOnly(options.apiUrl, options.pollMs);
    }
  }

  const context = await chromium.launchPersistentContext(options.profileDir, {
    headless: true,
    userAgent:
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    locale: "en-US",
    timezoneId: "America/New_York",
    viewport: { width: 1365, height: 900 },
    extraHTTPHeaders: {
      "accept-language": "en-US,en;q=0.9",
    },
  });
  try {
    const page = context.pages()[0] ?? (await context.newPage());
    await page.addInitScript(() => {
      Object.defineProperty(navigator, "webdriver", {
        get: () => undefined,
      });
    });
    await page.goto(options.url, {
      waitUntil: "domcontentloaded",
      timeout: 30_000,
    });

    for (;;) {
      if (options.apiUrl) {
        const apiResult = await readCurrentValuationFromApiResult(options.apiUrl);
        if (apiResult.fetched) {
          if (apiResult.data) {
            return apiResult.data;
          }
          await sleep(jitterDelay(options.pollMs));
          continue;
        }
      }

      const parsed = await readCurrentValuationFast(
        page,
        options.url,
        options.targetAsOf,
      );
      if (parsed) {
        return parsed;
      }

      await sleep(jitterDelay(options.pollMs));
    }
  } finally {
    await closeContext(context);
  }
}

async function watchNpmApiOnly(
  apiUrl: string,
  pollMs: number,
): Promise<ParsedNpmValuation> {
  for (;;) {
    const apiResult = await readCurrentValuationFromApiResult(apiUrl);
    if (!apiResult.fetched) {
      throw new Error("NPM API fetch failed after API-only watcher started");
    }
    if (apiResult.data) {
      return apiResult.data;
    }
    await sleep(jitterDelay(pollMs));
  }
}

export async function readCurrentValuationFromApi(
  apiUrl: string,
): Promise<ParsedNpmValuation | null> {
  const result = await readCurrentValuationFromApiResult(apiUrl);
  return result.data;
}

async function readCurrentValuationFromApiResult(
  apiUrl: string,
): Promise<{ fetched: boolean; data: ParsedNpmValuation | null }> {
  try {
    const response = await fetch(apiUrl, {
      cache: "no-store",
      headers: {
        accept: "application/json",
        referer: "https://fe.secondmarket.com/",
        "user-agent":
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
      },
    });
    if (!response.ok) {
      return { fetched: false, data: null };
    }

    const parsed = parseNpmApiResponse(await response.json());
    return {
      fetched: true,
      data: parsed.ok ? parsed.data : null,
    };
  } catch {
    return { fetched: false, data: null };
  }
}

export async function readCurrentValuationFast(
  page: Page,
  url: string,
  targetAsOf?: string,
): Promise<ParsedNpmValuation | null> {
  const fetched = await fetchTextInPage(page, url);
  if (fetched) {
    const parsed = parseNpmValuationText(fetched, targetAsOf);
    return parsed.ok ? parsed.data : null;
  }

  return await readCurrentValuationFromReload(page, targetAsOf);
}

export async function readCurrentValuationFromReload(
  page: Page,
  targetAsOf?: string,
): Promise<ParsedNpmValuation | null> {
  await page.reload({ waitUntil: "domcontentloaded", timeout: 30_000 });
  const text = await page.locator("body").innerText({ timeout: 15_000 });
  const parsed = parseNpmValuationText(text, targetAsOf);
  return parsed.ok ? parsed.data : null;
}

async function fetchTextInPage(page: Page, url: string): Promise<string | null> {
  try {
    const result = await page.evaluate(async (fetchUrl) => {
      const response = await fetch(fetchUrl, {
        cache: "no-store",
        credentials: "include",
      });
      const text = await response.text();
      const contentType = response.headers.get("content-type") ?? "";

      if (contentType.includes("text/html")) {
        const doc = new DOMParser().parseFromString(text, "text/html");
        return {
          ok: response.ok,
          status: response.status,
          text: doc.body?.innerText || text,
        };
      }

      return {
        ok: response.ok,
        status: response.status,
        text,
      };
    }, url);

    if (!result.ok) {
      return null;
    }
    return result.text;
  } catch {
    return null;
  }
}

function jitterDelay(pollMs: number): number {
  return pollMs + Math.floor(Math.random() * 50);
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function closeContext(context: BrowserContext): Promise<void> {
  try {
    await context.close();
  } catch {
    // Nothing useful to do during process exit.
  }
}
