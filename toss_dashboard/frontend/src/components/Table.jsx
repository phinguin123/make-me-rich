export default function Table({ columns, rows, empty }) {
  if (!rows.length) return <div className="px-3 py-6 text-center text-xs text-zinc-600">{empty}</div>;
  return (
    <div className="max-h-[360px] overflow-auto">
      <table className="w-full text-xs tabular-nums">
        <thead className="sticky top-0 bg-zinc-950 text-zinc-500">
          <tr>
            {columns.map((c) => (
              <th key={c.key} className="px-3 py-1.5 text-left font-normal">
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={`${r.symbol}-${r.closed ?? r.opened ?? i}`} className="border-t border-zinc-900">
              {columns.map((c) => (
                <td key={c.key} className={`px-3 py-1.5 ${c.cls ? c.cls(r[c.key]) : "text-zinc-200"}`}>
                  {c.fmt ? c.fmt(r[c.key]) : r[c.key] ?? ""}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
