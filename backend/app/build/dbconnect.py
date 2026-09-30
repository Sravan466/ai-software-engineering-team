"""Connecting the database Atlas picked: which variables it needs, and checking them.

A build used to end with a `.env.example` holding a localhost placeholder, and the
person downloading it had to work out which variable their code read, what shape
their provider's connection string takes, and why it did not connect. This module is
the part of that the platform can answer:

  **The contract.** One table from (database, provider) to the variable *names* the
  generated code reads. The names go into the stack charter, so every agent writes
  code against exactly those names. The values never go anywhere near a model.

  **Parsing.** What people paste is a connection string copied from a dashboard,
  often with `<password>` still in it, an `@` in the password, or quotes around it.
  Those are caught here, before anything touches the network.

  **The test.** A live ping when the driver is installed, a DNS and TCP reach
  otherwise, and an HTTP call for Supabase. Every failure maps to a cause and a fix
  a person can act on, never "something went wrong". The result never says
  "connected" unless something actually answered with those credentials.

Nothing here stores anything. `app.core.project_secrets` does that.
"""
from __future__ import annotations

import base64
import json
import re
import socket
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional
from urllib.parse import quote, unquote, urlsplit

from app.core.logging import get_logger

log = get_logger(__name__)

#: How long any one check may take, end to end.
TIMEOUT_SECONDS = 5.0


# ── the contract ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Var:
    """One variable the generated code reads."""

    name: str
    label: str
    #: A secret is shown back only as `…last4`; anything else is shown whole.
    secret: bool = True
    required: bool = True
    placeholder: str = ""
    #: `uri` values are parsed as connection strings; `url`, `key` and `text` are not.
    kind: str = "key"
    #: The schemes a `uri` may use.
    schemes: tuple[str, ...] = ()
    help: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "secret": self.secret,
            "required": self.required,
            "placeholder": self.placeholder,
            "kind": self.kind,
            "help": self.help,
        }


@dataclass(frozen=True)
class Contract:
    database: str
    provider: str
    #: What to call it on screen: "MongoDB Atlas", "Supabase", "Other Postgres".
    label: str
    variables: tuple[Var, ...]
    #: Firebase is pasted as one `firebaseConfig` object and split into six.
    paste_object: bool = False

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(v.name for v in self.variables)

    def var(self, name: str) -> Optional[Var]:
        return next((v for v in self.variables if v.name == name), None)

    def as_dict(self) -> dict:
        return {
            "database": self.database,
            "provider": self.provider,
            "label": self.label,
            "variables": [v.as_dict() for v in self.variables],
            "paste_object": self.paste_object,
        }


_MONGO_URI = Var(
    "MONGODB_URI",
    "Connection string",
    placeholder="mongodb+srv://user:password@cluster0.xxxxx.mongodb.net/app",
    kind="uri",
    schemes=("mongodb", "mongodb+srv"),
)
_PG_URI = Var(
    "DATABASE_URL",
    "Connection string",
    placeholder="postgresql://user:password@host:5432/app",
    kind="uri",
    schemes=("postgres", "postgresql"),
)
_MYSQL_URI = Var(
    "DATABASE_URL",
    "Connection string",
    placeholder="mysql://user:password@host:3306/app",
    kind="uri",
    schemes=("mysql",),
)
_FIREBASE = tuple(
    Var(name, label, secret=secret, placeholder=ph, kind="text")
    for name, label, secret, ph in (
        ("FIREBASE_API_KEY", "API key", True, "AIza…"),
        ("FIREBASE_AUTH_DOMAIN", "Auth domain", False, "your-app.firebaseapp.com"),
        ("FIREBASE_PROJECT_ID", "Project ID", False, "your-app"),
        ("FIREBASE_STORAGE_BUCKET", "Storage bucket", False, "your-app.appspot.com"),
        ("FIREBASE_MESSAGING_SENDER_ID", "Messaging sender ID", False, "123456789012"),
        ("FIREBASE_APP_ID", "App ID", False, "1:123456789012:web:abc123"),
    )
)

