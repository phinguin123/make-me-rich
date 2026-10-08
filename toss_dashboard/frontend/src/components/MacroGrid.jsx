import { num, pct, signClass } from "../format";

const ORDER = ["QQQ", "SPY", "IWM", "SMH", "VIXY", "TLT", "UUP", "GLD", "USO", "HYG", "IBIT", "BTC"];

export default function MacroGrid({ macro }) {
  return (
    <section className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-6 xl:grid-cols-12">
      {ORDER.map((sym) => {
        const m = macro[sym];
        return (
          <div key={sym} className="rounded border border-zinc-800 bg-zinc-950 px-2.5 py-2" title={m?.label}>
            <div className="flex items-baseline justify-between">
              <span className="text-xs text-zinc-300">{sym}</span>
              <span className={`text-[11px] ${signClass(m?.chg)}`}>{m?.chg == null ? "" : pct(m.chg)}</span>
            </div>
            <div className="mt-0.5 text-sm tabular-nums text-zinc-100">{m ? num(m.last) : "—"}</div>
            <div className="truncate text-[10px] text-zinc-600">{m?.label ?? ""}</div>
          </div>
        );
      })}
    </section>
  );
}
