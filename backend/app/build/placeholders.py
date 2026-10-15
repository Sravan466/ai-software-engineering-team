"""Code that parses and is still not finished (#101).

The compile gate catches code that is broken: a file that does not parse, an import
of nothing, a package that does not exist. What it let through is code that is valid
and fake — and a small model writes a lot of it:

  **placeholder** — a function whose body is only `pass` or `{}`, a comment standing in
                    for code (`// ... rest of the file`), a `TODO` left in, lorem ipsum,
                    `"your-api-key"` where a value should be read, an empty file, or a
                    refusal ("I'm sorry, I can't…") written where the file should be
  **weak_test**   — a test that asserts nothing, or only something always true
                    (`assert True`), a skipped one, or one that mocks the very module it
                    is testing, so it passes whatever the code does

Each is a `Problem` like a missing import: named with its file and line, sent to the
phase that wrote the file, fixed by the same loop. Everything here is read from the
text — Python with `ast`, JavaScript and TypeScript with a small lexer that knows
strings and comments — so it runs in-process on each file as it lands, and a stub is
repaired before the next batch is written against it.

Built to stay quiet on correct code: what is flagged is a narrow, named shape, not a
judgement. An abstract method, a `Protocol`, an exception class, an `__init__.py`, a
no-op hook a subclass overrides in the same file are all left alone.

Edit mode has one more check (`lost_code`): a rewrite of an existing file that drops
much of it, which is how a model "changes one line" by returning ten (Aider's lost-code
check).
"""
from __future__ import annotations

import ast
import io
import posixpath
import re
import tokenize
from typing import Iterable, Optional

from app.build.check import Problem
from app.build.exports import JS_EXT

PLACEHOLDER = "placeholder"
WEAK_TEST = "weak_test"
KINDS = (PLACEHOLDER, WEAK_TEST)

#: At most this many placeholder problems a file: the fourth TODO in one file adds
#: nothing to a repair prompt the first three didn't.
_PER_FILE = 3

_CSS_EXT = (".css", ".scss")
#: Files that are meant to be empty.
_EMPTY_OK = frozenset({"__init__.py", "conftest.py", ".gitkeep", ".keep", "py.typed", ".nojekyll"})
#: Prose, data and configuration: no function bodies, no comments that stand in for
#: code — and "Here is" is a fine way to start a README.
_SKIP_EXT = (".md", ".mdx", ".txt", ".json", ".lock", ".svg", ".env", ".example", ".ini", ".cfg", ".toml")


# ── what a placeholder says ──────────────────────────────────────────────────
#: Aider's lazy-comment test, widened to `//` and `/* */`: a comment whose text starts
#: with an ellipsis is code left out (`# ... existing code ...`, `{/* ... */}`).
_ELLIPSIS = re.compile(r"^\s*(?:\.\.\.|…)(?:\s|$)")
#: Each says code was left out, about the code itself — "the total is unchanged" is a
#: comment about a value, and stays.
_LAZY = re.compile(
    r"\brest of (?:the |this |your )?(?:code|file|function|component|implementation|logic|methods|routes)\b"
    r"|\b(?:the rest|everything else|rest of (?:it|this)|(?:the )?(?:code|implementation|logic|function|component"
    r"|file|methods?|imports?)(?: below| above| here)?)\s+(?:remains?|stays?|is|are) (?:the same|unchanged)\b"
    r"|^\W*(?:keep(?:ing)? |\(?)?(?:the )?existing code\b\W*(?:here|goes here|unchanged|remains|stays|\.\.\.|…)?\W*$"
    r"|\bomitted for brevity\b"
    r"|\bsame as (?:before|above)\b"
    r"|\b(?:other|more|remaining|additional)\s+(?:code|methods|routes|fields|functions|components|handlers|logic"
    r"|styles|endpoints|tests|cases|properties)\b.*(?:\.\.\.|…|\bhere|\bas needed)\s*$",
    re.IGNORECASE,
)
#: Uppercase only, and as a marker — `TODO:`, `TODO(name)`, `TODO -`, or alone — so a
#: comment about a to-do app's `TODO list` is not one.
_TODO = re.compile(r"\b(?:TODO|FIXME)\b(?:\s*(?:[:(\-–]|$))|\bFIXME\b")
_IMPLEMENT = re.compile(r"\bimplement (?:this|later|me)\b", re.IGNORECASE)
_LOREM = re.compile(r"\blorem ipsum\b", re.IGNORECASE)
#: A placeholder where a value goes: `"your-api-key"`, `"sk_test_xxx"`, `"xxxx"`.
_FAKE_VALUE = re.compile(
    r"""(?:[=:(,]|\breturn)\s*(["'`])((?:<\s*)?your[-_ ]?(?:api[-_ ]?)?(?:key|token|secret)\b[^"'`\n]*"""
    r"""|(?:[A-Za-z]{2,4}_(?:test|live)_)?[xX]{3,})\1""",
    re.IGNORECASE,
)
_PLACEHOLDER_ATTR = re.compile(r"""\bplaceholder\s*[=:]\s*$""", re.IGNORECASE)
#: The whole of a JSX text node when it is filler, not copy.
_JSX_FILLER = re.compile(
    r"^(?:placeholder(?: (?:text|content|page|component|section))?"
    r"|(?:(?:your|the|page|main|some) )?(?:content|text|component|page|section|form|chart|table|list|image) (?:goes )?here"
    r"|insert [^<>{}]{1,40} here"
    r"|not (?:yet )?implemented(?: yet)?)[.!…]*$",
    re.IGNORECASE,
)
#: Case matters here: `<h1>Todo</h1>` is a to-do app's heading, `TODO` is a marker.
_JSX_TODO = re.compile(r"^(?:TODO|FIXME)\b")
_JSX_TEXT = re.compile(r">\s*([^<>{}\n]{3,80}?)\s*</")
#: How a file that is not code starts: a refusal, or chat around the answer.
_CHATTER = re.compile(
    r"^(?:i can(?:'|’)?t|i cannot|i(?:'|’)m sorry|sorry,|i(?:'|’)m unable|i am unable|as an ai\b"
    r"|here is\b|here(?:'|’)s\b|here are\b|sure[,!]|certainly[,!]|below is\b)",
    re.IGNORECASE,
)