CONTRACTS: dict[tuple[str, str], Contract] = {
    ("mongodb", "atlas"): Contract("mongodb", "atlas", "MongoDB Atlas", (_MONGO_URI,)),
    ("mongodb", "generic"): Contract("mongodb", "generic", "MongoDB", (_MONGO_URI,)),
    ("postgres", "supabase"): Contract(
        "postgres",
        "supabase",
        "Supabase",
        (
            Var(
                "SUPABASE_URL",
                "Project URL",
                secret=False,
                placeholder="https://abcdefghijklmnop.supabase.co",
                kind="url",
            ),
            Var(
                "SUPABASE_ANON_KEY",
                "Publishable key",
                placeholder="sb_publishable_…",
                help="The publishable (anon) key. Never the secret or service_role key.",
            ),
            Var(
                "DATABASE_URL",
                "Postgres connection string",
                required=False,
                placeholder="postgresql://postgres.ref:password@aws-0-region.pooler.supabase.com:5432/postgres",
                kind="uri",
                schemes=("postgres", "postgresql"),
                help="Optional. Use the Session pooler string, which works over IPv4.",
            ),
        ),
    ),
    ("postgres", "neon"): Contract("postgres", "neon", "Neon", (_PG_URI,)),
    ("postgres", "generic"): Contract("postgres", "generic", "Postgres", (_PG_URI,)),
    ("mysql", "planetscale"): Contract("mysql", "planetscale", "PlanetScale", (_MYSQL_URI,)),
    ("mysql", "generic"): Contract("mysql", "generic", "MySQL", (_MYSQL_URI,)),
    ("firestore", "firebase"): Contract(
        "firestore", "firebase", "Firebase", _FIREBASE, paste_object=True
    ),
    ("dynamodb", "aws"): Contract(
        "dynamodb",
        "aws",
        "DynamoDB",
        (
            Var("AWS_REGION", "Region", secret=False, placeholder="us-east-1", kind="text"),
            Var("AWS_ACCESS_KEY_ID", "Access key ID", placeholder="AKIA…"),
            Var("AWS_SECRET_ACCESS_KEY", "Secret access key", placeholder="40 characters"),
        ),
    ),
}

#: The providers a database can be switched between on the card, first is default.
PROVIDERS: dict[str, tuple[str, ...]] = {
    "mongodb": ("atlas", "generic"),
    "postgres": ("supabase", "neon", "generic"),
    "mysql": ("planetscale", "generic"),
    "firestore": ("firebase",),
    "dynamodb": ("aws",),
}

#: Databases that need nothing from the user: a file database has no URI.
NEEDS_CREDENTIALS = frozenset(PROVIDERS)

#: Aliases that name a provider in Atlas's prose, longest first where it matters.
_PROVIDER_ALIASES: tuple[tuple[str, str, str], ...] = (
    ("mongodb", "mongodb atlas", "atlas"),
    ("mongodb", "atlas", "atlas"),
    ("postgres", "supabase", "supabase"),
    ("postgres", "neon", "neon"),
    ("mysql", "planetscale", "planetscale"),
    ("mysql", "planet scale", "planetscale"),
)


def default_provider(database: Optional[str]) -> Optional[str]:
    choices = PROVIDERS.get(database or "")
    if not choices:
        return None
    # Only one choice (Firebase, AWS) is the provider; otherwise "generic" until the
    # architecture names a host.
    return choices[0] if len(choices) == 1 else "generic"


def provider_for(database: Optional[str], texts: Iterable[object]) -> Optional[str]:
    """Which host Atlas named for this database, read from its own words.

    The charter folds Supabase into `postgres` and PlanetScale into `mysql`, which is
    right for checking code and wrong for telling someone where to find a key. This
    reads the same prose back for the host. "neon" is only trusted here, beside a
    database already known to be Postgres; on its own it is an English word.
    """
    if database not in PROVIDERS:
        return None
    haystack = " " + re.sub(r"[^a-z0-9]+", " ", " ".join(str(t) for t in texts).lower()) + " "
    for db, alias, provider in _PROVIDER_ALIASES:
        if db == database and f" {alias} " in haystack:
            return provider
    return default_provider(database)


def contract_for(database: Optional[str], provider: Optional[str] = None) -> Optional[Contract]:
    if database not in PROVIDERS:
        return None
    return CONTRACTS.get((database, provider or "")) or CONTRACTS.get(
        (database, default_provider(database) or "")
    )


def env_names(database: Optional[str], provider: Optional[str]) -> tuple[str, ...]:
    """Every variable name any provider of this database may use, contract first.

    The charter carries the names so the code reads them. Supabase's optional
    `DATABASE_URL` is included: code that talks to Postgres directly reads it.
    """
    contract = contract_for(database, provider)
    return contract.names if contract else ()


