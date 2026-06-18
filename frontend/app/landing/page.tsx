import type { Metadata } from "next";
import Link from "next/link";
import Blueprint from "@/components/blueprint/Blueprint";

export const metadata: Metadata = {
  title: "Ship the whole engineering team — AI Software Engineering Team",
  description:
    "Eight specialist agents take one idea to production-ready software, pausing for your approval at every phase.",
};

// Full-bleed marketing landing (Direction A · Blueprint). Lives outside the
// (app) route group, so it renders without the app header / centered main —
// instead it carries its own on-brand nav into the rest of the app.
export default function LandingPage() {
  return (
    <Blueprint
      nav={
        <nav className="bp-nav">
          <Link className="bp-nav-link" href="/">
            projects
          </Link>
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
      }
    />
  );
}
