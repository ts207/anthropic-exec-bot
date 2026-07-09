import type { CurvePoint } from "./signalTypes.ts";

export type ImpliedCurve = {
  company: string;
  deadlineIso: string;
  points: CurvePoint[];
  medianValuation?: number;
  expectedValuation?: number;
};

export function buildImpliedCurves(points: CurvePoint[]): ImpliedCurve[] {
  const groups = new Map<string, CurvePoint[]>();
  for (const point of points) {
    if (point.leg.threshold === undefined) continue;
    const key = `${point.leg.company}\u0000${point.leg.deadlineIso}`;
    groups.set(key, [...(groups.get(key) ?? []), point]);
  }
  return [...groups.values()].map((group) => buildCurve(group));
}

export function buildCurve(points: CurvePoint[]): ImpliedCurve {
  const sorted = [...points].sort((left, right) => (left.leg.threshold ?? 0) - (right.leg.threshold ?? 0));
  const first = sorted[0];
  if (!first) throw new Error("cannot build empty implied curve");
  return {
    company: first.leg.company,
    deadlineIso: first.leg.deadlineIso,
    points: sorted,
    medianValuation: interpolateQuantile(sorted, 0.5),
    expectedValuation: estimateExpectedValuation(sorted),
  };
}

export function interpolateQuantile(points: CurvePoint[], probability: number): number | undefined {
  const sorted = [...points].sort((left, right) => (left.leg.threshold ?? 0) - (right.leg.threshold ?? 0));
  for (let i = 0; i < sorted.length; i += 1) {
    const current = sorted[i];
    if (!current?.leg.threshold) continue;
    if (current.yesAsk <= probability) {
      const previous = sorted[i - 1];
      if (!previous?.leg.threshold) return current.leg.threshold;
      const priceSpan = previous.yesAsk - current.yesAsk;
      if (priceSpan <= 0) return current.leg.threshold;
      const fraction = (previous.yesAsk - probability) / priceSpan;
      return previous.leg.threshold + fraction * (current.leg.threshold - previous.leg.threshold);
    }
  }
  return sorted.at(-1)?.leg.threshold;
}

function estimateExpectedValuation(points: CurvePoint[]): number | undefined {
  const sorted = [...points].filter((point) => point.leg.threshold !== undefined);
  if (!sorted.length) return undefined;
  const weighted = sorted.reduce((sum, point) => sum + (point.leg.threshold ?? 0) * point.yesAsk, 0);
  const total = sorted.reduce((sum, point) => sum + point.yesAsk, 0);
  return total > 0 ? weighted / total : undefined;
}
