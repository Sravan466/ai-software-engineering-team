// One drawn icon set for the whole app: 24×24 viewBox, 1.7 stroke, round caps.
// Nothing here is a unicode glyph or an emoji standing in for an icon.
import type { ReactNode } from "react";

function svg(children: ReactNode, strokeWidth = 1.7) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {children}
    </svg>
  );
}

export type IconName =
  | "menu"
  | "plus"
  | "gear"
  | "api"
  | "check"
  | "chevron"
  | "clock"
  | "dot"
  | "download"
  | "external"
  | "alert"
  | "info"
  | "refresh"
  | "undo"
  | "sparkle"
  | "github"
  | "rotate"
  | "arrowRight"
  | "stop"
  | "play"
  | "trash"
  | "file"
  | "folder"
  | "diagram"
  | "list";

export const Icon: Record<IconName, ReactNode> = {
  menu: svg(<path d="M4 6h16M4 12h16M4 18h16" />),
  plus: svg(<path d="M12 5v14M5 12h14" />),
  gear: svg(
    <>
      <circle cx="12" cy="12" r="3.2" />
      <path d="M19.1 14.5a1.6 1.6 0 0 0 .32 1.76l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.6 1.6 0 0 0-1.76-.32 1.6 1.6 0 0 0-.97 1.46V21a2 2 0 1 1-4 0v-.1a1.6 1.6 0 0 0-1.05-1.46 1.6 1.6 0 0 0-1.76.32l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.6 1.6 0 0 0 .32-1.76 1.6 1.6 0 0 0-1.46-.97H3a2 2 0 1 1 0-4h.1a1.6 1.6 0 0 0 1.46-1.05 1.6 1.6 0 0 0-.32-1.76l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.6 1.6 0 0 0 1.76.32H9a1.6 1.6 0 0 0 .97-1.46V3a2 2 0 1 1 4 0v.1a1.6 1.6 0 0 0 .97 1.46 1.6 1.6 0 0 0 1.76-.32l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.6 1.6 0 0 0-.32 1.76V9a1.6 1.6 0 0 0 1.46.97H21a2 2 0 1 1 0 4h-.1a1.6 1.6 0 0 0-1.46.97z" />
    </>,
    1.5,
  ),
  api: svg(<path d="M8 8.5 4 12l4 3.5M16 8.5 20 12l-4 3.5M13.5 5.5l-3 13" />),
  check: svg(<path d="m4.5 12.5 5 5 10-11" />, 2.2),
  chevron: svg(<path d="m9.5 5.5 6.5 6.5-6.5 6.5" />, 1.9),
  clock: svg(
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 7.5V12l3 1.8" />
    </>,
  ),
  dot: svg(<circle cx="12" cy="12" r="3.6" fill="currentColor" stroke="none" />),
  download: svg(<path d="M12 4v11m0 0 4.5-4.5M12 15l-4.5-4.5M4.5 19h15" />),
  external: svg(<path d="M14 4h6v6M20 4l-8.5 8.5M18 14.5V19a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h4.5" />),
  alert: svg(
    <>
      <path d="M12 4.5 2.8 20h18.4z" />
      <path d="M12 10v4.2M12 17.2h.01" />
    </>,
  ),
  info: svg(
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 11v5.5M12 7.8h.01" />
    </>,
  ),
  refresh: svg(<path d="M20 12a8 8 0 1 1-2.6-5.9M20 4.5V10h-5.5" />),
  undo: svg(<path d="M4 9h10a5 5 0 0 1 0 10h-4M4 9l4-4M4 9l4 4" />),
  sparkle: svg(<path d="M12 3.5 13.9 9l5.6 2-5.6 2-1.9 5.5L10.1 13 4.5 11l5.6-2zM18.5 3.5v3M20 5h-3" />),
  github: svg(
    <path d="M9 19.5c-4.3 1.3-4.3-2.2-6-2.6m12 5v-3.4a3 3 0 0 0-.8-2.3c2.7-.3 5.5-1.3 5.5-6a4.7 4.7 0 0 0-1.3-3.2 4.3 4.3 0 0 0-.1-3.3s-1.1-.3-3.5 1.3a12 12 0 0 0-6.3 0C6.1 3.4 5 3.7 5 3.7a4.3 4.3 0 0 0-.1 3.3A4.7 4.7 0 0 0 3.5 10.2c0 4.7 2.8 5.7 5.5 6a3 3 0 0 0-.8 2.3V22" />,
  ),
  rotate: svg(<path d="M4 12a8 8 0 1 1 2.6 5.9M4 19.5V14h5.5" />),
  arrowRight: svg(<path d="M4.5 12h15m0 0-5.5-5.5M19.5 12 14 17.5" />),
  // Run controls: a square halts, a triangle resumes — the transport vocabulary
  // every person already knows, drawn to this set's 24/1.7 spec.
  stop: svg(<rect x="6.5" y="6.5" width="11" height="11" rx="1.6" />),
  play: svg(<path d="M8 5.6v12.8a.7.7 0 0 0 1.07.6l10.2-6.4a.7.7 0 0 0 0-1.2L9.07 5a.7.7 0 0 0-1.07.6z" />),
  trash: svg(<path d="M4.5 7h15M9.5 7V5.2a1.2 1.2 0 0 1 1.2-1.2h2.6a1.2 1.2 0 0 1 1.2 1.2V7M6.5 7l.8 12a1.5 1.5 0 0 0 1.5 1.4h6.4a1.5 1.5 0 0 0 1.5-1.4l.8-12M10.5 11v5.5M13.5 11v5.5" />),
  file: svg(<path d="M13.5 3.5H7a1.5 1.5 0 0 0-1.5 1.5v14A1.5 1.5 0 0 0 7 20.5h10a1.5 1.5 0 0 0 1.5-1.5V8.5zM13.5 3.5v5h5" />),
  folder: svg(<path d="M3.5 6.5A1.5 1.5 0 0 1 5 5h3.8l1.7 2.2H19a1.5 1.5 0 0 1 1.5 1.5v9.8A1.5 1.5 0 0 1 19 20H5a1.5 1.5 0 0 1-1.5-1.5z" />),
  diagram: svg(<path d="M9 4.5h6v4H9zM3.5 15.5h5v4h-5zM15.5 15.5h5v4h-5zM12 8.5v3M6 15.5v-2a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v2" />),
  list: svg(<path d="M9 6.5h11M9 12h11M9 17.5h11M4.5 6.5h.01M4.5 12h.01M4.5 17.5h.01" />),
};
