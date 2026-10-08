export default function Panel({ title, right, children }) {
  return (
    <section className="overflow-hidden rounded-lg border border-zinc-800 bg-zinc-950">
      <header className="flex items-center justify-between border-b border-zinc-800 px-3 py-2 text-[11px] uppercase tracking-widest text-zinc-500">
        <span>{title}</span>
        {right && <span>{right}</span>}
      </header>
      {children}
    </section>
  );
}
