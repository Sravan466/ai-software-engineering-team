import "./globals.css";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "AI Software Engineering Team",
  description: "Turn a product idea into production-ready software with a team of AI agents.",
};

// Bare root layout. Page chrome (header + centered main) lives in the (app)
// route group so full-bleed routes like /landing can opt out of it.
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
