import Panel from "./Panel";
import Table from "./Table";
import { money, num, pct, signClass } from "../format";

const tradeCols = [
  { key: "day", label: "Day" },
  { key: "side", label: "ETF" },
  { key: "qty", label: "Qty", fmt: (v) => num(v, 0) },
  { key: "entry", label: "Entry", fmt: (v) => num(v) },
  { key: "exit", label: "Exit", fmt: (v) => num(v) },
  { key: "pnl", label: "P&L", fmt: money, cls: signClass },
  { key: "reason", label: "Exit" },
];

function PreMarket({ pre, last }) {
  const open = pre.qty > 0 && last ? (last - pre.entry) * pre.qty : 0;
  return (
    <div className="border-b border-zinc-900 px-3 py-2 text-xs">
      <div className="mb-1 text-[11px] uppercase tracking-wide text-zinc-500">Pre-market · SOXL dip below VWAP · entries 07:00–09:00 ET, out by 09:25</div>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-6">
        <div><div className="text-zinc-500">Status</div><div>{pre.status ?? "—"}</div></div>
        <div><div className="text-zinc-500">Dip size</div><div className="tabular-nums">{pre.dip ? pct(pre.dip, 2) : "—"}</div></div>
        <div><div className="text-zinc-500">VWAP / buy level</div><div className="tabular-nums">{pre.vwap ? `${num(pre.vwap)} / ${num(pre.level)}` : "—"}</div></div>
        <div><div className="text-zinc-500">Position</div><div className="tabular-nums">{pre.qty > 0 ? `${num(pre.qty, 0)} @ ${num(pre.entry)}` : "—"}</div></div>
        <div><div className="text-zinc-500">Take profit</div><div className="tabular-nums">{pre.target ? num(pre.target) : "—"}</div></div>
        <div><div className="text-zinc-500">Open P&L</div><div className={`tabular-nums ${signClass(open)}`}>{money(open)}</div></div>
      </div>
      {pre.note && <div className="mt-1 text-[11px] text-zinc-500">{pre.note}</div>}
    </div>
  );
}

export default function ScalpPanel({ scalp }) {
  if (!scalp?.enabled) return null;
  const st = scalp.state ?? {};
  const q = scalp.quotes ?? {};
  const last = st.side ? q[st.side] : null;
  const open = st.qty > 0 && last ? (last - st.entry) * st.qty : 0;
  const regimeTone = st.regime === "bear" ? "text-amber-300" : "text-zinc-400";
  return (
    <Panel title="SOXL / SOXS scalp · pre-market dip + bear-regime opening-range breakout" right={st.day ?? ""}>
      <div className="grid grid-cols-2 gap-2 border-b border-zinc-900 px-3 py-2 text-xs sm:grid-cols-6">
        <div><div className="text-zinc-500">Regime</div><div className={regimeTone}>{st.regime === "bear" ? "BEAR · trading" : st.regime === "bull" ? "bull · standing aside" : st.regime}</div></div>
        <div><div className="text-zinc-500">SOXX vs 50-day</div><div className="tabular-nums">{st.soxx_ma ? pct(st.soxx_close / st.soxx_ma - 1, 1) : "—"}</div></div>
        <div><div className="text-zinc-500">SOXL range</div><div className="tabular-nums">{st.or_high ? `${num(st.or_low)}–${num(st.or_high)}` : "—"}</div></div>
        <div><div className="text-zinc-500">Status</div><div>{st.status}{st.side ? ` · ${st.side} ${num(st.qty, 0)}` : ""}</div></div>
        <div><div className="text-zinc-500">Open P&L</div><div className={`tabular-nums ${signClass(open)}`}>{money(open)}</div></div>
        <div><div className="text-zinc-500">Equity / realized</div><div className="tabular-nums">{money(scalp.equity)} / <span className={signClass(scalp.realized)}>{money(scalp.realized)}</span></div></div>
      </div>
      {st.note && <div className="px-3 py-1.5 text-[11px] text-zinc-500">{st.note}</div>}
      {scalp.pre?.enabled && <PreMarket pre={scalp.pre.state ?? {}} last={q.SOXL} />}
      <div className="px-3 py-1 text-[11px] text-zinc-500">SOXL {num(q.SOXL)} · SOXS {num(q.SOXS)} · SOXX {num(q.SOXX)}</div>
      <Table columns={tradeCols} rows={[...(st.trades ?? [])].reverse()} empty="No scalp trades yet" />
    </Panel>
  );
}
