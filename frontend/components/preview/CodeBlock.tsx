"use client";

import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import vscDarkPlus from "react-syntax-highlighter/dist/esm/styles/prism/vsc-dark-plus";

// Map a file path / artifact language tag to a Prism language id (VS Code Dark+ palette).
const EXT_LANG: Record<string, string> = {
  js: "jsx",
  jsx: "jsx",
  mjs: "javascript",
  cjs: "javascript",
  ts: "typescript",
  tsx: "tsx",
  py: "python",
  rb: "ruby",
  go: "go",
  rs: "rust",
  java: "java",
  php: "php",
  json: "json",
  yml: "yaml",
  yaml: "yaml",
  toml: "toml",
  md: "markdown",
  mdx: "markdown",
  css: "css",
  scss: "scss",
  html: "markup",
  htm: "markup",
  xml: "markup",
  svg: "markup",
  vue: "markup",
  sh: "bash",
  bash: "bash",
  zsh: "bash",
  sql: "sql",
  graphql: "graphql",
  gql: "graphql",
};

function detectLang(path: string, tag?: string): string {
  const base = (path.split("/").pop() || "").toLowerCase();
  if (base.includes("dockerfile")) return "docker";
  if (base === "makefile") return "makefile";
  if (base.startsWith(".env")) return "bash";
  const ext = base.includes(".") ? base.split(".").pop()! : "";
  if (ext && EXT_LANG[ext]) return EXT_LANG[ext];
  const t = (tag || "").toLowerCase();
  if (EXT_LANG[t]) return EXT_LANG[t];
  // Accept a tag that's already a prism id.
  if (/^[a-z]+$/.test(t)) return t;
  return "text";
}

export default function CodeBlock({
  code,
  path,
  tag,
}: {
  code: string;
  path: string;
  tag?: string;
}) {
  return (
    <SyntaxHighlighter
      language={detectLang(path, tag)}
      style={vscDarkPlus}
      showLineNumbers
      customStyle={{
        margin: 0,
        padding: "16px 14px",
        maxHeight: 560,
        overflow: "auto",
        background: "transparent",
        fontSize: 12.5,
        lineHeight: 1.6,
      }}
      lineNumberStyle={{
        minWidth: "2.6em",
        paddingRight: "1em",
        color: "var(--dim, #4b4b57)",
        userSelect: "none",
      }}
      codeTagProps={{
        style: {
          fontFamily:
            "var(--font-mono, ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace)",
        },
      }}
    >
      {code}
    </SyntaxHighlighter>
  );
}