# ── placeholders for `.env.example` ──────────────────────────────────────────
def placeholder(name: str) -> Optional[str]:
    """The `.env.example` value for a contract variable, or None if not one of ours.

    None for a connection string too: the scaffold's localhost default is what makes
    `cp .env.example .env` run against a local database.
    """
    for contract in CONTRACTS.values():
        var = contract.var(name)
        if var is not None:
            if var.kind == "uri":
                return None
            if var.kind == "url" or not var.secret:
                return var.placeholder.replace("…", "")
            return "change-me"
    return None


# ── parsing what was pasted ──────────────────────────────────────────────────
@dataclass
class Problem:
    """A value that cannot be saved as it is, and what to do about it."""

    name: str
    message: str
    #: Which guide step fixes it, 1-based, when one does.
    step: Optional[int] = None

    def as_dict(self) -> dict:
        return {"name": self.name, "message": self.message, "step": self.step}


@dataclass
class Parsed:
    values: dict[str, str] = field(default_factory=dict)
    problems: list[Problem] = field(default_factory=list)
    #: Changes made on the person's behalf, said out loud: "we encoded the @".
    notices: list[Problem] = field(default_factory=list)


_PLACEHOLDER = re.compile(
    r"<\s*(db_)?password\s*>|\[\s*your[-_ ]password\s*\]|your[-_]password|<\s*username\s*>|<\s*user\s*>",
    re.IGNORECASE,
)
#: Characters that break a connection string when they appear raw in the password.
_RESERVED = set("@:/?#[]")


def _trim(value: object) -> str:
    text = str(value or "").strip()
    # A value copied with the quotes from a `.env` line or a code sample.
    while len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'`":
        text = text[1:-1].strip()
    # And the `NAME=` from a `.env` line.
    m = re.match(r"^[A-Z][A-Z0-9_]*\s*=\s*(.+)$", text)
    if m and "://" in m.group(1):
        text = _trim(m.group(1))
    return text


def _split_uri(text: str) -> Optional[tuple[str, str, str, str]]:
    """(scheme, userinfo, host part, rest) — by the *last* `@`, so an `@` in a
    password does not end the userinfo early the way a URL parser would."""
    m = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*)://(.*)$", text, re.DOTALL)
    if not m:
        return None
    scheme, body = m.group(1).lower(), m.group(2)
    at = body.rfind("@")
    userinfo, after = (body[:at], body[at + 1 :]) if at >= 0 else ("", body)
    cut = re.search(r"[/?#]", after)
    host, rest = (after[: cut.start()], after[cut.start() :]) if cut else (after, "")
    return scheme, userinfo, host, rest


def _encode_part(part: str) -> str:
    return quote(unquote(part), safe="")


def parse_uri(var: Var, raw: str, database: str, provider: str) -> tuple[Optional[str], list[Problem], list[Problem]]:
    problems: list[Problem] = []
    notices: list[Problem] = []
    text = _trim(raw)
    if any(c.isspace() for c in text):
        problems.append(Problem(var.name, "There's a space or line break inside it. Copy the connection string again, on one line."))
        return None, problems, notices
    parts = _split_uri(text)
    if parts is None:
        want = " or ".join(f"{s}://" for s in var.schemes)
        problems.append(Problem(var.name, f"That isn't a connection string. It should start with {want}"))
        return None, problems, notices
    scheme, userinfo, host, rest = parts
    if scheme not in var.schemes:
        want = " or ".join(f"{s}://" for s in var.schemes)
        problems.append(
            Problem(var.name, f"This starts with {scheme}://, but a {_db_label(database)} connection string starts with {want}")
        )
        return None, problems, notices
    if _PLACEHOLDER.search(text):
        problems.append(
            Problem(
                var.name,
                "It still says <password> — replace it with your database user's password.",
                step=_step(provider, "password"),
            )
        )
        return None, problems, notices
    if not host:
        problems.append(Problem(var.name, "There's no host after the @. Copy the whole string from the Connect screen."))
        return None, problems, notices
    if userinfo:
        user, sep, password = userinfo.partition(":")
        fixed_user = user if not (_RESERVED & set(user)) else _encode_part(user)
        fixed_password = password
        if sep and _RESERVED & set(password):
            fixed_password = _encode_part(password)
            shown = "".join(sorted(_RESERVED & set(password)))
            notices.append(
                Problem(var.name, f"Your password has {' '.join(shown)} in it — we encoded it so the string still reads correctly.")
            )
        if not sep and database != "mongodb":
            notices.append(Problem(var.name, "There's no password in it. That only works if your database allows it."))
        userinfo = fixed_user + (":" + fixed_password if sep else "")
    elif database in ("postgres", "mysql"):
        problems.append(Problem(var.name, "There's no user name or password in it. Add them before the @: user:password@host."))
        return None, problems, notices
    path = rest.split("?", 1)[0].split("#", 1)[0]
    if database in ("postgres", "mysql") and path.strip("/") == "":
        problems.append(Problem(var.name, "It doesn't name a database. Add one after the host: …/app"))
        return None, problems, notices
    if database == "mongodb" and path.strip("/") == "":
        notices.append(
            Problem(var.name, "It doesn't name a database, so MongoDB will use one called test. Add one after the host (…mongodb.net/app?…) to choose.")
        )
    if provider == "neon" and "sslmode=" not in rest:
        notices.append(Problem(var.name, "Neon needs TLS — add ?sslmode=require to the end.", step=_step(provider, "copy")))
    uri = f"{scheme}://" + (f"{userinfo}@" if userinfo else "") + host + rest
    return uri, problems, notices


