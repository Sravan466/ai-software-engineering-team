"use client";

import { useRouter, useSearchParams } from "next/navigation";
import Blueprint from "@/components/blueprint/Blueprint";
import ChatsView from "./ChatsView";
import TopNav from "./TopNav";
import type { HomeTab } from "./types";

/**
 * Merged home page: the marketing landing (Blueprint board) and the project
 * dashboard now live as two tabs of a single full-bleed page.
 *   • "home"  — the Blueprint pipeline board (the former /landing).
 *   • "chats" — previous chats + new-chat creation (the former / dashboard).
 * The active tab is mirrored in the URL (?tab=chats) so it is linkable from
 * the rest of the app. Both tabs share the same TopNav chrome.
 */
export default function HomeExperience() {
  const params = useSearchParams();
  const router = useRouter();
  const tab: HomeTab = params.get("tab") === "chats" ? "chats" : "home";

  const setTab = (next: HomeTab) =>
    router.replace(next === "chats" ? "/?tab=chats" : "/", { scroll: false });

  const nav = <TopNav active={tab} onSelect={setTab} />;

  return tab === "chats" ? <ChatsView nav={nav} /> : <Blueprint nav={nav} />;
}
