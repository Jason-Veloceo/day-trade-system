"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useBrokerStream } from "@/lib/ws";

const navItems = [
  { href: "/", label: "Watchlist" },
  { href: "/rejected", label: "Rejected" },
  { href: "/rules", label: "Rules" },
  { href: "/engine", label: "Engine" },
  { href: "/inspect", label: "Inspect" },
];

export function StatusBar() {
  const pathname = usePathname();
  const { connected } = useBrokerStream();

  return (
    <header className="sticky top-0 z-10 border-b border-neutral-200 bg-white/90 backdrop-blur">
      <div className="mx-auto flex max-w-screen-2xl items-center justify-between px-4 py-3">
        <div className="flex items-center gap-6">
          <Link href="/" className="text-base font-semibold tracking-tight text-neutral-900">
            day-trade
          </Link>
          <nav className="flex items-center gap-1 text-sm">
            {navItems.map((item) => {
              const active = item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  className={
                    active
                      ? "rounded-md bg-neutral-900 px-3 py-1.5 text-white"
                      : "rounded-md px-3 py-1.5 text-neutral-600 hover:bg-neutral-100 hover:text-neutral-900"
                  }
                >
                  {item.label}
                </Link>
              );
            })}
          </nav>
        </div>
        <div className="flex items-center gap-2">
          <span
            className={
              connected
                ? "inline-flex items-center gap-1.5 rounded-full bg-emerald-50 px-2.5 py-1 text-xs font-medium text-emerald-700 ring-1 ring-emerald-200"
                : "inline-flex items-center gap-1.5 rounded-full bg-neutral-100 px-2.5 py-1 text-xs font-medium text-neutral-600 ring-1 ring-neutral-200"
            }
          >
            <span
              className={
                connected
                  ? "h-1.5 w-1.5 rounded-full bg-emerald-500"
                  : "h-1.5 w-1.5 rounded-full bg-neutral-400"
              }
            />
            {connected ? "Connected" : "Reconnecting…"}
          </span>
        </div>
      </div>
    </header>
  );
}
