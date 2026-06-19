"use client";

import { useState, type ReactNode } from "react";
import Sidebar from "./Sidebar";
import { Icon } from "./icons";
import { ShellChromeProvider, useChromeValue } from "./ShellChrome";

/**
 * The app-wide shell: a collapsible left sidebar + a workspace column whose top
 * bar (title / status badge) is driven by whatever page is mounted, via the
 * ShellChrome context. The page renders into `.ws-main`.
 */
function ShellBody({ children }: { children: ReactNode }) {
  const [collapsed, setCollapsed] = useState(false);
  const chrome = useChromeValue();

  return (
    <div className={"shell" + (collapsed ? " collapsed" : "")}>
      <Sidebar onToggle={() => setCollapsed((c) => !c)} />
      <div className="ws-scrim" onClick={() => setCollapsed(true)} />
      <div className="workspace">
        <header className="ws-top">
          {collapsed && (
            <button
              className="icon-btn"
              onClick={() => setCollapsed(false)}
              title="Open sidebar"
              aria-label="Open sidebar"
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
        <div className="ws-main">{children}</div>
      </div>
    </div>
  );
}

export default function AppShell({ children }: { children: ReactNode }) {
  return (
    <ShellChromeProvider>
      <ShellBody>{children}</ShellBody>
    </ShellChromeProvider>
  );
}