def _clip(text: str, n: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _base(path: str) -> str:
    return posixpath.basename(path)


def _is_test(path: str) -> bool:
    from app.build.scaffold import _is_test as scaffold_is_test

    return scaffold_is_test(path)


# ── the checks ───────────────────────────────────────────────────────────────
def placeholder_problems(
    files: dict[str, str], targets: Iterable[str], cap: Optional[int] = _PER_FILE
) -> list[Problem]:
    """Every placeholder and weak test in `targets` (placed paths), at most `cap` a file
    (None: all of them, for a count rather than a repair prompt)."""
    out: list[Problem] = []
    for path in dict.fromkeys(targets):
        content = files.get(path)
        if isinstance(content, str):
            out += file_placeholders(path, content, cap)
    return out


def new_in(problems: list[Problem], before: list[Problem]) -> list[Problem]:
    """Of a rewritten file's problems, the ones its previous version didn't have (#79
    edits): a change is answerable for what it wrote, not for an old TODO it kept.
    Matched by message, since the line a problem is on moves with every edit."""
    old = {p.message for p in before}
    return [p for p in problems if p.message not in old]


def file_placeholders(path: str, content: str, cap: Optional[int] = _PER_FILE) -> list[Problem]:
    """One file's placeholders and weak tests, worst first, at most `cap` of them."""
    name = _base(path)
    low = name.lower()
    if low.endswith(_SKIP_EXT) or low.startswith(".env") or low.endswith(".d.ts") or low.endswith(".pyi"):
        return []
    whole = _whole_file(path, content)
    if whole is not None:
        return [whole]
    found: list[Problem] = []
    if low.endswith(".py"):
        found += _python(path, content)
    elif low.endswith(JS_EXT):
        found += _javascript(path, content)
    elif low.endswith(_CSS_EXT):
        found += _comments(path, _block_comments(content))
    elif low.endswith(".html"):
        found += _comments(path, _html_comments(content))
    elif low.endswith(".sql"):
        found += _comments(path, _dash_comments(content))
    found += _values(path, content)
    out: list[Problem] = []
    seen: set[str] = set()
    # Weak tests first: a test file's TODO is less news than a test that tests nothing.
    for p in sorted(found, key=lambda p: (p.kind != WEAK_TEST, p.line or 0)):
        if p.message in seen:
            continue
        seen.add(p.message)
        out.append(p)
    return out if cap is None else out[:cap]


def is_whole_file(problem: Problem) -> bool:
    """An empty file, or a refusal where the file should be — named instead of the parse
    error such a file also has, because it says what is actually wrong."""
    return (
        problem.kind == PLACEHOLDER
        and problem.line is None
        and (problem.message == _EMPTY_FILE or problem.message.endswith(_NOT_CODE))
    )


_EMPTY_FILE = "is empty. Write the whole file."
_NOT_CODE = " instead of code. Return only the file."


def _whole_file(path: str, content: str) -> Optional[Problem]:
    name = _base(path)
    if not content.strip():
        # Only a module is wrong for being empty: a CSS module, a `.gitignore` or a
        # Dockerfile with nothing in it yet is a fine file.
        if name in _EMPTY_OK or not name.lower().endswith((".py", *JS_EXT)):
            return None
        return Problem(path, _EMPTY_FILE, PLACEHOLDER)
    first = next((line.strip() for line in content.splitlines() if line.strip()), "")
    # A fence above the code is chat too, but the parser already names that; a refusal
    # in a file's place reads as a model that simply stopped.
    if _CHATTER.match(first):
        return Problem(
            path,
            f"starts with “{_clip(first, 50).rstrip('.')}”{_NOT_CODE}",
            PLACEHOLDER,
        )
    return None


def _comments(path: str, comments: Iterable[tuple[int, str]]) -> list[Problem]:
    out: list[Problem] = []
    for line, text in comments:
        body = text.strip()
        if not body:
            continue
        if _ELLIPSIS.match(body) or _LAZY.search(body):
            out.append(
                Problem(
                    path,
                    f"has a comment where code should be (`{_clip(body, 50)}`). Write that code out in full.",
                    PLACEHOLDER,
                    line,
                )
            )
        elif _TODO.search(body) or _IMPLEMENT.search(body):
            out.append(
                Problem(
                    path,
                    f"has unfinished work left in it (`{_clip(body, 50)}`). Finish it, then remove the comment.",
                    PLACEHOLDER,
                    line,
                )
            )
    return out


def _values(path: str, content: str) -> list[Problem]:
    if _is_test(path):
        return []  # test data may say anything: "xxx" is a fine masked value to expect
    out: list[Problem] = []
    for n, line in enumerate(content.splitlines(), start=1):
        if line.lstrip().startswith(("#", "//", "*", "/*", "<!--", "--")):
            continue  # an example in a comment is not a value
        if _LOREM.search(line):
            out.append(Problem(path, "has lorem ipsum filler. Write real copy for this app.", PLACEHOLDER, n))
            continue
        for m in _FAKE_VALUE.finditer(line):
            if _PLACEHOLDER_ATTR.search(line[: m.start(1)]):
                continue  # an input's placeholder="xxx-xxx-xxxx" is a format hint
            out.append(
                Problem(
                    path,
                    f"has a stand-in value ({m.group(1)}{_clip(m.group(2), 30)}{m.group(1)}). "
                    "Read the real one from configuration, such as an environment variable.",
                    PLACEHOLDER,
                    n,
                )
            )
            break
    return out


# ── Python ───────────────────────────────────────────────────────────────────
def _python(path: str, content: str) -> list[Problem]:
    out: list[Problem] = []
    try:
        tree = ast.parse(content, filename=path)
    except (SyntaxError, ValueError):
        return []  # the parser names it
    out += _comments(path, _py_comments(content))
    name = _base(path)
    if _is_test(path) or name == "conftest.py":
        out += _py_tests(path, tree)
        return out
    if name != "__init__.py":
        body = [n for n in tree.body if not _is_docstring(n) and not isinstance(n, ast.Pass)]
        if not body:
            return [Problem(path, "has no code in it. Write the module.", PLACEHOLDER)]
        out += _py_stubs(path, tree)
    return out


def _py_comments(content: str) -> list[tuple[int, str]]:
    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(content).readline):
            if tok.type == tokenize.COMMENT:
                out.append((tok.start[0], tok.string.lstrip("#")))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return out


