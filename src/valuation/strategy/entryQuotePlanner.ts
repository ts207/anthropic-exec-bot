export function nearBoundaryPassiveBid(input: {
  modelFair: number;
  requiredEdge?: number;
  maxBid?: number;
}): number | null {
  const requiredEdge = input.requiredEdge ?? 0.12;
  const maxBid = input.maxBid ?? 0.62;
  const price = Math.min(input.modelFair - requiredEdge, maxBid);
  return normalizedBid(price);
}

export function farOptionalityPassiveBid(input: {
  modelFair: number;
  yesAsk: number;
  requiredEdge?: number;
}): number | null {
  const requiredEdge = input.requiredEdge ?? 0.08;
  const price = Math.min(input.modelFair - requiredEdge, input.yesAsk * 0.65);
  return normalizedBid(price);
}

export function curveRepairPassiveBid(input: {
  lowerYesAsk: number;
  bidBackedEdge: number;
}): number | null {
  return normalizedBid(input.lowerYesAsk + Math.min(0.01, input.bidBackedEdge / 4));
}

function normalizedBid(value: number): number | null {
  if (!Number.isFinite(value) || value <= 0) return null;
  return Math.min(0.99, Math.max(0.01, Math.floor(value * 1000) / 1000));
}
