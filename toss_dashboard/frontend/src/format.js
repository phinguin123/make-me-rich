export function num(v, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  return Number(v).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function money(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  const n = Number(v);
  return `${n < 0 ? "-" : ""}$${num(Math.abs(n))}`;
}

export function pct(v, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  return `${(Number(v) * 100).toFixed(digits)}%`;
}

export function signClass(v) {
  if (!v) return "text-zinc-200";
  return Number(v) > 0 ? "text-emerald-400" : "text-red-400";
}