def _is_docstring(node: ast.AST) -> bool:
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    if isinstance(node, ast.Subscript):
        return _dotted(node.value)
    return ""


def _stub_body(body: list[ast.stmt]) -> Optional[str]:
    """What a body that does nothing consists of, or None when it does something."""
    what = None
    for stmt in body:
        if _is_docstring(stmt):
            what = what or "a docstring"
        elif isinstance(stmt, ast.Pass):
            what = "`pass`"
        elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and stmt.value.value is Ellipsis:
            what = "`...`"
        elif isinstance(stmt, ast.Raise) and _dotted(stmt.exc or ast.Pass()).split(".")[-1] == "NotImplementedError":
            what = "`raise NotImplementedError`"
        else:
            return None
    return what


_SKIP_DECORATORS = frozenset({"abstractmethod", "abstractproperty", "overload"})


def _py_stubs(path: str, tree: ast.Module) -> list[Problem]:
    """Functions and methods with no body, less the shapes that are meant to have none."""
    classes = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    # A base whose method a subclass in this file overrides: its body is a default.
    overridden: set[tuple[str, str]] = set()
    for cls in classes.values():
        mine = {n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for base in cls.bases:
            for method in mine:
                overridden.add((_dotted(base).split(".")[-1], method))

    def exempt_class(cls: ast.ClassDef) -> bool:
        for base in cls.bases:
            name = _dotted(base).split(".")[-1]
            if name in ("Protocol", "ABC", "Exception", "BaseException") or name.endswith(("Error", "Exception", "Warning")):
                return True
        return any(_dotted(k.value).split(".")[-1] == "ABCMeta" for k in cls.keywords if k.arg == "metaclass")

    out: list[Problem] = []

    def visit(node: ast.AST, owner: Optional[ast.ClassDef]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                if not exempt_class(child):
                    visit(child, child)
                continue
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorators = {_dotted(d).split(".")[-1] for d in child.decorator_list}
            name = child.name
            skip = (
                decorators & _SKIP_DECORATORS
                or (name.startswith("__") and name.endswith("__"))
                or (owner is not None and (owner.name, name) in overridden)
            )
            what = None if skip else _stub_body(child.body)
            if what:
                label = f"{owner.name}.{name}" if owner is not None else name
                out.append(
                    Problem(
                        path,
                        f"`{label}` has no body yet (only {what}). Write the real implementation.",
                        PLACEHOLDER,
                        child.lineno,
                    )
                )
            else:
                visit(child, None)  # nested functions are judged too

    visit(tree, None)
    return out


#: A call that checks something: `self.assertEqual`, `mock.assert_called_once`,
#: `assert_frame_equal`, `pytest.raises`, `pytest.fail`.
_PY_CHECK_CALL = re.compile(r"(?:^|\.)(?:assert\w*|fail|raises|warns|deprecated_call)$", re.IGNORECASE)


def _checks(node: ast.AST, helpers: set[str]) -> tuple[int, int]:
    """(assertions, of which always true) in a test body."""
    total = trivial = 0
    for n in ast.walk(node):
        if isinstance(n, ast.Assert):
            total += 1
            trivial += 1 if _always_true(n.test) else 0
        elif isinstance(n, ast.Call):
            name = _dotted(n.func)
            if _PY_CHECK_CALL.search(name):
                total += 1
                last = name.split(".")[-1]
                if last in ("assertTrue", "assert_") and n.args and _always_true(n.args[0]):
                    trivial += 1
                elif last in ("assertEqual", "assertEquals") and len(n.args) >= 2 and _same(n.args[0], n.args[1]):
                    trivial += 1
            elif name.split(".")[-1] in helpers:
                total += 1
    return total, trivial


def _always_true(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return bool(node.value) and node.value is not Ellipsis
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], (ast.Eq, ast.Is)):
        return _same(node.left, node.comparators[0])
    return False


def _same(a: ast.AST, b: ast.AST) -> bool:
    return isinstance(a, (ast.Constant, ast.Name)) and ast.dump(a) == ast.dump(b)


def _py_tests(path: str, tree: ast.Module) -> list[Problem]:
    # Helpers that assert, so a test that calls one asserts too — to a fixed point, as
    # helpers call helpers.
    funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    helpers: set[str] = set()
    while True:
        more = {f.name for f in funcs if not f.name.startswith("test") and f.name not in helpers and _checks(f, helpers)[0]}
        if not more:
            break
        helpers |= more

    def is_test(node: ast.AST) -> bool:
        # `@pytest.fixture def test_user()` is a fixture named like a test, not a test.
        return (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test")
            and not any(_dotted(d).split(".")[-1] == "fixture" for d in node.decorator_list)
        )

    tests: list[tuple[str, ast.AST]] = []
    for node in tree.body:
        if is_test(node):
            tests.append((node.name, node))
        elif isinstance(node, ast.ClassDef) and (
            node.name.startswith("Test") or any(_dotted(b).endswith("TestCase") for b in node.bases)
        ):
            for item in node.body:
                if is_test(item):
                    tests.append((f"{node.name}.{item.name}", item))

    out: list[Problem] = []
    for name, node in tests:
        skipped = next(
            (d for d in getattr(node, "decorator_list", []) if _dotted(d).split(".")[-1] in ("skip", "skipIf", "skipif")),
            None,
        )
        if skipped is not None and _dotted(skipped).split(".")[-1] == "skip":
            out.append(Problem(path, f"test `{name}` is skipped, so it checks nothing. Write it out or remove it.", WEAK_TEST, node.lineno))
            continue
        total, trivial = _checks(node, helpers)
        if not total:
            out.append(
                Problem(
                    path,
                    f"test `{name}` asserts nothing, so it passes whatever the code does. "
                    "Check what the code returns with `assert`.",
                    WEAK_TEST,
                    node.lineno,
                )
            )
        elif trivial == total:
            out.append(
                Problem(
                    path,
                    f"test `{name}` only asserts something that is always true. "
                    "Assert on what the code returns.",
                    WEAK_TEST,
                    node.lineno,
                )
            )
    out += _py_mocks_subject(path, tree)
    return out


def _subject(path: str) -> str:
    """The module a test file is named after: `test_api.py` → `api`, `Api.test.tsx` → `Api`."""
    name = _base(path)
    stem = re.sub(r"\.(?:test|spec)\.[cm]?[jt]sx?$", "", name)
    stem = re.sub(r"^test_|_test(?=\.py$)|\.py$", "", stem)
    if stem in ("index", ""):
        stem = posixpath.basename(posixpath.dirname(path))
    return stem


def _py_mocks_subject(path: str, tree: ast.Module) -> list[Problem]:
    subject = _subject(path)
    if not subject or subject in ("tests", "test"):
        return []
    # The module under test is one this file imports: patching a same-named dependency
    # (`app.services.users` beside `app.routes.users`) is how a unit is isolated.
    imported: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            imported |= {n.module, *(f"{n.module}.{a.name}" for a in n.names)}
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call) or _dotted(n.func).split(".")[-1] != "patch" or not n.args:
            continue
        target = n.args[0]
        if isinstance(target, ast.Constant) and isinstance(target.value, str):
            if target.value.split(".")[-1] == subject and target.value in imported:
                return [
                    Problem(
                        path,
                        f"mocks `{target.value}`, the module it tests, so it tests the mock. "
                        f"Mock what `{subject}` depends on instead.",
                        WEAK_TEST,
                        n.lineno,
                    )
                ]
    return []


