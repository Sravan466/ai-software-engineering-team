"""The boilerplate, written by the platform — never something a model must remember.

A run produced no `package.json`, no lockfile, no `tsconfig`, no `tailwind.config`,
no migrations and no `.env.example`, and nothing in the archive ran. None of those
files is application logic; every one of them follows from decisions already made —
the stack charter, the imports the agents wrote, the data model System Design drew.
So the platform writes them, and the agents write only what is specific to the app.

What the scaffold also does is fix the handful of mechanical mistakes that stop a
Next.js build and that no amount of prompting reliably prevents: a component using
state in the app router without `"use client"`, a `<Link>` wrapping an `<a>` (an
error since Next 13), a stylesheet imported but never written. Each fix is recorded
against the file it was made in, so the file browser can say what the platform
changed and why.
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from app.build import layout
from app.build import packages as pkg
from app.build.layout import BACKEND, FRONTEND

# ── what the platform owns ───────────────────────────────────────────────────
_OWNED_FRONTEND = re.compile(
    r"^(package(-lock)?\.json|yarn\.lock|pnpm-lock\.yaml|[tj]sconfig\.json|next-env\.d\.ts|"
    r"next\.config\.[cm]?[jt]s|tailwind\.config\.[cm]?[jt]s|postcss\.config\.[cm]?[jt]s|"
    r"vite\.config\.[cm]?[jt]s|jest\.config\.[cm]?[jt]s|\.env\.example)$"
)
_OWNED_BACKEND = re.compile(
    r"^(package(-lock)?\.json|yarn\.lock|pnpm-lock\.yaml|requirements(-dev)?\.txt|"
    r"\.env\.example|migrate\.py|scripts/(migrate|check)\.c?js)$"
)
_OWNED_ROOT = re.compile(r"^(package(-lock)?\.json|\.gitignore)$")


def platform_owned(path: str) -> bool:
    """Whether the platform writes this path — so an agent's copy is replaced."""
    p = layout.clean(path)
    side = layout.side_of(p)
    rel = layout.relative_to_side(p)
    if side == FRONTEND:
        return bool(_OWNED_FRONTEND.match(rel))
    if side == BACKEND:
        return bool(_OWNED_BACKEND.match(rel))
    return bool(_OWNED_ROOT.match(p))


#: Files that are one file under several names. When the platform writes one of a
#: family, an agent's other spelling beside it is not an extra file but a second,
#: contradicting copy — a tsconfig.json beside the platform's jsconfig.json makes
#: `next build` demand TypeScript, and two next.configs leave Next to pick one.
_FAMILIES = (
    re.compile(r"^[tj]sconfig\.json$"),
    re.compile(r"^next\.config\.[cm]?[jt]s$"),
    re.compile(r"^tailwind\.config\.[cm]?[jt]s$"),
    re.compile(r"^postcss\.config\.[cm]?[jt]s$"),
    re.compile(r"^vite\.config\.[cm]?[jt]s$"),
    re.compile(r"^jest\.config\.[cm]?[jt]s$"),
    re.compile(r"^(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|package\.json)$"),
)


def superseded(path: str, written: set[str]) -> bool:
    """Whether an agent's copy of `path` is replaced by what the platform wrote.

    True for the path the scaffold wrote itself, and for any other name in the same
    family in the same folder — the platform's manifest replaces the lockfile beside
    it, its jsconfig replaces a tsconfig. A platform-owned file the scaffold did not
    touch at all (a Vite project's jest.config) is the only copy there is, and stays.
    """
    p = layout.clean(path)
    if p in written:
        return True
    folder, _, name = p.rpartition("/")
    for family in _FAMILIES:
        if family.match(name) and any(
            w.rpartition("/")[0] == folder and family.match(w.rpartition("/")[2]) for w in written
        ):
            return True
    return False


@dataclass
class ScaffoldFile:
    path: str
    content: str
    purpose: str


@dataclass
class Scaffold:
    files: list[ScaffoldFile] = field(default_factory=list)
    #: agent path -> (rewritten content, what was changed and why)
    rewrites: dict[str, tuple[str, list[str]]] = field(default_factory=dict)
    frontend: Optional[str] = None
    backend: Optional[str] = None
    backend_framework: Optional[str] = None
    database: Optional[str] = None
    commands: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, path: str, content: str, purpose: str) -> None:
        self.files.append(ScaffoldFile(layout.clean(path), content, purpose))

    def paths(self) -> set[str]:
        return {f.path for f in self.files}

    def as_dict(self) -> dict:
        return {
            "frontend": self.frontend,
            "backend": self.backend,
            "backend_framework": self.backend_framework,
            "database": self.database,
            "commands": list(self.commands),
            "notes": list(self.notes),
            "files": sorted(self.paths()),
        }


# ── reading what the agents wrote ────────────────────────────────────────────
_JS_EXT = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
_TS_EXT = (".ts", ".tsx")
_SPEC = re.compile(
    r"""(?:^|[\s;])(?:import\s+(?:[\w*{}\s,$]+\s+from\s+)?|export\s+[\w*{}\s,$]+\s+from\s+)['"]([^'"]+)['"]"""
    r"""|\brequire\(\s*['"]([^'"]+)['"]\s*\)|\bimport\(\s*['"]([^'"]+)['"]\s*\)""",
    re.MULTILINE,
)
_ENV_JS = re.compile(r"\b(?:process\.env|import\.meta\.env)\.([A-Z][A-Z0-9_]*)")
_ENV_JS_BRACKET = re.compile(r"""\bprocess\.env\[\s*['"]([A-Z][A-Z0-9_]*)['"]\s*\]""")
_ENV_PY = re.compile(
    r"""\b(?:os\.getenv|os\.environ\.get|getenv|environ\.get)\(\s*['"]([A-Z][A-Z0-9_]*)['"]"""
    r"""|\bos\.environ\[\s*['"]([A-Z][A-Z0-9_]*)['"]\s*\]"""
)


def js_imports(content: str) -> list[str]:
    out = []
    for m in _SPEC.finditer(content or ""):
        spec = m.group(1) or m.group(2) or m.group(3)
        if spec:
            out.append(spec.strip())
    return out


