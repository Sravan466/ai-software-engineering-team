"use client";

import { createContext, useContext, useEffect, useState, type ReactNode } from "react";

// What the workspace top bar shows. Pages populate this via `useChrome(...)`:
//   • title  — a build/project name (rendered prominent)
//   • sub    — a small mono label like "new build" / "settings"
//   • badge  — an optional status pill on the right
export type Chrome = { title?: string; sub?: string; badge?: ReactNode };

const ChromeValue = createContext<Chrome>({});
const ChromeSetter = createContext<(c: Chrome) => void>(() => {});

export function ShellChromeProvider({ children }: { children: ReactNode }) {
  const [chrome, setChrome] = useState<Chrome>({});
  return (
    <ChromeSetter.Provider value={setChrome}>
      <ChromeValue.Provider value={chrome}>{children}</ChromeValue.Provider>
    </ChromeSetter.Provider>
  );
}

// Read the current chrome (used by the shell's top bar).
export function useChromeValue() {
  return useContext(ChromeValue);
}

// Set the top-bar chrome for the lifetime of a page. Pass a stable `deps` array
// (e.g. the values interpolated into the chrome) — the bar updates when they
// change and is cleared when the page unmounts.
export function useChrome(chrome: Chrome, deps: unknown[]) {
  const set = useContext(ChromeSetter);
  useEffect(() => {
    set(chrome);
    return () => set({});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}
