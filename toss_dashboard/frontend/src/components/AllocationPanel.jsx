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

export default function AllocationPanel({ alloc }) {
  if (!alloc?.enabled) return null;
  const c = alloc.current ?? {};
  const t = alloc.targets ?? {};
  const last = alloc.history?.length ? alloc.history[alloc.history.length - 1] : null;
  return (
    <Panel title="Allocation · scalp vs swing" right={alloc.next ? `next rebalance: ${alloc.next}` : ""}>
      <div className="grid gap-3 px-3 py-2 text-xs sm:grid-cols-2">
        <Bar label="Scalp (SOXL/SOXS)" value={c.scalp} share={c.scalp_share ?? 0} target={t.scalp ?? 0} />
        <Bar label="Swing (momentum)" value={c.swing} share={c.swing_share ?? 0} target={t.swing ?? 0} />
      </div>
      <div className="px-3 pb-2 text-[11px] text-zinc-500">
        {alloc.note ? `${alloc.note} · ` : ""}
        {last ? `last rebalance ${last.date} (${last.reason}): ${money(last.total)} → scalp ${money(last.scalp)} / swing ${money(last.swing)}` : "not rebalanced yet"}
      </div>
    </Panel>
  );
}
