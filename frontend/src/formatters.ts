export function formatBytes(value: number | null | undefined): string {
  if (
    value === null
    || value === undefined
    || !Number.isFinite(value)
    || value < 0
  ) {
    return "Unavailable";
  }
  if (value < 1024) return `${Math.round(value)} B`;

  const units = ["KiB", "MiB", "GiB", "TiB"];
  let scaled = value;
  let unit = "B";
  for (const candidate of units) {
    scaled /= 1024;
    unit = candidate;
    if (scaled < 1024 || candidate === "TiB") break;
  }
  const precision = scaled >= 100 ? 0 : scaled >= 10 ? 1 : 2;
  return `${Number(scaled.toFixed(precision)).toLocaleString()} ${unit}`;
}

export function formatAppInteger(value: number | null | undefined): string {
  if (
    value === null
    || value === undefined
    || !Number.isSafeInteger(value)
    || value < 0
  ) {
    return "Unavailable";
  }
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 }).format(value);
}
