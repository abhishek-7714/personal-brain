import Link from "next/link";

const LINKS = [
  { href: "/", label: "1. Status" },
  { href: "/ingest", label: "2. Ingest" },
  { href: "/experiences", label: "3. Experiences" },
  { href: "/explorer", label: "4. Brain Explorer" },
  { href: "/retrieval", label: "5. Retrieval Playground" },
  { href: "/brain", label: "6. Neural Brain" },
  { href: "/calibration", label: "7. Calibration" },
];

export default function Nav() {
  return (
    <div className="sidebar">
      <h1>Brain Console</h1>
      <nav>
        {LINKS.map((l) => (
          <Link key={l.href} href={l.href}>
            {l.label}
          </Link>
        ))}
      </nav>
    </div>
  );
}
