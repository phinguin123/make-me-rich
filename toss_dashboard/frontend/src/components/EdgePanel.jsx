import Panel from "./Panel";
import { money, num, pct, signClass } from "../format";

const tone = { healthy: "text-emerald-400", reduced: "text-amber-300", off: "text-red-400" };

export default function EdgePanel({ edges, onEnable, busy }) {
  if (!edges?.length) return null;
  return (
    <Panel title="Edge health · live trades vs backtest" right="sizes down or switches off broken edges">
      <div className="overflow-auto">
        <table className="w-full text-xs tabular-nums">
          <thead className="bg-zinc-950 text-zinc-500">
            <tr>
              {["Edge", "Status", "Size", "Live trades", "Live avg", "Backtest avg", "z", "Drawdown", "Live P&L", ""].map((h) => (
                <th key={h} className="px-3 py-1.5 text-left font-normal">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {edges.map((e) => (
              <tr key={e.name} className="border-t border-zinc-900 align-top">
                <td className="px-3 py-1.5 text-zinc-200">
                  {e.label}
                  {e.reason && <div className="text-[11px] text-zinc-500">{e.reason}</div>}
                </td>
                <td className={`px-3 py-1.5 ${tone[e.status] ?? "text-zinc-300"}`}>{e.status}</td>
                <td className="px-3 py-1.5 text-zinc-200">×{num(e.size, 1)}</td>
                <td className="px-3 py-1.5 text-zinc-200">{e.live.n}</td>
                <td className={`px-3 py-1.5 ${signClass(e.live.mean)}`}>{e.live.n ? pct(e.live.mean) : "—"}</td>
                <td className="px-3 py-1.5 text-zinc-400">
                  {e.backtest?.mean != null ? `${pct(e.backtest.mean)} (${e.backtest.n} trades, ~${Math.round(e.backtest.per_year)}/yr)` : "—"}
                </td>
                <td className="px-3 py-1.5 text-zinc-200">{e.live.n ? num(e.live.z, 1) : "—"}</td>
                <td className="px-3 py-1.5 text-zinc-200">
                  {pct(e.live.dd, 1)} <span className="text-zinc-500">/ off at {e.backtest?.dd_off != null ? pct(e.backtest.dd_off, 0) : "—"}</span>
                </td>
                <td className={`px-3 py-1.5 ${signClass(e.live.pnl)}`}>{money(e.live.pnl)}</td>
                <td className="px-3 py-1.5">
                  {e.status === "off" && (
                    <button
                      className="rounded border border-zinc-700 px-2 py-0.5 text-[11px] text-zinc-200 hover:bg-zinc-800 disabled:opacity-40"
                      disabled={busy}
                      onClick={() => onEnable(e.name)}
                    >
                      Re-enable
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}