def _jwt_role(token: str) -> Optional[str]:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("role")
    except Exception:  # noqa: BLE001 - not a JWT, or not one we can read
        return None


def _check_plain(var: Var, text: str, contract: Contract) -> Optional[Problem]:
    name = var.name
    if name == "SUPABASE_URL":
        if not re.match(r"^https://[a-z0-9]{10,40}\.supabase\.(co|in)/?$", text):
            return Problem(name, "The Project URL looks like https://abcdefghijklmnop.supabase.co — copy it from Project Settings → API.", step=1)
    elif name == "SUPABASE_ANON_KEY":
        if text.startswith("sb_secret_") or _jwt_role(text) == "service_role":
            return Problem(name, "That's a secret key. It bypasses your security rules — paste the publishable (anon) key instead.", step=1)
        if not (text.startswith("sb_publishable_") or _jwt_role(text) == "anon"):
            return Problem(name, "This doesn't look like a publishable key. It starts with sb_publishable_ (or eyJ for a legacy anon key).", step=1)
    elif name == "AWS_REGION":
        if not re.match(r"^[a-z]{2}(-gov)?-[a-z]+-\d$", text):
            return Problem(name, "A region looks like us-east-1 or eu-west-2.")
    elif name == "AWS_ACCESS_KEY_ID":
        if not re.match(r"^(AKIA|ASIA)[A-Z0-9]{16}$", text):
            return Problem(name, "An access key ID is 20 characters and starts with AKIA.")
    elif name == "AWS_SECRET_ACCESS_KEY":
        if not re.match(r"^[A-Za-z0-9/+=]{40}$", text):
            return Problem(name, "A secret access key is 40 characters long.")
    elif name == "FIREBASE_API_KEY":
        if not re.match(r"^AIza[0-9A-Za-z_\-]{30,}$", text):
            return Problem(name, "A Firebase API key starts with AIza.", step=3)
    return None


_FIREBASE_KEYS = {
    "apiKey": "FIREBASE_API_KEY",
    "authDomain": "FIREBASE_AUTH_DOMAIN",
    "projectId": "FIREBASE_PROJECT_ID",
    "storageBucket": "FIREBASE_STORAGE_BUCKET",
    "messagingSenderId": "FIREBASE_MESSAGING_SENDER_ID",
    "appId": "FIREBASE_APP_ID",
}


def expand_firebase(text: str) -> dict[str, str]:
    """`const firebaseConfig = { apiKey: "…", … };` → the six variables it holds."""
    found: dict[str, str] = {}
    for key, value in re.findall(r"[\"']?(\w+)[\"']?\s*:\s*[\"']([^\"']*)[\"']", text or ""):
        if key in _FIREBASE_KEYS:
            found[_FIREBASE_KEYS[key]] = value.strip()
    return found


