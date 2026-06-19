import type { Metadata } from "next";
import { Suspense } from "react";
import HomeExperience from "@/components/home/HomeExperience";

export const metadata: Metadata = {
  title: "AI Software Engineering Team — ship the whole team",
  description:
    "Eight specialist agents take one idea to production-ready software, pausing for your approval at every phase. Browse your chats or start a new build.",
};

// Merged full-bleed home: the marketing landing and the project dashboard,
// unified into one page with Home / Chats tabs. Lives at the root (bare layout)
// so it renders full-bleed, outside the (app) header chrome.
// Suspense wraps the client experience because it reads ?tab= via useSearchParams.
export default function HomePage() {
  return (
    <Suspense>
      <HomeExperience />
    </Suspense>
  );
}
