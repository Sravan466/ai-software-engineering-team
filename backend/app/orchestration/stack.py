"""The vocabulary of technology choices, and how to spot one in generated code.

Eight agents wrote a build whose architecture document specified PostgreSQL, whose
backend was Mongoose, and whose tests were pytest files aimed at `.js` controllers.
Every phase was individually plausible; nothing compared them, and `artifacts.assemble`
zipped all three together without a word.

Comparing them needs two things this module provides and nothing else does:

  **A name for each choice.** A design says "Postgres", "PostgreSQL", "postgresql" or
  "Amazon RDS (Postgres)"; those are one decision, so they collapse to one token.

  **Evidence in code.** Not the prose an agent wrote about itself — the `import`, the
  connection string, the lockfile. An agent that *says* Postgres and writes `mongoose`
  is exactly the failure being caught, so its own summary cannot be the witness.

Detection is deliberately conservative. A false positive fails a phase that was fine
and sends a correct agent back to redo correct work, which is worse than missing a
contradiction — so every signal here is an import, a driver name, a URL scheme or a
file extension, never a word that could appear in a comment.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional


@dataclass(frozen=True)
class Choice:
    """One technology, the names a model calls it, and how it shows up in code."""

    token: str
    #: What to print when this application has to name the choice itself.
    label: str
    #: Substrings that identify this choice in a design's prose. Matched against a
    #: normalised (lowercase, punctuation-stripped) form, longest alias first, so
    #: "postgresql" cannot be claimed by a shorter alias of something else.
    aliases: tuple[str, ...] = ()
    #: Patterns that are evidence of this choice in a generated file — an import, a
    #: driver, a URL scheme, a lockfile name. Applied to path and content alike.
    signals: tuple[str, ...] = ()
    #: Tokens this one legitimately contains. A charter naming Next.js is not
    #: contradicted by React, because Next.js *is* React — but the reverse is a real
    #: substitution: a charter that says React and a build that says Next.js changes
    #: the build command, the routing and the deployment target under everyone else.
    subsumes: tuple[str, ...] = ()
    #: Choices in other categories that follow from this one when nothing says
    #: otherwise — FastAPI means Python, Express means a JavaScript test runner.
    implies: tuple[tuple[str, str], ...] = ()
    _matchers: tuple[re.Pattern, ...] = field(default=(), init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_matchers",
            tuple(re.compile(s, re.IGNORECASE | re.MULTILINE) for s in self.signals),
        )

    def seen_in(self, text: str) -> bool:
        return any(m.search(text) for m in self._matchers)


# ── the categories a charter freezes ─────────────────────────────────────────
#: Ordered, because this is also the order the charter is printed in.
CATEGORIES: tuple[tuple[str, str], ...] = (
    ("language", "Language"),
    ("backend_framework", "Backend framework"),
    ("frontend_framework", "Frontend framework"),
    ("database", "Database"),
    ("test_runner", "Test runner"),
    ("package_manager", "Package manager"),
)

CATEGORY_LABELS: dict[str, str] = dict(CATEGORIES)


CHOICES: dict[str, tuple[Choice, ...]] = {
    # ── languages ────────────────────────────────────────────────────────────
    "language": (
        Choice(
            token="python",
            label="Python",
            aliases=("python3", "python"),
            signals=(r"\.pyi?$", r"^\s*from\s+\w[\w.]*\s+import\s", r"^\s*def\s+\w+\(.*\)\s*:"),
            implies=(("test_runner", "pytest"), ("package_manager", "pip")),
        ),
        Choice(
            token="typescript",
            label="TypeScript",
            aliases=("typescript", "ts"),
            signals=(r"\.tsx?$", r"^\s*interface\s+\w+\s*\{", r":\s*Promise<"),
            # TypeScript compiles to JavaScript and the two coexist in one repo
            # constantly, so neither direction of that pair is a contradiction.
            subsumes=("javascript",),
            implies=(("test_runner", "jest"), ("package_manager", "npm")),
        ),
        Choice(
            token="javascript",
            label="JavaScript",
            aliases=("javascript", "node js", "nodejs", "node", "js"),
            signals=(r"\.[cm]?jsx?$", r"\bmodule\.exports\b", r"\brequire\(['\"]"),
            subsumes=("typescript",),
            implies=(("test_runner", "jest"), ("package_manager", "npm")),
        ),
        Choice(
            token="go",
            label="Go",
            aliases=("golang", "go"),
            signals=(r"\.go$", r"^\s*package\s+main\b", r"^\s*func\s+\w+\("),
            implies=(("test_runner", "gotest"), ("package_manager", "gomod")),
        ),
        Choice(
            token="java",
            label="Java",
            aliases=("java",),
            signals=(r"\.java$", r"^\s*package\s+[a-z]+(\.[a-z]+)+\s*;"),
            implies=(("test_runner", "junit"), ("package_manager", "maven")),
        ),
        Choice(
            token="ruby",
            label="Ruby",
            aliases=("ruby",),
            signals=(r"\.rb$", r"^\s*require\s+['\"]"),
            implies=(("test_runner", "rspec"), ("package_manager", "bundler")),
        ),
        Choice(
            token="csharp",
            label="C#",
            aliases=("c sharp", "csharp", "dotnet", "net core", "net"),
            signals=(r"\.cs$", r"^\s*using\s+System\b", r"\.csproj$"),
        ),
        Choice(
            token="php",
            label="PHP",
            aliases=("php",),
            signals=(r"\.php$", r"<\?php"),
            implies=(("package_manager", "composer"),),
        ),
        Choice(
            token="rust",
            label="Rust",
            aliases=("rust",),
            signals=(r"\.rs$", r"^\s*fn\s+main\s*\(", r"\bCargo\.toml$"),
            implies=(("package_manager", "cargo"),),
        ),
    ),
    # ── backend frameworks ───────────────────────────────────────────────────
    "backend_framework": (
        Choice(
            token="fastapi",
            label="FastAPI",
            aliases=("fastapi",),
            signals=(r"\bfrom\s+fastapi\b", r"\bFastAPI\s*\(", r"\bAPIRouter\s*\("),
            implies=(("language", "python"),),
        ),
        Choice(
            token="django",
            label="Django",
            aliases=("django rest framework", "django"),
            signals=(r"\bfrom\s+django\b", r"\bdjango\.(db|urls|conf)\b", r"\bmanage\.py$"),
            implies=(("language", "python"),),
        ),
        Choice(
            token="flask",
            label="Flask",
            aliases=("flask",),
            signals=(r"\bfrom\s+flask\b", r"\bFlask\s*\(\s*__name__"),
            implies=(("language", "python"),),
        ),
        Choice(
            token="express",
            label="Express",
            aliases=("express js", "expressjs", "express"),
            signals=(
                r"require\(['\"]express['\"]\)",
                r"\bfrom\s+['\"]express['\"]",
                r"\bexpress\s*\(\s*\)",
                r"\bexpress\.Router\s*\(",
            ),
            implies=(("language", "javascript"),),
        ),
        Choice(
            token="nestjs",
            label="NestJS",
            aliases=("nest js", "nestjs", "nest"),
            signals=(r"@nestjs/", r"\bNestFactory\b"),
            subsumes=("express",),
            implies=(("language", "typescript"),),
        ),
        Choice(
            token="spring",
            label="Spring Boot",
            aliases=("spring boot", "spring"),
            signals=(r"\borg\.springframework\b", r"@SpringBootApplication\b"),
            implies=(("language", "java"),),
        ),
        Choice(
            token="rails",
            label="Ruby on Rails",
            aliases=("ruby on rails", "rails"),
            signals=(r"\bActiveRecord\b", r"\bRails\.application\b"),
            implies=(("language", "ruby"),),
        ),
        Choice(
            token="laravel",
            label="Laravel",
            aliases=("laravel",),
            signals=(r"\bIlluminate\\\\", r"\bartisan$"),
            implies=(("language", "php"),),
        ),
        Choice(
            token="aspnet",
            label="ASP.NET",
            aliases=("asp net core", "asp net", "aspnet"),
            signals=(r"\bMicrosoft\.AspNetCore\b", r"\bWebApplication\.CreateBuilder\b"),
            implies=(("language", "csharp"),),
        ),
    ),
    # ── frontend frameworks ──────────────────────────────────────────────────
    "frontend_framework": (
        Choice(
            token="nextjs",
            label="Next.js",
            aliases=("next js", "nextjs", "next"),
            signals=(
                r"\bfrom\s+['\"]next(/[\w-]+)?['\"]",
                r"\bnext/(link|image|router|navigation|head|font|server)\b",
                r"\bnext\.config\.[mc]?[jt]s$",
                r"\bgetServerSideProps\b|\bgetStaticProps\b",
                r"^\s*['\"]use client['\"]",
            ),
            # Next.js is React. A charter that names Next.js is not contradicted by
            # React idioms; a charter that names React *is* contradicted by Next.js,
            # which is why this relationship only points one way.
            subsumes=("react",),
            implies=(("language", "typescript"),),
        ),
        Choice(
            token="react",
            label="React",
            aliases=("react js", "reactjs", "react native", "react"),
            signals=(
                r"\bfrom\s+['\"]react(-dom)?(/[\w.]+)?['\"]",
                r"require\(['\"]react['\"]\)",
                r"\bReactDOM\.createRoot\b",
            ),
        ),
        Choice(
            token="nuxt",
            label="Nuxt",
            aliases=("nuxt js", "nuxtjs", "nuxt"),
            signals=(r"\bfrom\s+['\"]nuxt", r"\bnuxt\.config\."),
            subsumes=("vue",),
        ),
        Choice(
            token="vue",
            label="Vue",
            aliases=("vue js", "vuejs", "vue"),
            signals=(r"\.vue$", r"\bfrom\s+['\"]vue['\"]", r"\bdefineComponent\s*\("),
        ),
        Choice(
            token="angular",
            label="Angular",
            aliases=("angular",),
            signals=(r"@angular/", r"\bNgModule\b"),
        ),
        Choice(
            token="svelte",
            label="Svelte",
            aliases=("sveltekit", "svelte"),
            signals=(r"\.svelte$", r"\bfrom\s+['\"]svelte", r"\bsvelte\.config\."),
        ),
    ),
    # ── databases ────────────────────────────────────────────────────────────
    "database": (
        Choice(
            token="postgres",
            label="PostgreSQL",
            aliases=("postgresql", "postgres", "pgsql", "rds postgres", "supabase", "neon"),
            signals=(
                r"\bpsycopg2?\b",
                r"\basyncpg\b",
                r"postgres(?:ql)?://",
                r"require\(['\"]pg['\"]\)",
                r"\bfrom\s+['\"]pg['\"]",
                r"\bnew\s+Pool\s*\(",
                r"\bSERIAL\s+PRIMARY\s+KEY\b",
                r"\bpostgresql\+\w+://",
                r"image:\s*[\w./-]*postgres",
            ),
        ),
        Choice(
            token="mongodb",
            label="MongoDB",
            aliases=("mongodb atlas", "mongodb", "mongo", "mongoose"),
            signals=(
                r"\bmongoose\b",
                r"mongodb(\+srv)?://",
                r"\bpymongo\b",
                r"\bMongoClient\b",
                r"\bfrom\s+['\"]mongodb['\"]",
                r"require\(['\"]mongo(db|ose)['\"]\)",
                r"\bnew\s+Schema\s*\(",
                r"image:\s*[\w./-]*mongo",
            ),
        ),
        Choice(
            token="mysql",
            label="MySQL",
            aliases=("mysql", "mariadb", "planetscale"),
            signals=(
                r"\bmysql2?\b",
                r"mysql://",
                r"\bpymysql\b",
                r"\bmariadb\b",
                r"\bAUTO_INCREMENT\b",
                r"image:\s*[\w./-]*(mysql|mariadb)",
            ),
        ),
        Choice(
            token="sqlite",
            label="SQLite",
            aliases=("sqlite3", "sqlite"),
            signals=(r"\bsqlite3?\b", r"sqlite://", r"\bbetter-sqlite3\b"),
        ),
        Choice(
            token="dynamodb",
            label="DynamoDB",
            aliases=("dynamodb", "dynamo"),
            signals=(r"\bDynamoDB(Client)?\b", r"@aws-sdk/client-dynamodb", r"\bboto3\b.*dynamodb"),
        ),
        Choice(
            token="firestore",
            label="Firestore",
            aliases=("cloud firestore", "firestore", "firebase"),
            signals=(r"\bfirebase-admin\b", r"\bgetFirestore\s*\(", r"firebase/firestore"),
        ),
    ),
    # ── test runners ─────────────────────────────────────────────────────────
    "test_runner": (
        Choice(
            token="pytest",
            label="pytest",
            aliases=("pytest", "py test"),
            signals=(r"\bimport\s+pytest\b", r"\bfrom\s+pytest\b", r"^\s*def\s+test_\w+", r"\.py$"),
            implies=(("language", "python"),),
        ),
        Choice(
            token="jest",
            label="Jest",
            aliases=("jest",),
            signals=(
                r"\bjest\.(fn|mock|spyOn|config)\b",
                r"@jest/globals",
                r"\bjest\.config\.",
                r"\bjest\b",
            ),
            subsumes=("vitest", "mocha"),
        ),
        Choice(
            token="vitest",
            label="Vitest",
            aliases=("vitest",),
            signals=(r"\bfrom\s+['\"]vitest['\"]", r"\bvitest\.config\.", r"\bvitest\b"),
            subsumes=("jest", "mocha"),
        ),
        Choice(
            token="mocha",
            label="Mocha",
            aliases=("mocha chai", "mocha"),
            signals=(r"require\(['\"](mocha|chai)['\"]\)", r"\bfrom\s+['\"]chai['\"]"),
            subsumes=("jest",),
        ),
        Choice(
            token="junit",
            label="JUnit",
            aliases=("junit",),
            signals=(r"\borg\.junit\b", r"@Test\b"),
            implies=(("language", "java"),),
        ),
        Choice(
            token="rspec",
            label="RSpec",
            aliases=("rspec",),
            signals=(r"\brspec\b", r"\.spec\.rb$"),
            implies=(("language", "ruby"),),
        ),
        Choice(
            token="gotest",
            label="go test",
            aliases=("go test", "gotest", "testing"),
            signals=(r"^\s*func\s+Test[A-Z]\w*\s*\(", r"_test\.go$"),
            implies=(("language", "go"),),
        ),
    ),
    # ── package managers ─────────────────────────────────────────────────────
    "package_manager": (
        Choice(
            token="pnpm",
            label="pnpm",
            aliases=("pnpm",),
            signals=(r"\bpnpm-lock\.yaml$", r"\bpnpm\s+(install|add|run)\b"),
        ),
        Choice(
            token="yarn",
            label="Yarn",
            aliases=("yarn",),
            signals=(r"\byarn\.lock$", r"\byarn\s+(install|add)\b"),
        ),
        Choice(
            token="npm",
            label="npm",
            aliases=("npm",),
            signals=(r"\bpackage-lock\.json$", r"\bnpm\s+(install|ci|run)\b"),
        ),
        Choice(
            token="poetry",
            label="Poetry",
            aliases=("poetry",),
            signals=(r"\[tool\.poetry\]", r"\bpoetry\s+(install|add)\b", r"\bpoetry\.lock$"),
        ),
        Choice(
            token="pip",
            label="pip",
            aliases=("pip", "pipenv"),
            signals=(r"\brequirements(-\w+)?\.txt$", r"\bpip\s+install\b"),
        ),
        Choice(
            token="bundler",
            label="Bundler",
            aliases=("bundler", "gem"),
            signals=(r"\bGemfile(\.lock)?$", r"\bbundle\s+install\b"),
        ),
        Choice(
            token="maven",
            label="Maven",
            aliases=("maven",),
            signals=(r"\bpom\.xml$", r"\bmvn\s+\w+"),
        ),
        Choice(
            token="gradle",
            label="Gradle",
            aliases=("gradle",),
            signals=(r"\bbuild\.gradle(\.kts)?$", r"\bgradlew?\s+\w+"),
        ),
        Choice(
            token="gomod",
            label="Go modules",
            aliases=("go modules", "go mod"),
            signals=(r"\bgo\.(mod|sum)$", r"\bgo\s+(get|mod)\b"),
        ),
        Choice(
            token="cargo",
            label="Cargo",
            aliases=("cargo",),
            signals=(r"\bCargo\.(toml|lock)$", r"\bcargo\s+(build|add)\b"),
        ),
        Choice(
            token="composer",
            label="Composer",
            aliases=("composer",),
            signals=(r"\bcomposer\.(json|lock)$", r"\bcomposer\s+(install|require)\b"),
        ),
    ),
}

BY_TOKEN: dict[str, Choice] = {c.token: c for choices in CHOICES.values() for c in choices}

#: Which category each token belongs to, so a token can answer for itself.
CATEGORY_OF: dict[str, str] = {
    c.token: category for category, choices in CHOICES.items() for c in choices
}


def _normalise(text: str) -> str:
    """"Node.js + Express" -> "node js express" — punctuation gone, spacing regular."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(text).lower())).strip()