# ── JavaScript and TypeScript ────────────────────────────────────────────────
_KEYWORDS_BEFORE_REGEX = frozenset(
    "return typeof instanceof in of new delete void throw case do else yield await".split()
)


def lex(content: str) -> tuple[str, list[tuple[int, str]]]:
    """(the code with every string, template, regex and comment emptied to spaces,
    the comments with their lines).

    Quote marks stay where they were, so an emptied string is never mistaken for an
    empty block. A string that runs past its line ends there — JSX text such as
    `Don't` opens a quote that never closes, and that must not swallow the file.
    """
    out = list(content)
    comments: list[tuple[int, str]] = []
    i, n = 0, len(content)
    line = 1
    prev = ""  # the last significant character, for "is this / a regex?"
    word = ""  # the last identifier, for `return /re/`
    braces: list[str] = []  # "{" in code, "`" for a `${` inside a template

    def blank(a: int, b: int) -> None:
        for k in range(a, b):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        ch = content[i]
        if ch == "\n":
            line += 1
            i += 1
            continue
        if content.startswith("//", i):
            end = content.find("\n", i)
            end = n if end < 0 else end
            comments.append((line, content[i + 2 : end]))
            blank(i, end)
            i = end
            continue
        if content.startswith("/*", i):
            end = content.find("*/", i + 2)
            end = n if end < 0 else end + 2
            text = content[i + 2 : end - 2]
            for k, part in enumerate(text.split("\n")):
                comments.append((line + k, part.strip().lstrip("*")))
            line += content.count("\n", i, end)
            blank(i, end)
            i = end
            continue
        if ch in "'\"":
            j = i + 1
            while j < n and content[j] != ch and content[j] != "\n":
                j += 2 if content[j] == "\\" else 1
            blank(i + 1, min(j, n))
            i = j + 1 if j < n and content[j] == ch else j
            prev, word = ch, ""
            continue
        if ch == "`" or (ch == "}" and braces and braces[-1] == "`"):
            if ch == "}":
                braces.pop()
            j = i + 1
            while j < n and content[j] != "`":
                if content[j] == "\\":
                    j += 2
                    continue
                if content.startswith("${", j):
                    braces.append("`")
                    break
                if content[j] == "\n":
                    line += 1
                j += 1
            blank(i + 1, min(j, n))
            i = j + 2 if j < n and content.startswith("${", j) else j + 1
            prev, word = "`", ""
            continue
        # Not after `<` or `>`: in JSX, `</a>` is a closing tag, never a pattern.
        if ch == "/" and (prev == "" or prev in "(,=:[!&|?{;+-*%~^" or word in _KEYWORDS_BEFORE_REGEX):
            j, in_class = i + 1, False
            while j < n and content[j] != "\n":
                c = content[j]
                if c == "\\":
                    j += 2
                    continue
                if c == "[":
                    in_class = True
                elif c == "]":
                    in_class = False
                elif c == "/" and not in_class:
                    break
                j += 1
            if j < n and content[j] == "/":
                blank(i + 1, j)
                i = j + 1
                prev, word = "/", ""
                continue
        if ch == "{":
            braces.append("{")
        elif ch == "}" and braces:
            braces.pop()
        if ch.isalnum() or ch in "_$":
            j = i
            while j < n and (content[j].isalnum() or content[j] in "_$"):
                j += 1
            word = content[i:j]
            prev = word[-1]
            i = j
            continue
        if not ch.isspace():
            prev, word = ch, ""
        i += 1
    return "".join(out), comments