def parse(contract: Contract, values: dict[str, object]) -> Parsed:
    """Clean what was pasted, and say what is wrong with it — no network here."""
    out = Parsed()
    given = {str(k): v for k, v in (values or {}).items()}
    if contract.paste_object:
        blob = str(given.pop("firebaseConfig", "") or "")
        if blob.strip():
            expanded = expand_firebase(blob)
            if not expanded:
                out.problems.append(Problem("firebaseConfig", "We couldn't read a firebaseConfig object there. Paste the whole { apiKey: … } block.", step=3))
                return out
            given = {**expanded, **{k: v for k, v in given.items() if str(v or "").strip()}}
    unknown = [k for k in given if contract.var(k) is None]
    for name in unknown:
        out.problems.append(Problem(name, f"{name} isn't a variable this database uses."))
    for var in contract.variables:
        raw = given.get(var.name)
        text = _trim(raw)
        if not text:
            if var.required:
                where = "firebaseConfig" if contract.paste_object else var.name
                missing = f"{var.label} is missing." if not contract.paste_object else f"The config has no {var.label.lower()}."
                out.problems.append(Problem(where, missing))
            continue
        if var.kind == "uri":
            uri, problems, notices = parse_uri(var, text, contract.database, contract.provider)
            out.problems += problems
            out.notices += notices
            if uri:
                out.values[var.name] = uri
            continue
        problem = _check_plain(var, text, contract)
        if problem:
            out.problems.append(problem)
        else:
            out.values[var.name] = text.rstrip("/") if var.kind == "url" else text
    return out


# ── showing a saved value back, without showing it ───────────────────────────
def host_of(uri: str) -> str:
    parts = _split_uri(uri or "")
    return parts[2] if parts else ""


def hint(var: Optional[Var], value: str) -> str:
    """What the page may show of a saved value: its host, the non-secret whole, or …last4."""
    if not value:
        return ""
    if var is not None and var.kind == "uri":
        parts = _split_uri(value)
        if parts:
            scheme, userinfo, host, _rest = parts
            return f"{scheme}://" + ("…@" if userinfo else "") + host
    if var is not None and not var.secret:
        return value
    return "…" + value[-4:] if len(value) > 8 else "…"


# ── testing it ───────────────────────────────────────────────────────────────
CONNECTED = "connected"
UNCHECKED = "unchecked"
FAILED = "failed"


@dataclass
class CheckResult:
    status: str
    message: str
    reason: str = ""
    #: The variable the failure is about, so the page can put it under that field.
    name: Optional[str] = None
    step: Optional[int] = None
    host: str = ""
    latency_ms: Optional[int] = None

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "message": self.message,
            "reason": self.reason,
            "name": self.name,
            "step": self.step,
            "host": self.host,
            "latency_ms": self.latency_ms,
        }


#: Which guide step fixes what, per provider — kept in step with the guides the page
#: shows (`frontend/components/build/DatabaseConnect.tsx`).
_STEPS: dict[str, dict[str, int]] = {
    "atlas": {"user": 1, "password": 3, "network": 2, "copy": 3},
    "generic": {"user": 1, "password": 2, "network": 3, "copy": 2},
    "supabase": {"copy": 1, "password": 2, "network": 2},
    "neon": {"copy": 2, "password": 2},
    "planetscale": {"password": 2, "copy": 3},
}


def _step(provider: str, what: str) -> Optional[int]:
    return _STEPS.get(provider, {}).get(what)


def _db_label(database: str) -> str:
    return {"postgres": "Postgres", "mongodb": "MongoDB", "mysql": "MySQL"}.get(database, database)


def _failure(contract: Contract, reason: str, name: str, host: str, detail: str = "") -> CheckResult:
    """A failure with its cause and its fix, in words, per provider."""
    p = contract.provider
    if reason == "auth":
        message = "The database refused the user name or password. Check both — the password is your database user's, not your account's."
        step = _step(p, "user")
    elif reason == "timeout":
        if p == "atlas":
            message = "Timed out — Atlas is probably blocking this computer's IP. Add it under Security → Network Access (0.0.0.0/0 allows everyone; use it only while developing)."
        elif p == "supabase":
            message = "Timed out. If you used the direct connection (db.<ref>.supabase.co), it only works over IPv6 — use the Session pooler string instead."
        else:
            message = "Timed out reaching the database. Its firewall or IP allowlist is probably blocking this computer."
        step = _step(p, "network")
    elif reason == "dns":
        message = f"Couldn't find {host or 'that host'}. Check the host part of the string for a typo."
        step = _step(p, "copy")
    elif reason == "refused":
        message = f"{host or 'The host'} refused the connection — nothing is listening on that port, or the database isn't running."
        step = None
    elif reason == "tls":
        fix = "?sslmode=require" if contract.database == "postgres" else "?ssl-mode=REQUIRED"
        message = f"The database requires TLS. Add {fix} to the end of the string."
        step = _step(p, "copy")
    elif reason == "ipv6":
        message = "Supabase's direct host only works over IPv6, which this network doesn't have. Use the Session pooler string (Connect → Session pooler)."
        step = _step(p, "password")
    elif reason == "key":
        message = "Supabase refused the key. Copy the publishable key again from Project Settings → API Keys."
        step = 1
    else:
        message = "The database answered with an error we don't recognise" + (f": {detail}" if detail else ".")
        step = None
    return CheckResult(FAILED, message, reason=reason, name=name, step=step, host=host)


