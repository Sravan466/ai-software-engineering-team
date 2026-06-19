import Link from "next/link";

// Chrome for the inner application pages (project detail, settings).
// Styled to match the merged home's Blueprint board so the whole app feels like one piece.
export default function AppLayout({ children }: { children: React.ReactNode }) {
  return (
    <>
      <header style={{ borderBottom: "1px solid var(--line)" }}>
        <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-4">
          <Link href="/" className="flex items-center gap-2.5">
            <span className="fd-logo-mark" />
            <span className="fd-wordmark">
              AI<span>SWE TEAM</span>
            </span>
          </Link>
          <nav className="flex items-center gap-5">
            <Link href="/" className="fd-nav-link">
              home
            </Link>
            <Link href="/?tab=chats" className="fd-nav-link">
              chats
            </Link>
            <Link href="/settings" className="fd-nav-link">
              settings
            </Link>
            <a
              href="http://localhost:8000/docs"
              target="_blank"
              rel="noreferrer"
              className="fd-nav-link"
            >
              api
            </a>
          </nav>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-6 py-10">{children}</main>
    </>
  );
}