def _close(code: str, i: int, open_: str, close: str) -> int:
    """The index of the bracket closing the one at `i`, or -1."""
    depth = 0
    for j in range(i, len(code)):
        c = code[j]
        if c == open_:
            depth += 1
        elif c == close:
            depth -= 1
            if depth == 0:
                return j
    return -1


def _line_at(code: str, i: int) -> int:
    return code.count("\n", 0, i) + 1


#: An exported function or component, by the way it is written.
_JS_DECL = re.compile(
    r"\bexport\s+(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)?\s*(?:<[^>]*>)?\s*\("
    r"|\bexport\s+(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s+)?"
    r"(?:function\s*\*?\s*\(|\(|[A-Za-z_$][\w$]*\s*=>)"
    r"|\bexport\s+default\s+(?:async\s+)?\("
)
_LOCAL_DECL = r"(?:\bfunction\s+{name}\s*(?:<[^>]*>)?\s*\(|\b(?:const|let|var)\s+{name}\s*(?::[^=]+)?=\s*(?:async\s+)?(?:function\s*\(|\(|[A-Za-z_$][\w$]*\s*=>))"
#: Helpers whose whole job is to do nothing.
_NOOP_NAMES = re.compile(r"^(?:noop|nothing|identity|_+)$")
_NOT_IMPLEMENTED = re.compile(r"^\s*throw\s+new\s+Error\s*\(\s*['\"`]\s*(?:not\s+implemented|todo|implement)", re.IGNORECASE)
#: A component that renders nothing: `null`, a bare fragment, or one empty container
#: (`<div />`, `<main></main>`). Containers only — `<hr />` is a whole divider, and
#: `<StandupForm />` is a component that renders plenty.
_EMPTY_TAG = r"(?:div|main|section|article|span|p|header|footer|nav|aside)"
_NULL_BODY = re.compile(
    rf"^\s*return\s*(?:null|undefined|<>\s*</>|<{_EMPTY_TAG}\s*/>|<({_EMPTY_TAG})>\s*</\1>)?\s*;?\s*$"
)