def py_imports(content: str) -> list[tuple[str, int]]:
    """(module, level) for every import; level > 0 is a relative import."""
    try:
        tree = ast.parse(content or "")
    except (SyntaxError, ValueError):
        found = re.findall(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", content or "", re.MULTILINE)
        return [((a or b), 0) for a, b in found]
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((alias.name, 0) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            out.append((node.module or "", node.level or 0))
    return out


def env_vars(files: Iterable[tuple[str, str]]) -> list[str]:
    seen: dict[str, None] = {}
    for path, content in files:
        if path.endswith(".py"):
            for a, b in _ENV_PY.findall(content or ""):
                seen[a or b] = None
        elif path.endswith(_JS_EXT):
            for name in _ENV_JS.findall(content or "") + _ENV_JS_BRACKET.findall(content or ""):
                seen[name] = None
    return [n for n in seen if n not in ("NODE_ENV",)]


def _side_files(tree: dict[str, str], side: str) -> dict[str, str]:
    """Relative path -> content for one side of the build."""
    return {
        layout.relative_to_side(p): c for p, c in tree.items() if layout.side_of(p) == side
    }


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "app").lower()).strip("-")[:40].strip("-")
    return s or "app"


def _is_test(rel: str) -> bool:
    low = rel.lower()
    return bool(
        re.search(r"(^|/)(__tests__|tests?)/", low)
        or re.search(r"\.(test|spec)\.[cm]?[jt]sx?$", low)
        or re.search(r"(^|/)test_[^/]*\.py$|_test\.py$", low)
    )


# ── framework detection ──────────────────────────────────────────────────────
def detect_frontend(files: dict[str, str], charter_token: Optional[str]) -> Optional[str]:
    if not files:
        return None
    specs = [s for c in files.values() for s in js_imports(c)]
    if any(s == "next" or s.startswith("next/") for s in specs):
        return "nextjs"
    if any(p.endswith(".vue") for p in files) or "vue" in specs:
        return "vue"
    if any(p.endswith(".svelte") for p in files):
        return "svelte"
    if charter_token in ("nextjs", "react", "vue", "svelte", "angular", "nuxt"):
        if charter_token == "react" and _looks_like_next(files):
            return "nextjs"
        return charter_token
    if _looks_like_next(files):
        return "nextjs"
    if any(re.match(r"src/(main|index)\.[jt]sx?$", p) for p in files) or "index.html" in files:
        return "react"
    return "nextjs"


def _looks_like_next(files: dict[str, str]) -> bool:
    return any(re.match(r"(src/)?(pages|app)/", p) for p in files if p.endswith(_JS_EXT))


def detect_backend(files: dict[str, str], charter) -> tuple[Optional[str], Optional[str]]:
    """(language, framework) of the backend side."""
    if not files:
        return None, None
    code = [p for p in files if not _is_test(p)]
    py = [p for p in code if p.endswith(".py")]
    js = [p for p in code if p.endswith(_JS_EXT)]
    language = "python" if len(py) >= len(js) and py else ("typescript" if any(p.endswith(_TS_EXT) for p in js) else ("javascript" if js else None))
    if language is None and charter is not None and charter.get("language"):
        language = charter.get("language").token
    text = "\n".join(files.values())
    framework = None
    for token, pattern in (
        ("fastapi", r"\bfrom\s+fastapi\b|\bFastAPI\("),
        ("flask", r"\bfrom\s+flask\b|\bFlask\("),
        ("django", r"\bfrom\s+django\b|manage\.py"),
        ("nestjs", r"@nestjs/"),
        ("express", r"""require\(['"]express['"]\)|from\s+['"]express['"]"""),
    ):
        if re.search(pattern, text):
            framework = token
            break
    if framework is None and charter is not None and charter.get("backend_framework"):
        framework = charter.get("backend_framework").token
    return language, framework


# ── codemods: mechanical fixes to what the agents wrote ──────────────────────
_HOOK = re.compile(
    r"\buse(State|Effect|Context|Reducer|Ref|Memo|Callback|Router|Pathname|SearchParams|Params|"
    r"LayoutEffect|Transition|Id|FormState|Optimistic)\s*\(|\bcreateContext\s*\("
)
_HANDLER = re.compile(r"\son[A-Z]\w*=\{")
_DIRECTIVE = re.compile(r"""^\s*(?:(?://[^\n]*\n|/\*[\s\S]*?\*/)\s*)*['"]use (client|server)['"]""")
_METADATA = re.compile(r"\bexport\s+(const\s+metadata\b|async\s+function\s+generateMetadata\b|function\s+generateMetadata\b)")
_LINK_A = re.compile(r"<Link\b([^>]*)>\s*<a\b([^>]*)>([\s\S]*?)</a>\s*</Link>")


def _client_directive(rel: str, content: str, app_router: bool) -> Optional[str]:
    """`"use client"` on a module the app router would render on the server.

    Any folder, not only `components/`: a real run's `app/page.tsx` imported a
    `pages/RecipeList.jsx` that used state, and `next build` failed on it. The
    directive is inert where the pages router renders a file, so adding it wherever
    state or handlers appear costs nothing and removes the whole class of failure.
    """
    if not app_router or not rel.endswith(_JS_EXT) or _is_test(rel):
        return None
    if re.search(r"(^|/)(next|tailwind|postcss|jest|vite)\.config\.", rel):
        return None
    # Never on what only runs on the server: route handlers and middleware export
    # functions Next calls there, and an async component cannot be a client one.
    if re.search(r"(^|/)(route|middleware)\.[cm]?[jt]sx?$", rel):
        return None
    if re.search(r"export\s+default\s+async\s+function|export\s+default\s+async\s*\(", content):
        return None
    if _DIRECTIVE.match(content) or _METADATA.search(content):
        return None
    client_only = any(
        spec in _CLIENT_ONLY or pkg.npm_package(spec) in _CLIENT_ONLY for spec in js_imports(content)
    )
    if _HOOK.search(content) or _HANDLER.search(content) or client_only:
        return "'use client';\n\n" + content
    return None


#: Packages that create React context or touch the browser when imported, so a Server
#: Component importing one fails `next build` ("createContext is not a function")
#: whether or not it calls a hook itself.
_CLIENT_ONLY = frozenset({
    "react-router-dom", "@headlessui/react", "framer-motion", "react-hot-toast",
    "react-toastify", "sonner", "zustand", "jotai", "@tanstack/react-query", "swr",
    "react-redux", "recharts", "react-chartjs-2", "react-hook-form", "formik",
    "react-datepicker", "styled-components", "@emotion/react", "@emotion/styled",
    "@mui/material", "@mui/icons-material",
    # next-auth is mostly server code (handlers, providers, middleware); only its
    # React bindings are client-only, so the subpath is listed rather than the package.
    "next-auth/react",
})


def _link_children(content: str) -> Optional[str]:
    if "<Link" not in content or not _LINK_A.search(content):
        return None

    def merge(m: "re.Match") -> str:
        link_attrs = re.sub(r"\s(passHref|legacyBehavior)\b(=\{[^}]*\})?", "", m.group(1))
        a_attrs = re.sub(r"\shref=(\"[^\"]*\"|'[^']*'|\{[^}]*\})", "", m.group(2))
        return f"<Link{link_attrs}{a_attrs}>{m.group(3)}</Link>"

    return _LINK_A.sub(merge, content)