def _alias_index(category: str) -> list[tuple[str, Choice]]:
    """(alias, choice) longest-first, so "react native" cannot be read as "react"."""
    pairs = [(alias, choice) for choice in CHOICES[category] for alias in choice.aliases]
    return sorted(pairs, key=lambda p: len(p[0]), reverse=True)


def name_to_choice(category: str, text: object) -> Optional[Choice]:
    """The choice a design's prose names, or None when it names nothing known.

    Matched on whole words against a normalised form, so "Postgres" in
    "Amazon RDS (PostgreSQL 15)" is found and "pgcrypto" is not mistaken for it.
    """
    if category not in CHOICES:
        return None
    haystack = f" {_normalise(text)} "
    for alias, choice in _alias_index(category):
        if f" {alias} " in haystack:
            return choice
    return None


def names_to_choice(category: str, values: Iterable[object]) -> Optional[Choice]:
    """The first recognisable choice across several strings (a `tech_stack` list)."""
    for value in values or ():
        found = name_to_choice(category, value)
        if found is not None:
            return found
    return None


def detect(category: str, path: str, content: str) -> Optional[Choice]:
    """Which choice in `category` this one file is evidence of, if any.

    The path is searched as well as the content because half the strongest signals
    *are* paths — `pnpm-lock.yaml`, `_test.go`, `next.config.ts`. The most specific
    choice wins: a file matching both Next.js and React is Next.js, because Next.js
    subsumes React and not the other way round.
    """
    text = f"{path}\n{content}"
    hits = [choice for choice in CHOICES.get(category, ()) if choice.seen_in(text)]
    if not hits:
        return None
    # Prefer a choice that subsumes another hit — that is the specific one.
    tokens = {c.token for c in hits}
    for choice in hits:
        if tokens & set(choice.subsumes):
            return choice
    return hits[0]


def satisfies(charter_token: str, found_token: str) -> bool:
    """Whether code that looks like `found_token` is allowed under `charter_token`."""
    if charter_token == found_token:
        return True
    charter = BY_TOKEN.get(charter_token)
    return bool(charter and found_token in charter.subsumes)


def implications(choice: Choice) -> dict[str, str]:
    """The categories this choice settles on its own: FastAPI means Python, and pip."""
    return {category: token for category, token in choice.implies}


def label_for(token: str) -> str:
    choice = BY_TOKEN.get(token)
    return choice.label if choice else token