def _classify(text: str) -> str:
    low = text.lower()
    if "network is unreachable" in low or "cannot assign requested address" in low:
        return "ipv6"
    if any(s in low for s in ("authentication failed", "bad auth", "access denied", "password authentication", "auth failed", "(1045")):
        return "auth"
    if any(s in low for s in ("ssl", "tls", "encryption")) and any(s in low for s in ("require", "off", "insecure")):
        return "tls"
    if any(s in low for s in ("name or service not known", "nodename nor servname", "getaddrinfo", "could not translate host", "dns", "no such host", "unknown host")):
        return "dns"
    if "refused" in low:
        return "refused"
    if "timed out" in low or "timeout" in low:
        return "timeout"
    return "other"


def _reach(host: str, port: int) -> tuple[Optional[str], Optional[int]]:
    """DNS, then TCP. (failure reason or None, latency ms)."""
    started = time.monotonic()
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return "dns", None
    except OSError as e:
        return _classify(str(e)), None
    last = "timeout"
    deadline = started + TIMEOUT_SECONDS
    for family, socktype, proto, _canon, addr in infos:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        try:
            with socket.socket(family, socktype, proto) as s:
                s.settimeout(left)
                s.connect(addr)
                return None, int((time.monotonic() - started) * 1000)
        except socket.timeout:
            last = "timeout"
        except ConnectionRefusedError:
            last = "refused"
        except OSError as e:
            last = "ipv6" if family == socket.AF_INET6 and "unreachable" in str(e).lower() else _classify(str(e))
    return last, None


_DEFAULT_PORT = {"postgres": 5432, "mysql": 3306, "mongodb": 27017}


def _host_port(uri: str, database: str) -> tuple[str, int]:
    host = host_of(uri).split(",")[0]
    if host.startswith("["):
        name, _, port = host[1:].partition("]")
        return name, int(port.lstrip(":") or _DEFAULT_PORT.get(database, 0))
    name, _, port = host.partition(":")
    try:
        return name, int(port) if port else _DEFAULT_PORT.get(database, 0)
    except ValueError:
        return name, _DEFAULT_PORT.get(database, 0)


def _unchecked(host: str, latency: Optional[int], why: str) -> CheckResult:
    return CheckResult(
        UNCHECKED,
        f"Saved. {why} The crew will build against it anyway.",
        reason="no_driver",
        host=host,
        latency_ms=latency,
    )


def _ping_uri(contract: Contract, name: str, uri: str) -> CheckResult:
    database = contract.database
    shown = host_of(uri)
    started = time.monotonic()
    try:
        if database == "mongodb":
            try:
                import pymongo  # type: ignore
            except ImportError:
                pymongo = None
            if pymongo is None:
                if uri.startswith("mongodb+srv://"):
                    # An SRV host has no address of its own to reach without dnspython.
                    return _unchecked(shown, None, "We couldn't test it here: the MongoDB driver isn't installed on this server.")
                return _reach_then_unchecked(contract, name, uri, "the MongoDB driver isn't installed on this server")
            client = pymongo.MongoClient(uri, serverSelectionTimeoutMS=int(TIMEOUT_SECONDS * 1000))
            try:
                client.admin.command("ping")
            finally:
                client.close()
        elif database == "postgres":
            try:
                import psycopg  # type: ignore

                conn = psycopg.connect(uri, connect_timeout=int(TIMEOUT_SECONDS))
            except ImportError:
                try:
                    import psycopg2  # type: ignore
                except ImportError:
                    return _reach_then_unchecked(contract, name, uri, "the Postgres driver isn't installed on this server")
                conn = psycopg2.connect(uri, connect_timeout=int(TIMEOUT_SECONDS))
            conn.close()
        elif database == "mysql":
            try:
                import pymysql  # type: ignore
            except ImportError:
                return _reach_then_unchecked(contract, name, uri, "the MySQL driver isn't installed on this server")
            u = urlsplit(uri)
            conn = pymysql.connect(
                host=u.hostname,
                port=u.port or 3306,
                user=unquote(u.username or ""),
                password=unquote(u.password or ""),
                database=u.path.lstrip("/") or None,
                connect_timeout=int(TIMEOUT_SECONDS),
            )
            conn.close()
        else:
            return _unchecked(shown, None, "We couldn't test this kind of database here.")
    except Exception as e:  # noqa: BLE001 - every driver has its own exception tree
        return _failure(contract, _classify(f"{type(e).__name__} {e}"), name, shown, _safe_detail(e))
    return CheckResult(
        CONNECTED,
        f"Connected to {shown}",
        reason="ok",
        host=shown,
        latency_ms=int((time.monotonic() - started) * 1000),
    )