# ── the frontend ─────────────────────────────────────────────────────────────
_TAILWIND_CSS = "@tailwind base;\n@tailwind components;\n@tailwind utilities;\n"


def _deps_for(files: dict[str, str], base: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """(dependencies, devDependencies) from what the files import."""
    deps: dict[str, str] = dict(base)
    dev: dict[str, str] = {}
    for rel, content in files.items():
        if not rel.endswith(_JS_EXT):
            continue
        testing = _is_test(rel)
        for spec in js_imports(content):
            if pkg.is_node_builtin(spec):
                continue
            name = pkg.npm_package(spec)
            if not name or name.startswith("@/") or name == "@":
                continue
            version = pkg.npm_version(name)
            if version is None:
                continue  # the compile gate reports it; the manifest cannot pin it
            if name in deps:
                continue
            if testing or pkg.is_npm_dev(name):
                dev.setdefault(name, version)
            else:
                deps[name] = version
    return dict(sorted(deps.items())), dict(sorted(dev.items()))


def _json(value: object) -> str:
    return json.dumps(value, indent=2) + "\n"


def _missing_css(files: dict[str, str]) -> list[str]:
    """Stylesheets imported by a relative path that nobody wrote."""
    missing: list[str] = []
    for rel, content in files.items():
        if not rel.endswith(_JS_EXT):
            continue
        base = rel.rsplit("/", 1)[0] if "/" in rel else ""
        for spec in js_imports(content):
            if not spec.startswith(".") or not spec.endswith((".css", ".scss")):
                continue
            target = layout.join(base, spec)
            if target not in files and target not in missing:
                missing.append(target)
    return missing


def _frontend_env(files: dict[str, str]) -> str:
    names = env_vars(files.items())
    lines = ["# Generated by the platform from the variables the frontend reads.", ""]
    if not names:
        lines.append("# The frontend reads no environment variables.")
    for name in names:
        default = "http://localhost:8000" if re.search(r"API|BACKEND|SERVER", name) and "URL" in name else ""
        lines.append(f"{name}={default}")
    return "\n".join(lines) + "\n"


def _scaffold_next(sc: Scaffold, files: dict[str, str], slug: str, product: str) -> None:
    ts = any(p.endswith(_TS_EXT) for p in files)
    src = "src/" if any(p.startswith(("src/app/", "src/pages/")) for p in files) else ""
    app_router = any(re.match(rf"{src}app/.*page\.[jt]sx?$", p) for p in files)
    pages_router = any(re.match(rf"{src}pages/.*\.[jt]sx?$", p) for p in files)
    ext = "tsx" if ts else "jsx"
    fe = lambda rel: f"{FRONTEND}/{rel}"  # noqa: E731

    tests = [p for p in files if _is_test(p) and p.endswith(_JS_EXT)]
    deps, dev = _deps_for(files, {k: pkg.NPM_FRAMEWORK[k] for k in ("next", "react", "react-dom")})
    dev.update({k: pkg.NPM_FRAMEWORK[k] for k in ("tailwindcss", "postcss", "autoprefixer")})
    if ts:
        dev.update({k: pkg.NPM_FRAMEWORK[k] for k in ("typescript", "@types/react", "@types/react-dom", "@types/node")})
    scripts = {"dev": "next dev", "build": "next build", "start": "next start"}
    if tests:
        dev.update({k: pkg.NPM_DEV[k] for k in ("jest", "jest-environment-jsdom", "@testing-library/react", "@testing-library/jest-dom", "@testing-library/dom")})
        scripts["test"] = "jest"
        sc.add(
            fe("jest.config.js"),
            "const nextJest = require('next/jest');\n\n"
            "const createJestConfig = nextJest({ dir: './' });\n\n"
            "module.exports = createJestConfig({\n"
            "  testEnvironment: 'jsdom',\n"
            "  moduleNameMapper: { '^@/(.*)$': '<rootDir>/" + src + "$1' },\n"
            "});\n",
            "Runs the QA suite's frontend tests with Next's own Jest preset.",
        )
    sc.add(
        fe("package.json"),
        _json({
            "name": f"{slug}-frontend",
            "version": "0.1.0",
            "private": True,
            "scripts": scripts,
            "dependencies": deps,
            "devDependencies": dict(sorted(dev.items())),
        }),
        "Dependencies derived from what the frontend imports, pinned to known-good ranges.",
    )
    sc.add(
        fe("next.config.js"),
        "/** @type {import('next').NextConfig} */\n"
        "// Written by the platform. Type and lint errors are reported by the build's own\n"
        "// compile check before it ships; `next build` fails only on what cannot run.\n"
        "module.exports = {\n"
        "  reactStrictMode: true,\n"
        "  typescript: { ignoreBuildErrors: true },\n"
        "  eslint: { ignoreDuringBuilds: true },\n"
        "};\n",
        "Next.js configuration.",
    )
    globs = [f"./{src}{d}/**/*.{{js,jsx,ts,tsx,mdx}}" for d in ("app", "pages", "components")]
    if not src:
        globs.append("./src/**/*.{js,jsx,ts,tsx,mdx}")
    sc.add(
        fe("tailwind.config.js"),
        "/** @type {import('tailwindcss').Config} */\n"
        "module.exports = {\n"
        f"  content: {json.dumps(globs)},\n"
        "  theme: { extend: {} },\n"
        "  plugins: [],\n"
        "};\n",
        "Tailwind, scanning every place the frontend keeps markup.",
    )
    sc.add(
        fe("postcss.config.js"),
        "module.exports = {\n  plugins: {\n    tailwindcss: {},\n    autoprefixer: {},\n  },\n};\n",
        "PostCSS pipeline for Tailwind.",
    )
    paths_cfg = {"@/*": [f"./{src}*"]}
    if ts:
        sc.add(
            fe("tsconfig.json"),
            _json({
                "compilerOptions": {
                    "target": "ES2017",
                    "lib": ["dom", "dom.iterable", "esnext"],
                    "allowJs": True,
                    "skipLibCheck": True,
                    "strict": False,
                    "noEmit": True,
                    "esModuleInterop": True,
                    "module": "esnext",
                    "moduleResolution": "bundler",
                    "resolveJsonModule": True,
                    "isolatedModules": True,
                    "jsx": "preserve",
                    "incremental": True,
                    "plugins": [{"name": "next"}],
                    "paths": paths_cfg,
                },
                "include": ["next-env.d.ts", "**/*.ts", "**/*.tsx", ".next/types/**/*.ts"],
                "exclude": ["node_modules"],
            }),
            "TypeScript configuration, with the @/ import alias.",
        )
        sc.add(
            fe("next-env.d.ts"),
            '/// <reference types="next" />\n/// <reference types="next/image-types/global" />\n',
            "Next.js type references.",
        )
    else:
        sc.add(
            fe("jsconfig.json"),
            _json({"compilerOptions": {"baseUrl": ".", "paths": paths_cfg}}),
            "The @/ import alias for a JavaScript project.",
        )

    # The pages and layout Next.js refuses to build without.
    if app_router:
        layout_file = next((p for p in files if re.match(rf"{src}app/layout\.[jt]sx?$", p)), None)
        if layout_file is None:
            if f"{src}app/globals.css" not in files:
                sc.add(fe(f"{src}app/globals.css"), _TAILWIND_CSS, "Global styles: Tailwind's layers.")
            sc.add(
                fe(f"{src}app/layout.{ext}"),
                "import './globals.css';\n\n"
                f"export const metadata = {{ title: {json.dumps(product)} }};\n\n"
                + (
                    "export default function RootLayout({ children }: { children: React.ReactNode }) {\n"
                    if ts
                    else "export default function RootLayout({ children }) {\n"
                )
                + "  return (\n    <html lang=\"en\">\n      <body>{children}</body>\n    </html>\n  );\n}\n",
                "The root layout the app router requires; no agent wrote one.",
            )
    elif pages_router:
        if not any(re.match(rf"{src}pages/_app\.[jt]sx?$", p) for p in files):
            if f"{src}styles/globals.css" not in files:
                sc.add(fe(f"{src}styles/globals.css"), _TAILWIND_CSS, "Global styles: Tailwind's layers.")
            sc.add(
                fe(f"{src}pages/_app.{ext}"),
                "import '../styles/globals.css';\n\n"
                "export default function App({ Component, pageProps }"
                + (": { Component: any; pageProps: any }" if ts else "")
                + ") {\n  return <Component {...pageProps} />;\n}\n",
                "Loads the global stylesheet for every page.",
            )
    else:
        # Components but no page: `next build` stops at "couldn't find any pages or
        # app directory". A page that says so is better than a build that cannot run.
        names = sorted({re.sub(r"\.[jt]sx?$", "", p.rsplit("/", 1)[-1]) for p in files if p.endswith(_JS_EXT)})
        sc.add(fe("app/globals.css"), _TAILWIND_CSS, "Global styles: Tailwind's layers.")
        sc.add(
            fe(f"app/layout.{ext}"),
            "import './globals.css';\n\n"
            f"export const metadata = {{ title: {json.dumps(product)} }};\n\n"
            + ("export default function RootLayout({ children }: { children: React.ReactNode }) {\n" if ts
               else "export default function RootLayout({ children }) {\n")
            + "  return (\n    <html lang=\"en\">\n      <body>{children}</body>\n    </html>\n  );\n}\n",
            "The root layout the app router requires.",
        )
        sc.add(
            fe(f"app/page.{ext}"),
            "export default function Home() {\n  return (\n    <main className=\"mx-auto max-w-2xl p-10\">\n"
            f"      <h1 className=\"text-3xl font-bold\">{product}</h1>\n"
            "      <p className=\"mt-4 text-gray-600\">No page was generated for this build. Its components are:</p>\n"
            f"      <ul className=\"mt-4 list-disc pl-6\">{''.join(f'<li>{n}</li>' for n in names[:20])}</ul>\n"
            "    </main>\n  );\n}\n",
            "A placeholder index: the build produced components but no page.",
        )
        sc.notes.append("No page was generated, so the platform added a placeholder index page.")

    for css in _missing_css(files):
        name = css.rsplit("/", 1)[-1]
        body = _TAILWIND_CSS if re.match(r"(globals?|index|app|main|styles?|tailwind)\.s?css$", name) else f"/* {name} */\n"
        sc.add(fe(css), body, "A stylesheet the code imports that no agent wrote.")

    sc.add(fe(".env.example"), _frontend_env(files), "The variables the frontend reads.")

    for rel, content in files.items():
        notes: list[str] = []
        updated = content
        directive = _client_directive(rel, updated, app_router)
        if directive is not None:
            reason = (
                "uses React state or event handlers"
                if _HOOK.search(updated) or _HANDLER.search(updated)
                else "imports a library that only runs in the browser"
            )
            updated = directive
            notes.append(f"added 'use client' — it {reason}, and the app router would render it on the server")
        linked = _link_children(updated)
        if linked is not None:
            updated = linked
            notes.append("merged <Link><a> into <Link> — Next.js 13+ rejects an <a> child")
        if notes:
            sc.rewrites[fe(rel)] = (updated, notes)

    sc.commands += ["npm install", "npm run build", "npm run dev --workspace frontend   # http://localhost:3000"]


def _scaffold_vite(sc: Scaffold, files: dict[str, str], slug: str, product: str, framework: str) -> None:
    fe = lambda rel: f"{FRONTEND}/{rel}"  # noqa: E731
    ts = any(p.endswith(_TS_EXT) for p in files)
    plugin = {
        "react": ("@vitejs/plugin-react", "react", ("react", "react-dom")),
        "vue": ("@vitejs/plugin-vue", "vue", ("vue",)),
        "svelte": ("@sveltejs/vite-plugin-svelte", "svelte", ("svelte",)),
    }[framework]
    plugin_pkg, plugin_fn, runtime = plugin
    deps, dev = _deps_for(files, {k: pkg.NPM_FRAMEWORK[k] for k in runtime})
    dev.update({k: pkg.NPM_FRAMEWORK[k] for k in ("vite", plugin_pkg, "tailwindcss", "postcss", "autoprefixer")})
    if ts:
        dev["typescript"] = pkg.NPM_FRAMEWORK["typescript"]
    import_name = "svelte" if framework == "svelte" else plugin_fn
    # The `@/` alias, configured once and written twice — for Vite, which resolves
    # it, and for the editor and the compile gate, which read it from the config.
    alias_root = "src" if any(p.startswith("src/") for p in files) else "."
    sc.add(
        fe("package.json"),
        _json({
            "name": f"{slug}-frontend",
            "version": "0.1.0",
            "private": True,
            "type": "module",
            "scripts": {"dev": "vite", "build": "vite build", "preview": "vite preview"},
            "dependencies": deps,
            "devDependencies": dict(sorted(dev.items())),
        }),
        "Dependencies derived from what the frontend imports, pinned to known-good ranges.",
    )
    plugin_import = (
        "import { svelte } from '@sveltejs/vite-plugin-svelte';"
        if import_name == "svelte"
        else f"import {plugin_fn} from '{plugin_pkg}';"
    )
    plugin_call = "svelte()" if import_name == "svelte" else f"{plugin_fn}()"
    sc.add(
        fe("vite.config.js"),
        "import { fileURLToPath, URL } from 'node:url';\n"
        "import { defineConfig } from 'vite';\n"
        f"{plugin_import}\n\n"
        "export default defineConfig({\n"
        f"  plugins: [{plugin_call}],\n"
        f"  resolve: {{ alias: {{ '@': fileURLToPath(new URL('./{alias_root}', import.meta.url)) }} }},\n"
        "});\n",
        "Vite configuration, with the @/ import alias.",
    )
    alias_target = "./src/*" if alias_root == "src" else "./*"
    options = {"baseUrl": ".", "paths": {"@/*": [alias_target]}}
    if ts:
        options = {
            "target": "ES2020",
            "module": "ESNext",
            "moduleResolution": "bundler",
            "skipLibCheck": True,
            "strict": False,
            "noEmit": True,
            **({"jsx": "react-jsx"} if framework == "react" else {}),
            **options,
        }
    sc.add(
        fe("tsconfig.json" if ts else "jsconfig.json"),
        _json({"compilerOptions": options, **({"include": ["src"]} if ts else {})}),
        "The @/ import alias, for the editor and the compile check.",
    )
    sc.add(
        fe("tailwind.config.js"),
        "/** @type {import('tailwindcss').Config} */\nexport default {\n"
        "  content: ['./index.html', './src/**/*.{js,jsx,ts,tsx,vue,svelte}'],\n"
        "  theme: { extend: {} },\n  plugins: [],\n};\n",
        "Tailwind, scanning the app's source.",
    )
    sc.add(
        fe("postcss.config.js"),
        "export default {\n  plugins: {\n    tailwindcss: {},\n    autoprefixer: {},\n  },\n};\n",
        "PostCSS pipeline for Tailwind.",
    )
    entry = next((p for p in files if re.match(r"src/(main|index)\.[jt]sx?$", p)), None)
    if entry is None and framework == "react":
        app = next((p for p in files if re.match(r"src/App\.[jt]sx?$", p)), None)
        entry = "src/main.tsx" if ts else "src/main.jsx"
        body = (
            "import React from 'react';\nimport ReactDOM from 'react-dom/client';\n"
            + (f"import App from './{app.rsplit('/', 1)[-1].rsplit('.', 1)[0]}';\n" if app else "")
            + "import './index.css';\n\n"
            + "ReactDOM.createRoot(document.getElementById('root')"
            + ("!" if ts else "")
            + ").render(\n  <React.StrictMode>\n    "
            + ("<App />" if app else f"<h1>{product}</h1>")
            + "\n  </React.StrictMode>,\n);\n"
        )
        sc.add(fe(entry), body, "The entry point that mounts the app; no agent wrote one.")
        if "src/index.css" not in files:
            sc.add(fe("src/index.css"), _TAILWIND_CSS, "Global styles: Tailwind's layers.")
    if "index.html" not in files and entry:
        sc.add(
            fe("index.html"),
            "<!doctype html>\n<html lang=\"en\">\n  <head>\n    <meta charset=\"UTF-8\" />\n"
            "    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />\n"
            f"    <title>{product}</title>\n  </head>\n  <body>\n    <div id=\"{'app' if framework != 'react' else 'root'}\"></div>\n"
            f"    <script type=\"module\" src=\"/{entry}\"></script>\n  </body>\n</html>\n",
            "The page Vite serves and builds from.",
        )
    for css in _missing_css(files):
        name = css.rsplit("/", 1)[-1]
        body = _TAILWIND_CSS if re.match(r"(globals?|index|app|main|styles?|tailwind)\.s?css$", name) else f"/* {name} */\n"
        sc.add(fe(css), body, "A stylesheet the code imports that no agent wrote.")
    sc.add(fe(".env.example"), _frontend_env(files), "The variables the frontend reads.")
    sc.commands += ["npm install", "npm run build", "npm run dev --workspace frontend"]


# ── the backend ──────────────────────────────────────────────────────────────
_DEFAULT_DB_URL = {
    "postgres": "postgresql://postgres:postgres@localhost:5432/app",
    "mysql": "mysql://root:root@localhost:3306/app",
    "mongodb": "mongodb://localhost:27017/app",
    "sqlite": "sqlite:///./app.db",
}


def _backend_env(files: dict[str, str], database: Optional[str], port: str) -> str:
    names = env_vars(files.items())
    lines = ["# Generated by the platform from the variables the backend reads.", ""]
    if "DATABASE_URL" not in names and database in _DEFAULT_DB_URL:
        names.insert(0, "DATABASE_URL")
    if not names:
        lines.append("# The backend reads no environment variables.")
    for name in names:
        if name in ("DATABASE_URL", "DB_URL"):
            value = _DEFAULT_DB_URL.get(database or "sqlite", _DEFAULT_DB_URL["sqlite"])
        elif name in ("MONGO_URI", "MONGODB_URI", "MONGO_URL"):
            value = _DEFAULT_DB_URL["mongodb"]
        elif re.search(r"SECRET|KEY|TOKEN|PASSWORD", name):
            value = "change-me"
        elif name == "PORT":
            value = port
        elif "URL" in name or "ORIGIN" in name:
            value = "http://localhost:3000"
        else:
            value = ""
        lines.append(f"{name}={value}")
    return "\n".join(lines) + "\n"


def _python_requirements(files: dict[str, str], framework: Optional[str], database: Optional[str], with_migrations: bool) -> str:
    local = _python_locals(files)
    main: dict[str, str] = {}
    test: dict[str, str] = {}
    for rel, content in files.items():
        if not rel.endswith(".py"):
            continue
        bucket = test if _is_test(rel) else main
        for module, level in py_imports(content):
            if level or not module:
                continue
            top = module.split(".")[0]
            if top in local or pkg.is_stdlib(top):
                continue
            found = pkg.pip_requirement(top)
            if found:
                dist, spec = found
                if dist not in main:
                    (test if dist in pkg.PIP_DEV else bucket)[dist] = spec
    if framework == "fastapi":
        main.setdefault("fastapi", pkg.PIP["fastapi"][1])
        main.setdefault("uvicorn[standard]", pkg.PIP["uvicorn"][1])
        if test or any("TestClient" in c for c in files.values()):
            test.setdefault("httpx", pkg.PIP["httpx"][1])
    elif framework == "flask":
        main.setdefault("Flask", pkg.PIP["flask"][1])
    elif framework == "django":
        main.setdefault("Django", pkg.PIP["django"][1])
    if with_migrations:
        has_driver = lambda *names: any(n in main for n in names)  # noqa: E731
        if database == "postgres" and not has_driver("psycopg2-binary", "psycopg[binary]"):
            main["psycopg2-binary"] = pkg.PIP["psycopg2"][1]
        if database == "mysql" and not has_driver("PyMySQL"):
            main["PyMySQL"] = pkg.PIP["pymysql"][1]
    if any(_is_test(p) for p in files if p.endswith(".py")):
        test.setdefault("pytest", pkg.PIP["pytest"][1])
    test = {k: v for k, v in test.items() if k not in main}
    lines = ["# Generated by the platform from what the backend imports.", ""]
    lines += [f"{name}{spec}" for name, spec in sorted(main.items(), key=lambda kv: kv[0].lower())]
    if test:
        lines += ["", "# tests"]
        lines += [f"{name}{spec}" for name, spec in sorted(test.items(), key=lambda kv: kv[0].lower())]
    return "\n".join(lines) + "\n"


def _python_locals(files: dict[str, str]) -> set[str]:
    """Top-level module names the backend's own files provide."""
    names: set[str] = set()
    for rel in files:
        if not rel.endswith(".py"):
            continue
        parts = rel.split("/")
        names.add(parts[0][:-3] if len(parts) == 1 else parts[0])
        # `tests/test_x.py` importing `conftest` or a sibling module by bare name.
        names.add(parts[-1][:-3])
    return names


def _python_entry(files: dict[str, str], framework: Optional[str]) -> Optional[str]:
    for rel in sorted(files, key=lambda p: (p.count("/"), p)):
        if not rel.endswith(".py") or _is_test(rel):
            continue
        content = files[rel]
        if framework == "fastapi":
            m = re.search(r"^(\w+)\s*=\s*FastAPI\(", content, re.MULTILINE)
            if m:
                return f"uvicorn {rel[:-3].replace('/', '.')}:{m.group(1)} --reload --port 8000"
        if framework == "flask":
            m = re.search(r"^(\w+)\s*=\s*Flask\(", content, re.MULTILINE)
            if m:
                return f"flask --app {rel[:-3].replace('/', '.')}:{m.group(1)} run --port 8000"
    if framework == "django" and "manage.py" in files:
        return "python manage.py runserver 8000"
    return None


_JS_ENTRIES = ("server.js", "index.js", "app.js", "main.js", "src/server.js", "src/index.js", "src/app.js", "src/main.js")


def _scaffold_node_backend(sc: Scaffold, files: dict[str, str], slug: str, database: Optional[str], with_migrations: bool) -> None:
    be = lambda rel: f"{BACKEND}/{rel}"  # noqa: E731
    code = {p: c for p, c in files.items() if p.endswith(_JS_EXT)}
    esm = any(re.search(r"^\s*import\s.+\sfrom\s|^\s*export\s", c, re.MULTILINE) for p, c in code.items() if not _is_test(p))
    cjs = any(re.search(r"\brequire\(|module\.exports", c) for p, c in code.items() if not _is_test(p))
    module_type = "module" if esm and not cjs else "commonjs"
    if esm and cjs:
        sc.notes.append(
            "The backend mixes `require` and `import`; it is set up as CommonJS, so files "
            "using `import` will need converting."
        )
    base = {}
    framework = sc.backend_framework
    if framework == "express":
        base["express"] = pkg.NPM_FRAMEWORK["express"]
    driver = {"postgres": "pg", "mysql": "mysql2", "sqlite": "better-sqlite3"}.get(database or "")
    if with_migrations and driver:
        base[driver] = pkg.NPM[driver]
    deps, dev = _deps_for(files, base)
    entry = next((e for e in _JS_ENTRIES if e in files), None)
    scripts = {"build": "node scripts/check.cjs"}
    if entry:
        scripts["start"] = f"node {entry}"
        scripts["dev"] = f"node --watch {entry}"
    if with_migrations and driver:
        scripts["migrate"] = "node scripts/migrate.cjs"
    tests = [p for p in files if _is_test(p) and p.endswith(_JS_EXT)]
    if tests:
        runner = "jest" if any("jest" in c or "describe(" in c for c in (files[t] for t in tests)) else "mocha"
        scripts["test"] = runner
        dev.setdefault(runner, pkg.NPM_DEV[runner])
    manifest = {
        "name": f"{slug}-backend",
        "version": "0.1.0",
        "private": True,
        "type": module_type,
        "scripts": scripts,
        "dependencies": deps,
    }
    if dev:
        manifest["devDependencies"] = dict(sorted(dev.items()))
    sc.add(be("package.json"), _json(manifest), "Dependencies derived from what the backend imports.")
    sc.add(be("scripts/check.cjs"), _NODE_CHECK, "`npm run build` for a server: every file must parse.")
    if with_migrations and driver:
        sc.add(be("scripts/migrate.cjs"), _NODE_MIGRATE, "Applies migrations/*.sql once each, in order.")
    sc.add(be(".env.example"), _backend_env(files, database, "3001"), "The variables the backend reads.")
    if entry:
        sc.commands += ["cp backend/.env.example backend/.env"]
        if with_migrations and driver:
            sc.commands += ["npm run migrate --workspace backend"]
        sc.commands += ["npm run start --workspace backend"]


def _scaffold_python_backend(sc: Scaffold, files: dict[str, str], database: Optional[str], with_migrations: bool) -> None:
    be = lambda rel: f"{BACKEND}/{rel}"  # noqa: E731
    sc.add(
        be("requirements.txt"),
        _python_requirements(files, sc.backend_framework, database, with_migrations),
        "Dependencies derived from what the backend imports, pinned to compatible ranges.",
    )
    sc.add(be(".env.example"), _backend_env(files, database, "8000"), "The variables the backend reads.")
    if with_migrations:
        sc.add(be("migrate.py"), _PY_MIGRATE, "Applies migrations/*.sql once each, in order.")
    sc.commands += [
        "cd backend && python -m venv .venv && . .venv/bin/activate",
        "pip install -r requirements.txt",
        "cp .env.example .env",
    ]
    if with_migrations:
        sc.commands.append("python migrate.py")
    entry = _python_entry(files, sc.backend_framework)
    if entry:
        sc.commands.append(entry)


# ── migrations from the data model ───────────────────────────────────────────
def _snake(text: str) -> str:
    s = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", str(text or ""))
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def _plural(word: str) -> str:
    if word.endswith("s"):
        return word
    if word.endswith("y") and len(word) > 1 and word[-2] not in "aeiou":
        return word[:-1] + "ies"
    return word + "s"


def _sql_type(declared: str, name: str, dialect: str) -> str:
    d = (declared or "").lower()
    table = {
        "int": ("INTEGER", "INT", "INTEGER"),
        "float": ("NUMERIC(12,2)", "DECIMAL(12,2)", "REAL"),
        "bool": ("BOOLEAN", "BOOLEAN", "INTEGER"),
        "date": ("DATE", "DATE", "TEXT"),
        "timestamp": ("TIMESTAMPTZ", "DATETIME", "TEXT"),
        "json": ("JSONB", "JSON", "TEXT"),
        "uuid": ("UUID", "CHAR(36)", "TEXT"),
        "text": ("TEXT", "VARCHAR(255)", "TEXT"),
    }
    index = {"postgres": 0, "mysql": 1}.get(dialect, 2)
    if re.search(r"\b(int|integer|bigint|smallint|serial)\b", d):
        kind = "int"
    elif re.search(r"(float|decimal|numeric|double|money|real|number)", d):
        kind = "float"
    elif re.search(r"bool", d):
        kind = "bool"
    elif re.search(r"(datetime|timestamp|time)", d) or re.search(r"(_at|_time)$", name):
        kind = "timestamp"
    elif re.search(r"\bdate\b", d) or name.endswith("_date"):
        kind = "date"
    elif re.search(r"(json|object|dict|array|list|map)", d):
        kind = "json"
    elif "uuid" in d:
        kind = "uuid"
    else:
        kind = "text"
    return table[kind][index]


def initial_migration(entities: list[dict], dialect: str) -> Optional[str]:
    """`CREATE TABLE` for every entity System Design drew, in dependency order."""
    q = (lambda n: f"`{n}`") if dialect == "mysql" else (lambda n: f'"{n}"')
    pk = {
        "postgres": "SERIAL PRIMARY KEY",
        "mysql": "INT AUTO_INCREMENT PRIMARY KEY",
    }.get(dialect, "INTEGER PRIMARY KEY AUTOINCREMENT")
    tables: dict[str, list[tuple[str, str]]] = {}
    for entity in entities:
        name = _plural(_snake(entity.get("entity") or entity.get("name") or ""))
        if not name or name in tables:
            continue
        columns: list[tuple[str, str]] = []
        for raw in entity.get("fields") or []:
            text = str(raw)
            col, _, declared = text.partition(":")
            col = _snake(col)
            if not col or col == "id" or any(c == col for c, _ in columns):
                continue
            columns.append((col, declared.strip()))
        tables[name] = columns
    if not tables:
        return None

    singular = {t[:-3] + "y" if t.endswith("ies") else t.rstrip("s"): t for t in tables}

    def refs(table: str) -> list[tuple[str, str]]:
        out = []
        for col, _decl in tables[table]:
            if col.endswith("_id") and col[:-3] in singular and singular[col[:-3]] != table:
                out.append((col, singular[col[:-3]]))
        return out

    ordered: list[str] = []
    visiting: set[str] = set()

    def visit(t: str) -> None:
        if t in ordered or t in visiting:
            return
        visiting.add(t)
        for _col, target in refs(t):
            visit(target)
        visiting.discard(t)
        ordered.append(t)

    for t in tables:
        visit(t)

    statements = ["-- Generated by the platform from the System Design data model."]
    for t in ordered:
        fks = dict(refs(t))
        lines = [f"  {q('id')} {pk}"]
        for col, declared in tables[t]:
            kind = "INTEGER" if col in fks and dialect != "mysql" else ("INT" if col in fks else _sql_type(declared, col, dialect))
            extra = ""
            if col == "created_at":
                extra = " DEFAULT CURRENT_TIMESTAMP"
            if col in fks:
                extra += f" REFERENCES {q(fks[col])}({q('id')}) ON DELETE CASCADE"
            lines.append(f"  {q(col)} {kind}{extra}")
        statements.append(f"CREATE TABLE IF NOT EXISTS {q(t)} (\n" + ",\n".join(lines) + "\n);")
    return "\n\n".join(statements) + "\n"


def _database_in_code(files: dict[str, str]) -> Optional[str]:
    """The database the backend's code actually talks to, when no charter says.

    A build from before charters existed still has a database — it is in the imports.
    Guessing SQLite for a pymongo backend would hand it SQL migrations for tables it
    never creates.
    """
    from app.orchestration import stack

    fallback: Optional[str] = None
    for rel, content in files.items():
        found = stack.detect("database", rel, content)
        if found is None:
            continue
        if not found.dev_fallback:
            return found.token
        fallback = fallback or found.token
    return fallback


# ── the build ────────────────────────────────────────────────────────────────
def build(
    tree: dict[str, str],
    charter=None,
    design_output: Optional[dict] = None,
    product: str = "app",
) -> Scaffold:
    """Everything the platform writes for this build, from what the agents wrote."""
    sc = Scaffold()
    slug = _slug(product)
    frontend_files = _side_files(tree, FRONTEND)
    backend_files = _side_files(tree, BACKEND)
    charter_front = charter.get("frontend_framework").token if charter is not None and charter.get("frontend_framework") else None
    database = charter.get("database").token if charter is not None and charter.get("database") else None
    if database is None:
        database = _database_in_code(backend_files)
    sc.database = database

    sc.frontend = detect_frontend(frontend_files, charter_front)
    language, framework = detect_backend(backend_files, charter)
    sc.backend, sc.backend_framework = language, framework

    entities = []
    if isinstance(design_output, dict):
        entities = [e for e in design_output.get("data_model") or [] if isinstance(e, dict)]
    agent_migrations = any(
        re.match(r"(migrations|alembic|prisma|db/migrate)/", p) or p.endswith(("alembic.ini", "schema.prisma"))
        for p in backend_files
    )
    sql_db = database in ("postgres", "mysql", "sqlite") or (database is None and bool(entities))
    with_migrations = bool(backend_files) and sql_db and not agent_migrations
    dialect = database if database in ("postgres", "mysql") else "sqlite"

    if sc.frontend == "nextjs":
        _scaffold_next(sc, frontend_files, slug, product)
    elif sc.frontend in ("react", "vue", "svelte"):
        _scaffold_vite(sc, frontend_files, slug, product, sc.frontend)
    elif sc.frontend:
        deps, dev = _deps_for(frontend_files, {})
        sc.add(
            f"{FRONTEND}/package.json",
            _json({"name": f"{slug}-frontend", "version": "0.1.0", "private": True,
                   "dependencies": deps, "devDependencies": dev}),
            "Dependencies derived from what the frontend imports.",
        )
        sc.notes.append(
            f"The platform does not scaffold a {sc.frontend} build yet: the frontend has its "
            "dependencies but no build script, so `npm run build` skips it."
        )

    if language == "python":
        _scaffold_python_backend(sc, backend_files, database, with_migrations)
    elif language in ("javascript", "typescript"):
        if language == "typescript":
            sc.notes.append("The backend is TypeScript; its build script checks only the JavaScript files.")
        _scaffold_node_backend(sc, backend_files, slug, database, with_migrations)

    if with_migrations and entities:
        sql = initial_migration(entities, dialect)
        if sql:
            sc.add(f"{BACKEND}/migrations/0001_initial.sql", sql, "The tables System Design drew, as SQL.")
    elif agent_migrations:
        sc.notes.append("The backend brings its own migrations, so the platform added no runner.")
    if database == "mongodb":
        sc.notes.append("MongoDB is schemaless: there are no SQL migrations to run.")

    workspaces = [
        side
        for side, present in ((FRONTEND, bool(frontend_files)), (BACKEND, language in ("javascript", "typescript")))
        if present
    ]
    if workspaces:
        scripts = {"build": "npm run build --workspaces --if-present"}
        if FRONTEND in workspaces:
            scripts["dev"] = "npm run dev --workspace frontend"
        sc.add(
            "package.json",
            _json({"name": slug, "private": True, "workspaces": workspaces, "scripts": scripts}),
            "The workspace root: `npm install && npm run build` builds every JavaScript side.",
        )
    sc.add(".gitignore", _GITIGNORE, "Keeps dependencies, builds and secrets out of version control.")
    # Commands are listed in the order a person runs them: JS first (one install at
    # the root covers every workspace), then the Python backend.
    js_first = [c for c in sc.commands if c.startswith("npm") or c.startswith("cp backend")]
    rest = [c for c in sc.commands if c not in js_first]
    sc.commands = list(dict.fromkeys(js_first + rest))
    return sc


_GITIGNORE = """node_modules/
.next/
dist/
build/
.env
.venv/
__pycache__/
*.pyc
*.db
.DS_Store
"""

_PY_MIGRATE = '''"""Apply migrations/*.sql in order, each exactly once. Written by the platform.

    python migrate.py            # uses DATABASE_URL, or ./app.db (SQLite) if unset
"""
import os
import pathlib
import sys
from urllib.parse import unquote, urlparse

HERE = pathlib.Path(__file__).resolve().parent
URL = os.environ.get("DATABASE_URL", "sqlite:///./app.db")


def connect(url):
    if url.startswith("sqlite"):
        import sqlite3

        path = url.split(":///", 1)[1] if ":///" in url else "app.db"
        return sqlite3.connect(path), "?"
    if url.startswith(("postgres://", "postgresql")):
        # `postgresql+psycopg2://` is SQLAlchemy's spelling; the driver wants the plain one.
        scheme, rest = url.split("://", 1)
        url = "postgresql://" + rest
        try:
            import psycopg2 as driver
        except ImportError:
            import psycopg as driver
        return driver.connect(url), "%s"
    if url.startswith("mysql"):
        import pymysql

        u = urlparse(url)
        return pymysql.connect(
            host=u.hostname or "localhost", port=u.port or 3306, user=unquote(u.username or "root"),
            password=unquote(u.password or ""), database=(u.path or "/").lstrip("/"),
        ), "%s"
    sys.exit(f"Unsupported DATABASE_URL: {url}")


def statements(sql):
    """Split a file into statements, dropping whole-line comments first."""
    kept = [line for line in sql.splitlines() if not line.strip().startswith("--")]
    return [s.strip() for s in "\\n".join(kept).split(";") if s.strip()]


def main():
    conn, mark = connect(URL)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name VARCHAR(255) PRIMARY KEY)")
    conn.commit()
    cur.execute("SELECT name FROM schema_migrations")
    done = {row[0] for row in cur.fetchall()}
    applied = 0
    for path in sorted((HERE / "migrations").glob("*.sql")):
        if path.name in done:
            continue
        for statement in statements(path.read_text()):
            cur.execute(statement)
        cur.execute(f"INSERT INTO schema_migrations (name) VALUES ({mark})", (path.name,))
        conn.commit()
        applied += 1
        print(f"applied {path.name}")
    print(f"{applied} migration(s) applied; {len(done)} already in place.")
    conn.close()


if __name__ == "__main__":
    main()
'''

_NODE_MIGRATE = """// Apply migrations/*.sql in order, each exactly once. Written by the platform.
//   npm run migrate      (uses DATABASE_URL)
const fs = require('fs');
const path = require('path');

const url = process.env.DATABASE_URL || 'sqlite:///./app.db';
const dir = path.join(__dirname, '..', 'migrations');

async function open() {
  if (url.startsWith('postgres')) {
    const { Client } = require('pg');
    const client = new Client({ connectionString: url });
    await client.connect();
    return { run: (sql, params) => client.query(sql.replace(/\\?/g, () => '$1'), params), rows: async (sql) => (await client.query(sql)).rows, close: () => client.end() };
  }
  if (url.startsWith('mysql')) {
    const mysql = require('mysql2/promise');
    const conn = await mysql.createConnection(url);
    return { run: (sql, params) => conn.query(sql, params), rows: async (sql) => (await conn.query(sql))[0], close: () => conn.end() };
  }
  const Database = require('better-sqlite3');
  const db = new Database(url.replace(/^sqlite:\\/\\/\\/?/, '') || 'app.db');
  return { run: async (sql, params) => db.prepare(sql).run(...(params || [])), rows: async (sql) => db.prepare(sql).all(), close: async () => db.close() };
}

(async () => {
  const db = await open();
  await db.run('CREATE TABLE IF NOT EXISTS schema_migrations (name VARCHAR(255) PRIMARY KEY)');
  const done = new Set((await db.rows('SELECT name FROM schema_migrations')).map((r) => r.name));
  const files = fs.existsSync(dir) ? fs.readdirSync(dir).filter((f) => f.endsWith('.sql')).sort() : [];
  let applied = 0;
  for (const file of files) {
    if (done.has(file)) continue;
    const sql = fs.readFileSync(path.join(dir, file), 'utf8');
    for (const statement of sql.split(/;\\s*\\n/).map((s) => s.trim()).filter((s) => s && !/^--[^\\n]*$/.test(s))) {
      await db.run(statement);
    }
    await db.run('INSERT INTO schema_migrations (name) VALUES (?)', [file]);
    applied += 1;
    console.log(`applied ${file}`);
  }
  console.log(`${applied} migration(s) applied; ${done.size} already in place.`);
  await db.close();
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""

_NODE_CHECK = """// `npm run build` for the backend: every JavaScript file must parse. Written by the platform.
const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..');
const failures = [];

function walk(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === 'node_modules' || entry.name.startsWith('.')) continue;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) walk(full);
    else if (/\\.(c|m)?js$/.test(entry.name)) {
      const result = spawnSync(process.execPath, ['--check', full], { encoding: 'utf8' });
      if (result.status !== 0) failures.push(`${path.relative(root, full)}\\n${result.stderr}`);
    }
  }
}

walk(root);
if (failures.length) {
  console.error(failures.join('\\n'));
  process.exit(1);
}
console.log('backend: every file parses');
"""
