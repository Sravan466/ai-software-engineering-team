"use client";

import { useCallback, useEffect, useLayoutEffect, useState, type ReactNode } from "react";
import { usePathname } from "next/navigation";
import Sidebar from "./Sidebar";
import { Icon } from "./icons";
import { ShellChromeProvider, useChromeValue } from "./ShellChrome";

// Below this width the sidebar stops being a rail and becomes an overlay
// drawer. Matches the `max-width: 900px` block in shell.css.
const DRAWER_BREAKPOINT = 900;

// The server can't know the viewport, so the markup ships collapsed and the real
// state is resolved from matchMedia. Doing that in a layout effect means it lands
// before the browser paints, so a desktop load never flashes a closed rail.
const useIsomorphicLayoutEffect = typeof window !== "undefined" ? useLayoutEffect : useEffect;

function ShellBody({ children }: { children: ReactNode }) {
  // Ships collapsed so the first paint on a phone is never a navigation drawer
  // covering the composer; resolved from the real viewport below.
  const [collapsed, setCollapsed] = useState(true);
  const [isDrawer, setIsDrawer] = useState(false);
  // Until the breakpoint is known the rail doesn't animate, so resolving it
  // can't read as a slide-in on every desktop page load.
  const [ready, setReady] = useState(false);
  const chrome = useChromeValue();
  const pathname = usePathname();

  // Track the breakpoint: on a wide screen the rail is open and pinned; on a
  // narrow one it is a drawer that starts closed.
  useIsomorphicLayoutEffect(() => {
    const mq = window.matchMedia(`(max-width: ${DRAWER_BREAKPOINT}px)`);
    const apply = (matches: boolean) => {
      setIsDrawer(matches);
      setCollapsed(matches);
    };
    apply(mq.matches);
    setReady(true);
    const onChange = (e: MediaQueryListEvent) => apply(e.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  const close = useCallback(() => setCollapsed(true), []);

  // Following a link inside the drawer should dismiss it.
  useEffect(() => {
    if (isDrawer) setCollapsed(true);
  }, [pathname, isDrawer]);

  // Escape closes the drawer, the way every other overlay on the web does.
  useEffect(() => {
    if (!isDrawer || collapsed) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [isDrawer, collapsed, close]);

  return (
    <div className={"shell" + (collapsed ? " collapsed" : "") + (ready ? " ready" : "")}>
      <Sidebar onClose={close} />
      <div className="ws-scrim" onClick={close} aria-hidden="true" />
      <div className="workspace">
        <header className="ws-top">
          {collapsed && (
            <button
              className="icon-btn"
              onClick={() => setCollapsed(false)}
              aria-label="Open navigation"
              aria-expanded={false}
            >
              {Icon.menu}
            </button>
          )}
          {chrome.title ? (
            <span className="ws-top-title">{chrome.title}</span>
          ) : (
            <span className="ws-top-sub">{chrome.sub || ""}</span>
          )}
          <span className="ws-top-spacer" />
          {chrome.badge}
        </header>
        <main className="ws-main" id="main">
          {children}
        </main>
      </div>
    </div>
  );
}

export default function AppShell({ children }: { children: ReactNode }) {
  return (
    <ShellChromeProvider>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <ShellBody>{children}</ShellBody>
    </ShellChromeProvider>
  );
}
