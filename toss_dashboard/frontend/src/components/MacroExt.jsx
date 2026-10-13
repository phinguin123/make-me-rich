import Panel from "./Panel";
import { num, pct, signClass } from "../format";

const ORDER = ["ES=F", "NQ=F", "^SOX", "^VIX", "^VIX3M", "^MOVE", "DGS10", "DGS2", "BAMLH0A0HYM2", "DX-Y.NYB", "CL=F", "GC=F", "ZN=F"];
const SHORT = { "ES=F": "ES", "NQ=F": "NQ", "^SOX": "SOX", "^VIX": "VIX", "^VIX3M": "VIX3M", "^MOVE": "MOVE", DGS10: "10y %", DGS2: "2y %",
  BAMLH0A0HYM2: "HY spread %", "DX-Y.NYB": "DXY", "CL=F": "Oil", "GC=F": "Gold", "ZN=F": "10y note" };

export default function MacroExt({ macro, risk }) {
  const q = macro?.quotes ?? {};
  return (
    <Panel title="Futures · rates · risk" right={macro?.updated ?? "loading…"}>
      <div className="grid grid-cols-2 gap-2 p-3 sm:grid-cols-4 lg:grid-cols-7">
        {ORDER.filter((k) => q[k]).map((k) => {
          const x = q[k];
          const change = x.chg_3m_bp != null ? `${x.chg_3m_bp > 0 ? "+" : ""}${num(x.chg_3m_bp, 0)}bp 3m` : pct(x.chg);
          const tone = x.chg_3m_bp != null ? signClass(-x.chg_3m_bp) : signClass(x.chg);
          return (
            <div key={k} className="rounded border border-zinc-800 px-2 py-1.5" title={x.label}>
              <div className="flex justify-between gap-1 text-[11px]"><span className="text-zinc-300">{SHORT[k] ?? k}</span><span className={tone}>{change}</span></div>
              <div className="text-sm tabular-nums">{num(x.last, 2)}</div>
              <div className="truncate text-[10px] text-zinc-600">{x.label}</div>
            </div>
          );
        })}
      </div>
      {risk && Object.keys(risk).length > 0 && (
        <div className="border-t border-zinc-900 px-3 py-2 text-xs text-zinc-400">
          Breadth: {pct(risk.breadth_above_200dma, 0)} of liquid stocks above 200-day, {pct(risk.breadth_above_50dma, 0)} above 50-day ·
          SMH {pct(risk.smh_vs_200dma, 0)} vs 200-day · SPY {pct(risk.spy_off_high, 1)} from high
        </div>
      )}
      <div className="px-3 pb-3 text-xs">
        {(macro?.flags ?? []).length === 0 ? (
          <span className="text-zinc-600">No macro stress flags</span>
        ) : (
          macro.flags.map((f) => <div key={f} className="text-amber-300">⚠ {f}</div>)
        )}
      </div>
    </Panel>
  );
}
