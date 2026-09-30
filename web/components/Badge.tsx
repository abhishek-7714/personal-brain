export default function Badge({ ok, label }: { ok: boolean | "warn"; label: string }) {
  const cls = ok === true ? "ok" : ok === "warn" ? "warn" : "err";
  return <span className={`badge ${cls}`}>{label}</span>;
}
