import { useState } from "react";
import Panel from "./Panel";
import { money, pct } from "../format";

function Bar({ label, value, share, target }) {
  return (
    <div>
      <div className="mb-1 flex justify-between text-zinc-400">
        <span>{label}</span>
        <span className="tabular-nums text-zinc-200">
          {money(value)} · {pct(share, 0)} <span className="text-zinc-500">(target {pct(target, 0)})</span>
        </span>
      </div>
      <div className="relative h-2 rounded bg-zinc-800">
        <div className="absolute left-0 top-0 h-2 rounded bg-sky-500" style={{ width: `${Math.min(100, share * 100)}%` }} />
        <div className="absolute top-[-3px] h-3.5 w-px bg-amber-300" style={{ left: `${Math.min(100, target * 100)}%` }} />
      </div>
    </div>
  );
}

export default function AllocationPanel({ alloc, onDeposit, busy }) {
  const [amount, setAmount] = useState("");
  if (!alloc?.enabled) return null;
  const c = alloc.current ?? {};
  const t = alloc.targets ?? {};
  const last = alloc.history?.length ? alloc.history[alloc.history.length - 1] : null;
  return (
    <Panel title="Allocation · scalp / swing / reserve" right={alloc.next ? `next rebalance: ${alloc.next}` : ""}>
      <div className="grid gap-3 px-3 py-2 text-xs sm:grid-cols-2">
        <Bar label="Scalp (SOXL/SOXS)" value={c.scalp} share={c.scalp_share ?? 0} target={t.scalp ?? 0} />
        <Bar label="Swing (momentum)" value={c.swing} share={c.swing_share ?? 0} target={t.swing ?? 0} />
        {(c.reserve ?? 0) > 0 && (
          <Bar label="Cash reserve (never traded)" value={c.reserve} share={c.reserve_share ?? 0} target={1 - (t.scalp ?? 0) - (t.swing ?? 0)} />
        )}
        {alloc.breaker?.limit > 0 && (
          <div>
            <div className="mb-1 flex justify-between text-zinc-400">
              <span>Drawdown breaker</span>
              <span className={`tabular-nums ${alloc.breaker.breached ? "text-red-400" : "text-zinc-200"}`}>
                {alloc.breaker.breached ? "TRIPPED · entries paused" : `${pct(alloc.breaker.drawdown, 1)} from high`}
                <span className="text-zinc-500"> (pauses at -{pct(alloc.breaker.limit, 0)})</span>
              </span>
            </div>
            <div className="relative h-2 rounded bg-zinc-800">
              <div className={`absolute left-0 top-0 h-2 rounded ${alloc.breaker.breached ? "bg-red-500" : "bg-amber-400"}`}
                style={{ width: `${Math.min(100, (-Math.min(0, alloc.breaker.drawdown) / alloc.breaker.limit) * 100)}%` }} />
            </div>
          </div>
        )}
      </div>
      {alloc.phase && (
        <div className="px-3 pb-1 text-[11px] text-zinc-400">
          Phase {alloc.phase.index} of {alloc.phase.count}
          {" · "}
          {alloc.phase.plan.map((p, i) => (
            <span key={i} className={i + 1 === alloc.phase.index ? "text-sky-300" : "text-zinc-500"}>
              {i ? " → " : ""}from {money(p.from)}: {pct(p.scalp, 0)}/{pct(p.swing, 0)}
            </span>
          ))}
        </div>
      )}
      {onDeposit && (
        <div className="flex flex-wrap items-center gap-2 px-3 pb-2 text-[11px] text-zinc-400">
          <span>Money in/out of the bot (USD, after you deposit or withdraw in Toss):</span>
          <input
            className="w-24 rounded border border-zinc-700 bg-zinc-900 px-2 py-0.5 text-zinc-200"
            inputMode="decimal" placeholder="500" value={amount} onChange={(e) => setAmount(e.target.value)}
          />
          {[["Add to bot", 1], ["Take out", -1]].map(([label, sign]) => (
            <button
              key={label}
              className="rounded border border-zinc-700 px-2 py-0.5 text-zinc-200 hover:bg-zinc-800 disabled:opacity-40"
              disabled={busy || !(parseFloat(amount) > 0)}
              onClick={() => { onDeposit(sign * parseFloat(amount)); setAmount(""); }}
            >
              {label}
            </button>
          ))}
          {alloc.pending ? <span className="text-amber-300">pending {money(alloc.pending)} (applied when the sleeves are flat)</span> : null}
        </div>
      )}
      <div className="px-3 pb-2 text-[11px] text-zinc-500">
        {alloc.note ? `${alloc.note} · ` : ""}
        {last ? `last rebalance ${last.date} (${last.reason}): ${money(last.total)} → scalp ${money(last.scalp)} / swing ${money(last.swing)}${last.reserve ? ` / reserve ${money(last.reserve)}` : ""}` : "not rebalanced yet"}
      </div>
    </Panel>
  );
}