def _js_body(code: str, start: int) -> Optional[tuple[int, int]]:
    """From the `(` of a parameter list: the function's body as (open, close) indices,
    or None for an arrow with an expression body."""
    end = _close(code, start, "(", ")")
    if end < 0:
        return None
    k, n = end + 1, len(code)
    while k < n and code[k].isspace():
        k += 1
    if k < n and code[k] == ":":
        # A return type, which may hold braces of its own (`Promise<{}>`, `{ a: string }`):
        # the body is the first `{` outside it, once some of the type has been read.
        k += 1
        depth, started, last = 0, False, ":"
        while k < n:
            c = code[k]
            if c == "=" and code.startswith("=>", k) and depth == 0:
                break
            if c == "{" and depth == 0 and started and last not in "|&<,:(":
                break
            if c in "<([{":
                depth += 1
            elif c in ">)]}":
                depth = max(depth - 1, 0)
            if not c.isspace():
                started, last = True, c
            k += 1
    m = re.compile(r"\s*(=>)?\s*\{").match(code, k)
    if not m:
        return None
    brace = m.end() - 1
    close = _close(code, brace, "{", "}")
    return (brace, close) if close > 0 else None


def _javascript(path: str, content: str) -> list[Problem]:
    code, comments = lex(content)
    out = _comments(path, comments)
    if not code.strip(" \n\t;"):
        return [Problem(path, "has no code in it. Write the module.", PLACEHOLDER)]
    if _is_test(path):
        return out + _js_tests(path, content, code)
    component_file = path.endswith((".jsx", ".tsx"))

    def judge(name: str, paren: int, at: int) -> None:
        if name and _NOOP_NAMES.match(name):
            return
        body = _js_body(code, paren)
        if body is None:
            return
        inner_code = code[body[0] + 1 : body[1]]
        inner_text = content[body[0] + 1 : body[1]]
        label = f"`{name}`" if name else "The default export"
        if not inner_code.strip():
            out.append(Problem(path, f"{label} has an empty body. Write the real implementation.", PLACEHOLDER, _line_at(code, at)))
        elif _NOT_IMPLEMENTED.match(inner_text):
            out.append(
                Problem(path, f"{label} only throws “not implemented”. Write the real implementation.", PLACEHOLDER, _line_at(code, at))
            )
        elif component_file and name[:1].isupper() and _NULL_BODY.match(inner_code.replace("\n", " ")):
            out.append(
                Problem(path, f"{label} renders nothing. Build the real component.", PLACEHOLDER, _line_at(code, at))
            )

    seen: set[int] = set()
    for m in _JS_DECL.finditer(code):
        if not m.group(0).endswith("("):
            continue  # `x => …` with one bare parameter: rare for a stub, and skipped
        paren = m.end() - 1
        if paren not in seen:
            seen.add(paren)
            judge(m.group(1) or m.group(2) or "", paren, m.start())
    # `function Foo() {}` exported at the bottom: `export default Foo` / `export { Foo }`.
    exported = set(re.findall(r"\bexport\s+default\s+([A-Za-z_$][\w$]*)\s*;?\s*$", code, re.MULTILINE))
    for group in re.findall(r"\bexport\s*\{([^}]*)\}", code):
        exported |= {part.split(" as ")[0].strip() for part in group.split(",") if part.strip()}
    for name in exported:
        if not re.match(r"^[A-Za-z_$][\w$]*$", name):
            continue
        m = re.search(_LOCAL_DECL.format(name=re.escape(name)), code)
        if m and (m.end() - 1) not in seen:
            seen.add(m.end() - 1)
            judge(name, m.end() - 1, m.start())

    if component_file:
        for m in _JSX_TEXT.finditer(content):
            text = m.group(1).strip()
            if (_JSX_FILLER.match(text) or _JSX_TODO.match(text)) and code[m.start()] == ">":
                out.append(
                    Problem(
                        path,
                        f"shows placeholder text (“{_clip(text, 40)}”). Build the real content.",
                        PLACEHOLDER,
                        _line_at(content, m.start()),
                    )
                )
    return out