def _reach_then_unchecked(contract: Contract, name: str, uri: str, why: str) -> CheckResult:
    host, port = _host_port(uri, contract.database)
    shown = host_of(uri)
    failed, latency = _reach(host, port) if host and port else (None, None)
    if failed:
        return _failure(contract, failed, name, shown)
    return _unchecked(
        shown,
        latency,
        f"{shown} answered, but we couldn't sign in to check the password: {why}.",
    )


def _safe_detail(e: Exception) -> str:
    from app.core.scrub import scrub

    return scrub(str(e))[:200]


def _ping_supabase(contract: Contract, values: dict[str, str]) -> CheckResult:
    import httpx

    url = values["SUPABASE_URL"].rstrip("/")
    host = urlsplit(url).hostname or url
    key = values["SUPABASE_ANON_KEY"]
    started = time.monotonic()
    try:
        r = httpx.get(
            f"{url}/rest/v1/",
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            timeout=TIMEOUT_SECONDS,
        )
    except httpx.TimeoutException:
        return _failure(contract, "timeout", "SUPABASE_URL", host)
    except httpx.HTTPError as e:
        return _failure(contract, _classify(str(e)) if _classify(str(e)) != "other" else "dns", "SUPABASE_URL", host)
    latency = int((time.monotonic() - started) * 1000)
    if r.status_code in (401, 403):
        return _failure(contract, "key", "SUPABASE_ANON_KEY", host)
    if r.status_code == 404:
        return _failure(contract, "dns", "SUPABASE_URL", host)
    if r.status_code >= 500:
        return _failure(contract, "other", "SUPABASE_URL", host, f"HTTP {r.status_code}")
    if values.get("DATABASE_URL"):
        pg = _ping_uri(contract, "DATABASE_URL", values["DATABASE_URL"])
        if pg.status == FAILED:
            return pg
    return CheckResult(CONNECTED, f"Connected to {host}", reason="ok", host=host, latency_ms=latency)


def check_connection(contract: Contract, values: dict[str, str]) -> CheckResult:
    """Try the saved values against the real database. Never raises."""
    try:
        if contract.provider == "supabase":
            return _ping_supabase(contract, values)
        uri_var = next((v for v in contract.variables if v.kind == "uri" and values.get(v.name)), None)
        if uri_var is not None:
            return _ping_uri(contract, uri_var.name, values[uri_var.name])
        if contract.database == "firestore":
            return _unchecked(values.get("FIREBASE_PROJECT_ID", ""), None, "We can't test a Firebase config from here.")
        return _unchecked("", None, "We can't test these credentials from here.")
    except Exception:  # noqa: BLE001 - a check must never take the save down with it
        log.exception("Database check failed unexpectedly for %s", contract.label)
        return _unchecked("", None, "The check itself failed.")


# ── credentials where they don't belong ──────────────────────────────────────
_CREDENTIAL_SHAPES = (
    # scheme://user:password@host — any scheme, a non-empty password.
    re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s@/]+@[^\s/]+", re.IGNORECASE),
    re.compile(r"\bsb_(secret|publishable)_[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),
)


def looks_like_credential(text: object) -> bool:
    """Whether free text a person typed holds something that should never reach a model."""
    body = str(text or "")
    if any(p.search(body) for p in _CREDENTIAL_SHAPES):
        return True
    from app.core import scrub

    return scrub.holds_known(body)
