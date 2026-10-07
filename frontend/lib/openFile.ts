/**
 * "Show me that line" from anywhere on the build page (#77).
 *
 * A security finding says `backend/routes/users.js:42`; the file lives in the Files
 * view of the phase that wrote it, further down the page. A finding asks for it with
 * `openFile`, and the page opens that phase, switches it to Files and scrolls the code
 * to the line. An event rather than a prop chain: the finding sits inside a decision
 * panel that knows nothing of the phase list, and threading a callback through every
 * panel between them is how a link quietly stops working when one of them is moved.
 */

export type FileJump = {
  /** The phase that wrote the file: its Files view is where it opens. */
  phase: string;
  /** The file as the finding names it — placed (`backend/…`) or as the agent wrote it. */
  path: string;
  line?: number | null;
};

const EVENT = "aiteam:open-file";

export function openFile(jump: FileJump): void {
  window.dispatchEvent(new CustomEvent<FileJump>(EVENT, { detail: jump }));
}

/** Listen for `openFile`. Returns the unsubscribe. */
export function onOpenFile(handler: (jump: FileJump) => void): () => void {
  const listener = (e: Event) => handler((e as CustomEvent<FileJump>).detail);
  window.addEventListener(EVENT, listener);
  return () => window.removeEventListener(EVENT, listener);
}

/**
 * One file, however much of its path each side wrote: `users.js` and
 * `backend/routes/users.js` match; `admin/users.js` and `routes/users.js` don't.
 * The tree also renames an agent's own root (`server/app/x.py` is placed at
 * `backend/app/x.py`), so two paths that agree below their first folder match too.
 */
export function samePath(a: string, b: string): boolean {
  const x = a.toLowerCase().replace(/^\.?\//, "");
  const y = b.toLowerCase().replace(/^\.?\//, "");
  if (x === y || x.endsWith(`/${y}`) || y.endsWith(`/${x}`)) return true;
  // Only between an agent's own root and a placed one: `backend/src/index.js` and
  // `frontend/src/index.js` are two files.
  const side = (p: string) => ["backend", "frontend"].includes(p.split("/")[0]);
  if (side(x) === side(y)) return false;
  const below = (p: string) => (p.includes("/") ? p.slice(p.indexOf("/") + 1) : "");
  return below(x) !== "" && below(x).includes("/") && below(x) === below(y);
}