_JS_TEST = re.compile(r"\b(it|test)(\.(?:only|concurrent|skip|todo|each\b[^(]*\([^)]*\)))?\s*\(")
_JS_CHECK = re.compile(
    r"\bexpect\s*[.(]|\bexpectTypeOf\b|\bassert\w*\s*[.(]|\.should\b|\.expect\s*\(|\btoMatch\w*Snapshot\b"
    r"|\bt\.(?:is|not|deepEqual|true|false|truthy|falsy|throws|snapshot)\s*\("
)
_JS_ALWAYS = re.compile(
    r"\bexpect\s*\(\s*(true|false|null|\d+)\s*\)\s*\.\s*(?:toBe|toEqual|toStrictEqual)\s*\(\s*\1\s*\)"
    r"|\bexpect\s*\(\s*(?:true|\d*[1-9]\d*)\s*\)\s*\.\s*toBeTruthy\s*\(\s*\)"
    r"|\bexpect\s*\(\s*(?:false|null|undefined|0)\s*\)\s*\.\s*toBeFalsy\s*\(\s*\)"
)


def _js_tests(path: str, content: str, code: str) -> list[Problem]:
    out: list[Problem] = []
    # Helpers that check something count as checks in a test that calls them.
    helpers: set[str] = set()
    for m in re.finditer(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(|\b(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?\(", code):
        name = m.group(1) or m.group(2)
        body = _js_body(code, m.end() - 1)
        if body and _JS_CHECK.search(code[body[0] : body[1]]):
            helpers.add(name)
    helper_call = re.compile(r"\b(?:%s)\s*\(" % "|".join(re.escape(h) for h in helpers)) if helpers else None

    for m in _JS_TEST.finditer(code):
        # A method call `.it(` or a property is not a test.
        if m.start() > 0 and code[m.start() - 1] == ".":
            continue
        modifier = (m.group(2) or "").lstrip(".")
        end = _close(code, m.end() - 1, "(", ")")
        if end < 0:
            continue
        title_match = re.match(r"\s*(['\"`])(.*?)\1", content[m.end() : end])
        title = _clip(title_match.group(2), 50) if title_match else m.group(1)
        line = _line_at(code, m.start())
        if modifier in ("skip", "todo"):
            out.append(
                Problem(
                    path,
                    f"test “{title}” is {'skipped' if modifier == 'skip' else 'only a todo'}, so it checks nothing. "
                    "Write it out or remove it.",
                    WEAK_TEST,
                    line,
                )
            )
            continue
        span_code = code[m.end() : end]
        span_text = content[m.end() : end]
        checks = len(_JS_CHECK.findall(span_code)) + (len(helper_call.findall(span_code)) if helper_call else 0)
        if not checks:
            out.append(
                Problem(
                    path,
                    f"test “{title}” asserts nothing, so it passes whatever the code does. "
                    "Check what the code returns with `expect`.",
                    WEAK_TEST,
                    line,
                )
            )
        elif len(_JS_ALWAYS.findall(span_text)) >= checks:
            out.append(
                Problem(
                    path,
                    f"test “{title}” only asserts something that is always true. Assert on what the code returns.",
                    WEAK_TEST,
                    line,
                )
            )
    out += _js_mocks_subject(path, content, code)
    return out


def _spec_stem(spec: str) -> str:
    stem = posixpath.basename(spec.rstrip("/"))
    stem = re.sub(r"\.[cm]?[jt]sx?$", "", stem)
    if stem == "index":
        stem = posixpath.basename(posixpath.dirname(spec.rstrip("/")))
    return stem


def _js_mocks_subject(path: str, content: str, code: str) -> list[Problem]:
    subject = _subject(path)
    if not subject:
        return []
    from app.build.scaffold import js_imports

    imported = {s for s in js_imports(content) if s.startswith(".") or s.startswith("@/")}
    for m in re.finditer(r"\b(?:jest|vi)\s*\.\s*mock\s*\(\s*(['\"])([^'\"]+)\1", content):
        spec = m.group(2)
        if not (spec.startswith(".") or spec.startswith("@/")) or _spec_stem(spec) != subject:
            continue
        if spec not in imported:
            continue
        end = _close(code, code.find("(", m.start()), "(", ")")
        if end > 0 and re.search(r"\b(?:requireActual|importActual)\b", content[m.start() : end]):
            continue  # a partial mock keeps the real module
        return [
            Problem(
                path,
                f"mocks `{spec}`, the module it tests, so it tests the mock. Mock what `{subject}` depends on instead.",
                WEAK_TEST,
                _line_at(content, m.start()),
            )
        ]
    return []


# ── comments in other files ──────────────────────────────────────────────────
def _block_comments(content: str) -> list[tuple[int, str]]:
    out = []
    for m in re.finditer(r"/\*(.*?)\*/", content, re.DOTALL):
        line = _line_at(content, m.start())
        for k, part in enumerate(m.group(1).split("\n")):
            out.append((line + k, part.strip().lstrip("*")))
    return out


def _html_comments(content: str) -> list[tuple[int, str]]:
    return [(_line_at(content, m.start()), m.group(1)) for m in re.finditer(r"<!--(.*?)-->", content, re.DOTALL)]


def _dash_comments(content: str) -> list[tuple[int, str]]:
    return [(n, line.split("--", 1)[1]) for n, line in enumerate(content.splitlines(), start=1) if line.lstrip().startswith("--")]


# ── edit mode: a rewrite that lost the file ──────────────────────────────────
#: A change that asks for code to go may shrink the file as much as it likes.
#: Whole words in their own forms, so "dropdown", "Stripe" and "cutoff" are not asks.
_REMOVAL = re.compile(
    r"\b(?:remov(?:e|es|ed|ing)|delet(?:e|es|ed|ing)|drop(?:s|ped|ping)?|strip(?:s|ped|ping)?|get rid of|take out"
    r"|cut(?:s|ting)? (?:out|down|back)|simplif(?:y|ies|ied|ying)|shorten(?:s|ed|ing)?|trim(?:s|med|ming)?"
    r"|clean(?:s|ed|ing)?[- ]?up|refactor(?:s|ed|ing)?|split(?:s|ting)?|extract(?:s|ed|ing)?|mov(?:e|es|ed|ing)"
    r"|merg(?:e|es|ed|ing)|replac(?:e|es|ed|ing)|rewrit(?:e|es|ing|ten)|redesign(?:s|ed|ing)?|convert(?:s|ed|ing)?"
    r"|swap(?:s|ped|ping)?|instead of|turn(?:s|ed|ing)? (?:it |this |that )?into|(?:just|only) (?:a|an|the|one)"
    r"|smaller|shorter|minimal|fewer|less)\b",
    re.IGNORECASE,
)
#: Below this, a file is too small for a share of it to mean anything.
_LOST_MIN_LINES = 12
_LOST_SHARE = 0.4


def _definitions(path: str, content: str) -> list[str]:
    if path.endswith(".py"):
        try:
            tree = ast.parse(content)
        except (SyntaxError, ValueError):
            return []
        names: list[str] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names += [t.id for t in targets if isinstance(t, ast.Name)]
        return names
    if path.endswith(JS_EXT):
        return re.findall(
            r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function\*?|class|const|let|var|interface|type|enum)\s+([A-Za-z_$][\w$]*)",
            content,
            re.MULTILINE,
        )
    return []


def lost_code(path: str, before: str, after: str, asked: str = "") -> Optional[Problem]:
    """A rewrite of an existing file that dropped much of it — unless the change asked
    for code to go. Lines and top-level definitions both count: a file can keep its
    length and still lose the half that mattered."""
    if not before or after is None or _REMOVAL.search(asked or ""):
        return None
    old = [l for l in before.splitlines() if l.strip()]
    new = [l for l in after.splitlines() if l.strip()]
    if len(old) < _LOST_MIN_LINES:
        return None
    lost_lines = len(old) - len(new)
    old_defs = list(dict.fromkeys(_definitions(path, before)))
    new_defs = set(_definitions(path, after))
    gone = [d for d in old_defs if d not in new_defs]
    lines_bad = lost_lines > _LOST_SHARE * len(old)
    defs_bad = len(old_defs) >= 3 and len(gone) > _LOST_SHARE * len(old_defs)
    if not (lines_bad or defs_bad):
        return None
    named = ""
    if gone:
        shown = [f"`{d}`" for d in gone[:4]]
        named = (", ".join(shown[:-1]) + " and " + shown[-1]) if len(shown) > 1 else shown[0]
        named += " and more" if len(gone) > 4 else ""
    what = f"{lost_lines} of its {len(old)} lines" + (f", including {named}" if named else "") if lines_bad else named
    return Problem(path, f"dropped {what}. Return the whole file with only the change made.", PLACEHOLDER)


def edited(path: str, before: str, after: str, asked: str = "", cap: Optional[int] = _PER_FILE) -> list[Problem]:
    """What an edit is answerable for in a file it rewrote (#79): the placeholders it
    added — not a TODO the file already had — and the code it lost, unless the change
    asked for code to go. One rule for the per-file pass and the gate."""
    if after == before:
        return []
    found = new_in(file_placeholders(path, after, None), file_placeholders(path, before, None))
    found = found if cap is None else found[:cap]
    lost = lost_code(path, before, after, asked)
    return found + ([lost] if lost is not None else [])


def is_lost_code(problem: Problem) -> bool:
    return problem.kind == PLACEHOLDER and problem.message.startswith("dropped ")


def counts(problems: Iterable[Problem]) -> dict[str, int]:
    """`{"placeholders": n, "weak_tests": n}` — the eval scorecard's two numbers."""
    found = list(problems)
    return {
        "placeholders": sum(1 for p in found if p.kind == PLACEHOLDER),
        "weak_tests": sum(1 for p in found if p.kind == WEAK_TEST),
    }
