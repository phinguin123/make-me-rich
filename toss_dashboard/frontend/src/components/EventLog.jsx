import { useEffect, useRef } from "react";

const tone = { WARNING: "text-amber-300", ERROR: "text-red-400" };

export default function EventLog({ events }) {
  const box = useRef(null);
  useEffect(() => {
    if (box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [events.length]);
  return (
    <div ref={box} className="h-[360px] overflow-y-auto px-3 py-2 text-[12px] leading-5">
      {events.length === 0 && <div className="text-zinc-600">waiting for engine…</div>}
      {events.map((e, i) => (
        <div key={i} className={tone[e.level] ?? "text-emerald-300"}>
          <span className="mr-2 text-zinc-600">{e.ts}</span>
          {e.msg}
        </div>
      ))}
    </div>
  );
}
