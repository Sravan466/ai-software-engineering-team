import "@/components/shell/shell.css";
import AppShell from "@/components/shell/AppShell";

// The whole app lives inside the v3 workspace shell: a persistent left sidebar
// (brand, New build, recent builds, Ollama status, settings/api) plus a top bar.
// The home composer (/), project detail (/projects/[id]) and settings all render
// into the shell's main column.
export default function AppLayout({ children }: { children: React.ReactNode }) {
  return <AppShell>{children}</AppShell>;
}
