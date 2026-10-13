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

const statusTone = { in_trade: "text-emerald-300", entry_open: "text-sky-300", exiting: "text-amber-300", exit_pending: "text-red-400", watching: "text-zinc-300" };

function PreMarket({ pre }) {
  const rows = pre.names ?? [];
  const r = pre.regime ?? {};
  const active = rows.filter((x) => x.qty > 0 || ["entry_open", "in_trade", "exiting", "exit_pending"].includes(x.status));
  const shown = [...active, ...rows.filter((x) => !active.includes(x))];
  return (
    <div className="border-b border-zinc-900 px-3 py-2 text-xs">
      <div className="mb-1 flex flex-wrap justify-between gap-2 text-[11px] text-zinc-500">
        <span className="uppercase tracking-wide">Pre-market dip book · {rows.length} names · entries 07:00–09:00 ET, out by 09:25</span>
        <span>
          slots {pre.used ?? 0}/{pre.slots ?? 3} ·{" "}
          {r.ok === false ? <span className="text-amber-300">QQQ below 200-day: standing aside</span> : r.qqq ? `QQQ ${num(r.qqq)} > 200d ${num(r.ma200)}` : "QQQ filter pending"}
        </span>
      </div>
      <div className="max-h-[260px] overflow-auto">
        <table className="w-full tabular-nums">
          <thead className="text-zinc-500">
            <tr>{["Name", "Status", "Last", "VWAP", "Buy level", "Gap", "Dip", "Position", "Target", "P&L"].map((h) => <th key={h} className="px-1.5 py-1 text-left font-normal">{h}</th>)}</tr>
          </thead>
          <tbody>
            {shown.map((x) => {
              const gap = x.level && x.last ? x.last / x.level - 1 : null;
              const pnl = x.qty > 0 && x.last ? (x.last - x.entry) * x.qty : 0;
              return (
                <tr key={x.symbol} className="border-t border-zinc-900" title={x.note || ""}>
                  <td className="px-1.5 py-1 text-zinc-200">{x.symbol}</td>
                  <td className={`px-1.5 py-1 ${statusTone[x.status] ?? "text-zinc-500"}`}>{x.status}</td>
                  <td className="px-1.5 py-1">{x.last ? num(x.last) : "—"}</td>
                  <td className="px-1.5 py-1">{x.vwap ? num(x.vwap) : "—"}</td>
                  <td className="px-1.5 py-1">{x.level ? num(x.level) : "—"}</td>
                  <td className={`px-1.5 py-1 ${gap !== null && gap < 0.005 ? "text-amber-300" : "text-zinc-400"}`}>{gap !== null ? pct(gap, 1) : "—"}</td>
                  <td className="px-1.5 py-1 text-zinc-400">{x.dip ? pct(x.dip, 1) : "—"}</td>
                  <td className="px-1.5 py-1">{x.qty > 0 ? `${num(x.qty, 0)} @ ${num(x.entry)}` : "—"}</td>
                  <td className="px-1.5 py-1">{x.target ? num(x.target) : "—"}</td>
                  <td className={`px-1.5 py-1 ${signClass(pnl)}`}>{x.qty > 0 ? money(pnl) : ""}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
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
    <Panel title="Scalp · pre-market dip book (14 names) + SOXL/SOXS bear-regime breakout" right={st.day ?? ""}>
      <div className="grid grid-cols-2 gap-2 border-b border-zinc-900 px-3 py-2 text-xs sm:grid-cols-6">
        <div><div className="text-zinc-500">Regime</div><div className={regimeTone}>{st.regime === "bear" ? "BEAR · trading" : st.regime === "bull" ? "bull · standing aside" : st.regime}</div></div>
        <div><div className="text-zinc-500">SOXX vs 50-day</div><div className="tabular-nums">{st.soxx_ma ? pct(st.soxx_close / st.soxx_ma - 1, 1) : "—"}</div></div>
        <div><div className="text-zinc-500">SOXL range</div><div className="tabular-nums">{st.or_high ? `${num(st.or_low)}–${num(st.or_high)}` : "—"}</div></div>
        <div><div className="text-zinc-500">Status</div><div>{st.status}{st.side ? ` · ${st.side} ${num(st.qty, 0)}` : ""}</div></div>
        <div><div className="text-zinc-500">Open P&L</div><div className={`tabular-nums ${signClass(open)}`}>{money(open)}</div></div>
        <div><div className="text-zinc-500">Equity / realized</div><div className="tabular-nums">{money(scalp.equity)} / <span className={signClass(scalp.realized)}>{money(scalp.realized)}</span></div></div>
      </div>
      {st.note && <div className="px-3 py-1.5 text-[11px] text-zinc-500">{st.note}</div>}
      {scalp.pre?.enabled && <PreMarket pre={scalp.pre} />}
      <div className="px-3 py-1 text-[11px] text-zinc-500">SOXL {num(q.SOXL)} · SOXS {num(q.SOXS)} · SOXX {num(q.SOXX)}</div>
      <Table columns={tradeCols} rows={[...(st.trades ?? [])].reverse()} empty="No scalp trades yet" />
    </Panel>
  );
}
