"use client";

import { useMemo, useState, type ReactElement, type ReactNode } from "react";
import CodeBlock from "@/components/preview/CodeBlock";
import { Icon } from "@/components/shell/icons";
import { fileSummary, type PayloadFile } from "./payload";

/**
 * A file tree beside the file's contents — the thing the approval gate was missing.
 *
 * Backend and Frontend phases hand over whole source trees. Reviewing them from a
 * flat list of forty paths is technically possible and practically nobody does it, so
 * paths are folded back into directories and the deepest common prefix is opened by
 * default: the reviewer lands on real code, not on a closed root folder.
 */

type Node = {
  name: string;
  path: string;
  file?: PayloadFile;
  children: Node[];
};

function buildTree(files: PayloadFile[]): Node[] {
  const root: Node = { name: "", path: "", children: [] };

  for (const file of files) {
    const parts = file.path.split("/").filter(Boolean);
    let cursor = root;
    parts.forEach((part, i) => {
      const isLeaf = i === parts.length - 1;
      const path = parts.slice(0, i + 1).join("/");
      let next = cursor.children.find((c) => c.path === path);
      if (!next) {
        next = { name: part, path, children: [] };
        cursor.children.push(next);
      }
      if (isLeaf) next.file = file;
      cursor = next;
    });
  }

  // Directories first, then files, each alphabetical — the ordering every file
  // browser uses, so nobody has to learn this one.
  const sort = (nodes: Node[]): Node[] =>
    nodes
      .map((n) => ({ ...n, children: sort(n.children) }))
      .sort((a, b) => {
        const aDir = a.children.length > 0;
        const bDir = b.children.length > 0;
        if (aDir !== bDir) return aDir ? -1 : 1;
        return a.name.localeCompare(b.name);
      });

  return sort(root.children);
}

function allDirectories(nodes: Node[], into: Set<string> = new Set()): Set<string> {
  for (const node of nodes) {
    if (node.children.length) {
      into.add(node.path);
      allDirectories(node.children, into);
    }
  }
  return into;
}

export default function FileBrowser({
  files,
  /** Rendered in the open file's header. The Ship review puts per-file redo here,
   *  so "this one is wrong" is answered where the wrong thing is being read. */
  renderAction,
}: {
  files: PayloadFile[];
  renderAction?: (file: PayloadFile) => ReactNode;
}) {
  const tree = useMemo(() => buildTree(files), [files]);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [selected, setSelected] = useState(files[0]?.path ?? "");

  const current = files.find((f) => f.path === selected) ?? files[0];
  if (!current) return null;

  const toggle = (path: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });

  const rows: ReactElement[] = [];
  const walk = (nodes: Node[], depth: number) => {
    for (const node of nodes) {
      const isDir = node.children.length > 0;
      const base = 8 + depth * 13;
      // Files line up under their folder's name, past the chevron column, so the
      // indent reads as hierarchy rather than as a ragged left edge.
      const indent = { paddingLeft: isDir ? base : base + 19 };

      if (isDir) {
        const open = !collapsed.has(node.path);
        rows.push(
          <button
            key={node.path}
            className="treerow treedir"
            style={indent}
            aria-expanded={open}
            onClick={() => toggle(node.path)}
          >
            <span className={"tree-chev" + (open ? " open" : "")} aria-hidden="true">
              {Icon.chevron}
            </span>
            <span className="tree-name">{node.name}</span>
          </button>,
        );
        if (open) walk(node.children, depth + 1);
        continue;
      }

      rows.push(
        <button
          key={node.path}
          className="treerow treefile"
          style={indent}
          aria-current={node.path === current.path}
          onClick={() => setSelected(node.path)}
          title={node.path}
        >
          <span className="tree-name">{node.name}</span>
          <span className="tree-lines">{node.file?.lines}</span>
        </button>,
      );
    }
  };
  walk(tree, 0);

  return (
    <div className="files">
      <div className="files-tree">
        <div className="files-tree-head">
          <span className="label">{fileSummary(files)}</span>
          <button
            className="btn btn-sm btn-ghost"
            onClick={() =>
              setCollapsed((prev) => (prev.size ? new Set() : allDirectories(tree)))
            }
          >
            {collapsed.size ? "Expand all" : "Collapse all"}
          </button>
        </div>
        <div className="files-tree-body" role="tree" aria-label="Generated files">
          {rows}
        </div>
      </div>

      <div className="files-code">
        <div className="code-pane-head">
          <span className="mono file-path">{current.path}</span>
          <span className="file-facts">
            <span className="badge badge-mono">{current.language || "code"}</span>
            <span className="mono dim">{current.lines} lines</span>
            {renderAction?.(current)}
          </span>
        </div>
        <CodeBlock code={current.content} path={current.path} tag={current.language} />
      </div>
    </div>
  );
}
