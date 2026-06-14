import type { Metadata } from "next";
import "./globals.css";
import { StatusBar } from "@/components/StatusBar";

export const metadata: Metadata = {
  title: "day-trade",
  description: "Personal Ross-style momentum trading copilot",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      {/*
        suppressHydrationWarning is needed because browser extensions
        (ColorZilla, Grammarly, LastPass, etc.) inject attributes like
        `cz-shortcut-listen` into the body before React hydrates, producing
        a benign hydration mismatch warning that we can safely ignore.
      */}
      <body
        suppressHydrationWarning
        className="min-h-screen bg-neutral-50 text-neutral-900 antialiased"
      >
        <StatusBar />
        <main className="mx-auto max-w-screen-2xl px-4 py-6">{children}</main>
      </body>
    </html>
  );
}
