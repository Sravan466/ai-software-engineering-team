"use client";

import Link from "next/link";
import type { HomeTab } from "./types";

/**
 * Shared header navigation for the merged home page. The two primary tabs
 * (Home = the Blueprint landing board, Chats = previous + new chats) are
 * buttons that flip the page-level tab state; settings/api stay as links.
 * Rendered inside both the Blueprint header (via its `nav` slot) and the
 * ChatsView header so the chrome is identical across tabs.
 */
export default function TopNav({
  active,
  onSelect,
}: {
  active: HomeTab;
  onSelect: (tab: HomeTab) => void;
}) {
  return (
    <nav className="bp-nav">
      <button
        type="button"
        className={"bp-nav-link bp-nav-tab" + (active === "home" ? " on" : "")}
        aria-current={active === "home" ? "page" : undefined}
        onClick={() => onSelect("home")}
      >
        home
      </button>
      <button
        type="button"
        className={"bp-nav-link bp-nav-tab" + (active === "chats" ? " on" : "")}
        aria-current={active === "chats" ? "page" : undefined}
        onClick={() => onSelect("chats")}
      >
        chats
      </button>
      <Link className="bp-nav-link" href="/settings">
        settings
      </Link>
      <a
        className="bp-nav-link"
        href="http://localhost:8000/docs"
        target="_blank"
        rel="noreferrer"
      >
        api
      </a>
    </nav>
  );
}
