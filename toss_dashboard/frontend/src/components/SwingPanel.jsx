import Panel from "./Panel";
import Table from "./Table";
import { money, num, pct, signClass } from "../format";

const cols = [
  { key: "symbol", label: "Symbol" },
  { key: "qty", label: "Shares", fmt: (v) => num(v, 4) },
  { key: "price", label: "Price", fmt: (v) => (v ? num(v) : "—") },
  { key: "value", label: "Value", fmt: money },
  { key: "weight", label: "Weight", fmt: (v) => pct(v, 1) },
  { key: "target", label: "Model", fmt: (v) => pct(v, 1) },
  { key: "pnl", label: "P&L", fmt: money, cls: signClass },
];

export default function SwingPanel({ swing }) {
  if (!swing?.enabled) {
    return (
      <Panel title="Swing portfolio">
        <div className="px-3 py-6 text-center text-xs text-zinc-600">Disabled — set TRADER_SWING_CAPITAL in .env</div>
      </Panel>
    );
  }
  const m = swing.model ?? {};
  return (
    <Panel title="Swing portfolio · monthly momentum" right={`model data ${m.as_of ?? "—"}`}>
      <div className="grid grid-cols-2 gap-2 border-b border-zinc-900 px-3 py-2 text-xs sm:grid-cols-6">
        <div><div className="text-zinc-500">Equity</div><div className="tabular-nums">{money(swing.equity)}</div></div>
        <div><div className="text-zinc-500">Cash</div><div className="tabular-nums">{money(swing.cash)}</div></div>
        <div><div className="text-zinc-500">Exposure</div><div className="tabular-nums">{pct(m.exposure, 0)}</div></div>
        <div><div className="text-zinc-500">SOXS hedge</div><div>{m.hedge_on ? "ON" : swing.params?.hedge === "none" ? "off (disabled)" : "off"}</div></div>
        <div><div className="text-zinc-500">Model since 2024</div><div className={`tabular-nums ${signClass(m.model_return_since_anchor)}`}>{pct(m.model_return_since_anchor, 0)}</div></div>
        <div><div className="text-zinc-500">Last rebalance</div><div>{swing.last_run || "—"}</div></div>
      </div>
      <Table columns={cols} rows={swing.holdings ?? []} empty="Builds the portfolio at 09:36 ET on the next trading day" />
    </Panel>
  );
}
