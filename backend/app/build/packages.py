"""The dependencies the platform can put in a manifest, and how to recognise one.

The platform owns `package.json` and `requirements.txt`, so it has to know what an
import *is*: part of the language, a file in the build, or a package it can pin. A
package it cannot name a version for is an import it cannot install, and the build
that relies on it fails at `npm install` rather than here — so it is reported here,
where the agent that wrote it can still be told.

Versions are caret/compatible *ranges* on majors known to work together, never exact
pins: an exact version that was never published fails the install as surely as a
package that does not exist, and a range on a known major resolves to whatever is
current within it.
"""
from __future__ import annotations

import re
import sys
from typing import Optional

# ── npm ──────────────────────────────────────────────────────────────────────
#: Frameworks, as the scaffold installs them. Kept apart from the general list so a
#: manifest's framework pin cannot be overridden by a stray import.
NPM_FRAMEWORK: dict[str, str] = {
    "next": "^14.2.0",
    "react": "^18.3.1",
    "react-dom": "^18.3.1",
    "vite": "^5.4.0",
    "@vitejs/plugin-react": "^4.3.0",
    "vue": "^3.4.0",
    "@vitejs/plugin-vue": "^5.1.0",
    "svelte": "^4.2.0",
    "@sveltejs/vite-plugin-svelte": "^3.1.0",
    "express": "^4.21.0",
    "tailwindcss": "^3.4.0",
    "postcss": "^8.4.0",
    "autoprefixer": "^10.4.0",
    "typescript": "^5.5.0",
    "@types/react": "^18.3.0",
    "@types/react-dom": "^18.3.0",
    "@types/node": "^20.14.0",
}

#: Everything else a generated app may import. Chosen by what models actually reach
#: for; the agents are shown this list, so it is also the vocabulary they write in.
NPM: dict[str, str] = {
    # data fetching and state
    "axios": "^1.7.0",
    "swr": "^2.2.0",
    "@tanstack/react-query": "^5.51.0",
    "zustand": "^4.5.0",
    "jotai": "^2.9.0",
    "redux": "^5.0.0",
    "@reduxjs/toolkit": "^2.2.0",
    "react-redux": "^9.1.0",
    # forms and validation
    "react-hook-form": "^7.52.0",
    "@hookform/resolvers": "^3.9.0",
    "zod": "^3.23.0",
    "yup": "^1.4.0",
    "formik": "^2.4.0",
    # UI
    "clsx": "^2.1.0",
    "classnames": "^2.5.0",
    "tailwind-merge": "^2.4.0",
    "class-variance-authority": "^0.7.0",
    "lucide-react": "^0.424.0",
    "react-icons": "^5.2.0",
    "@heroicons/react": "^2.1.0",
    "@headlessui/react": "^2.1.0",
    "framer-motion": "^11.3.0",
    "react-hot-toast": "^2.4.0",
    "react-toastify": "^10.0.5",
    "sonner": "^1.5.0",
    "styled-components": "^6.1.0",
    "@mui/material": "^5.16.0",
    "@mui/icons-material": "^5.16.0",
    "@emotion/react": "^11.13.0",
    "@emotion/styled": "^11.13.0",
    "react-datepicker": "^7.3.0",
    "moment": "^2.30.1",
    "recharts": "^2.12.0",
    "chart.js": "^4.4.0",
    "react-chartjs-2": "^5.2.0",
    "react-router-dom": "^6.26.0",
    "react-markdown": "^9.0.0",
    # utilities
    "date-fns": "^3.6.0",
    "dayjs": "^1.11.0",
    "uuid": "^10.0.0",
    "nanoid": "^5.0.0",
    "lodash": "^4.17.21",
    "qs": "^6.13.0",
    "js-cookie": "^3.0.5",
    "jwt-decode": "^4.0.0",
    # auth
    "next-auth": "^4.24.0",
    "jsonwebtoken": "^9.0.0",
    "jose": "^5.6.0",
    "bcrypt": "^5.1.0",
    "bcryptjs": "^2.4.3",
    "passport": "^0.7.0",
    "passport-jwt": "^4.0.1",
    "passport-local": "^1.0.0",
    # server
    "cors": "^2.8.5",
    "helmet": "^7.1.0",
    "morgan": "^1.10.0",
    "dotenv": "^16.4.0",
    "body-parser": "^1.20.0",
    "cookie-parser": "^1.4.6",
    "express-validator": "^7.1.0",
    "express-rate-limit": "^7.4.0",
    "express-session": "^1.18.0",
    "compression": "^1.7.4",
    "multer": "^1.4.5-lts.1",
    "nodemailer": "^6.9.0",
    "socket.io": "^4.7.0",
    "socket.io-client": "^4.7.0",
    "ws": "^8.18.0",
    "joi": "^17.13.0",
    "winston": "^3.13.0",
    "pino": "^9.3.0",
    "node-cron": "^3.0.3",
    "stripe": "^16.6.0",
    # data stores
    "pg": "^8.12.0",
    "mysql2": "^3.11.0",
    "sqlite3": "^5.1.7",
    "better-sqlite3": "^11.1.0",
    "mongoose": "^8.5.0",
    "mongodb": "^6.8.0",
    "redis": "^4.7.0",
    "ioredis": "^5.4.0",
    "@prisma/client": "^5.18.0",
    "prisma": "^5.18.0",
    "sequelize": "^6.37.0",
    "knex": "^3.1.0",
    "typeorm": "^0.3.20",
    "drizzle-orm": "^0.33.0",
    # nest
    "@nestjs/common": "^10.4.0",
    "@nestjs/core": "^10.4.0",
    "@nestjs/platform-express": "^10.4.0",
    "reflect-metadata": "^0.2.2",
    "rxjs": "^7.8.1",
}

#: Test and type packages: installed as devDependencies, never shipped.
NPM_DEV: dict[str, str] = {
    "jest": "^29.7.0",
    "jest-environment-jsdom": "^29.7.0",
    "@testing-library/react": "^16.0.0",
    "@testing-library/jest-dom": "^6.4.0",
    "@testing-library/user-event": "^14.5.0",
    "@testing-library/dom": "^10.4.0",
    "supertest": "^7.0.0",
    "vitest": "^2.0.0",
    "mocha": "^10.7.0",
    "chai": "^5.1.0",
    "sinon": "^18.0.0",
    "nock": "^13.5.0",
    "msw": "^2.3.0",
    "@types/jest": "^29.5.0",
    "@types/express": "^4.17.21",
    "@types/supertest": "^6.0.0",
}

NODE_BUILTINS = frozenset(
    """assert async_hooks buffer child_process cluster console constants crypto dgram
    diagnostics_channel dns domain events fs http http2 https inspector module net os
    path perf_hooks process punycode querystring readline repl stream string_decoder
    sys timers tls trace_events tty url util v8 vm wasi worker_threads zlib test""".split()
)


def npm_package(specifier: str) -> Optional[str]:
    """`@scope/pkg/sub` -> `@scope/pkg`, `pkg/sub` -> `pkg`; None for relative paths."""
    s = (specifier or "").strip()
    if not s or s.startswith((".", "/")):
        return None
    parts = s.split("/")
    if s.startswith("@"):
        return "/".join(parts[:2]) if len(parts) > 1 else None
    return parts[0]


def is_node_builtin(specifier: str) -> bool:
    s = specifier.strip()
    if s.startswith("node:"):
        return True
    return s.split("/")[0] in NODE_BUILTINS


def npm_version(name: str) -> Optional[str]:
    """The range the platform installs `name` at, or None if it cannot provide it."""
    if name == "@next/font":
        return None
    return NPM_FRAMEWORK.get(name) or NPM.get(name) or NPM_DEV.get(name)


def is_npm_dev(name: str) -> bool:
    return name in NPM_DEV or name.startswith("@types/")


# ── pip ──────────────────────────────────────────────────────────────────────
#: import name -> (distribution name, version spec). Import and distribution names
#: differ often enough (`jwt` is PyJWT, `jose` is python-jose) that a manifest built
#: from imports alone would install the wrong thing, or nothing.
PIP: dict[str, tuple[str, str]] = {
    "fastapi": ("fastapi", ">=0.110,<1"),
    "starlette": ("starlette", ">=0.36,<1"),
    "uvicorn": ("uvicorn[standard]", ">=0.29,<1"),
    "pydantic": ("pydantic", ">=2.6,<3"),
    "pydantic_settings": ("pydantic-settings", ">=2.2,<3"),
    "email_validator": ("email-validator", ">=2.1,<3"),
    "flask": ("Flask", ">=3.0,<4"),
    "flask_sqlalchemy": ("Flask-SQLAlchemy", ">=3.1,<4"),
    "flask_cors": ("Flask-Cors", ">=4.0,<6"),
    "flask_login": ("Flask-Login", ">=0.6,<1"),
    "flask_jwt_extended": ("Flask-JWT-Extended", ">=4.6,<5"),
    "flask_migrate": ("Flask-Migrate", ">=4.0,<5"),
    "flask_wtf": ("Flask-WTF", ">=1.2,<2"),
    "flask_limiter": ("Flask-Limiter", ">=3.5,<4"),
    "flask_restful": ("Flask-RESTful", ">=0.3.10,<1"),
    "werkzeug": ("Werkzeug", ">=3.0,<4"),
    "django": ("Django", ">=5.0,<6"),
    "rest_framework": ("djangorestframework", ">=3.15,<4"),
    "corsheaders": ("django-cors-headers", ">=4.3,<5"),
    "sqlalchemy": ("SQLAlchemy", ">=2.0,<3"),
    "alembic": ("alembic", ">=1.13,<2"),
    "sqlmodel": ("sqlmodel", ">=0.0.16,<1"),
    "psycopg2": ("psycopg2-binary", ">=2.9,<3"),
    "psycopg": ("psycopg[binary]", ">=3.1,<4"),
    "asyncpg": ("asyncpg", ">=0.29,<1"),
    "pymysql": ("PyMySQL", ">=1.1,<2"),
    "aiosqlite": ("aiosqlite", ">=0.20,<1"),
    "pymongo": ("pymongo", ">=4.6,<5"),
    "bson": ("pymongo", ">=4.6,<5"),
    "motor": ("motor", ">=3.4,<4"),
    "redis": ("redis", ">=5.0,<6"),
    "jwt": ("PyJWT", ">=2.8,<3"),
    "jose": ("python-jose[cryptography]", ">=3.3,<4"),
    "passlib": ("passlib[bcrypt]", ">=1.7,<2"),
    "bcrypt": ("bcrypt", ">=4.1,<5"),
    "cryptography": ("cryptography", ">=42,<46"),
    "dotenv": ("python-dotenv", ">=1.0,<2"),
    "multipart": ("python-multipart", ">=0.0.9,<1"),
    "httpx": ("httpx", ">=0.27,<1"),
    "requests": ("requests", ">=2.31,<3"),
    "aiohttp": ("aiohttp", ">=3.9,<4"),
    "celery": ("celery", ">=5.3,<6"),
    "stripe": ("stripe", ">=9,<12"),
    "boto3": ("boto3", ">=1.34,<2"),
    "jinja2": ("Jinja2", ">=3.1,<4"),
    "marshmallow": ("marshmallow", ">=3.21,<4"),
    "dateutil": ("python-dateutil", ">=2.9,<3"),
    "pytz": ("pytz", ">=2024.1"),
    "yaml": ("PyYAML", ">=6.0,<7"),
    "slugify": ("python-slugify", ">=8.0,<9"),
    "shortuuid": ("shortuuid", ">=1.0,<2"),
    "gunicorn": ("gunicorn", ">=22,<24"),
    "websockets": ("websockets", ">=12,<14"),
    "apscheduler": ("APScheduler", ">=3.10,<4"),
    "slowapi": ("slowapi", ">=0.1.9,<1"),
    "pytest": ("pytest", ">=8.0,<9"),
    "pytest_asyncio": ("pytest-asyncio", ">=0.23,<1"),
    "faker": ("Faker", ">=25,<30"),
    "mongomock": ("mongomock", ">=4.1,<5"),
    "fakeredis": ("fakeredis", ">=2.23,<3"),
}

#: Distribution names that only tests need. Listed separately in the manifest.
PIP_DEV = frozenset({"pytest", "pytest-asyncio", "Faker", "mongomock", "fakeredis"})

#: The standard library, for the interpreter this checks *generated* code for — not
#: the one this runs on, which may be older than the code it is reading.
_STDLIB = frozenset(
    """__future__ _thread abc aifc argparse array ast asynchat asyncio asyncore atexit audioop
    base64 bdb binascii bisect builtins bz2 calendar cgi cgitb chunk cmath cmd code codecs
    codeop collections colorsys compileall concurrent configparser contextlib contextvars copy
    copyreg cProfile crypt csv ctypes curses dataclasses datetime dbm decimal difflib dis
    doctest email encodings ensurepip enum errno faulthandler fcntl filecmp fileinput fnmatch
    fractions ftplib functools gc getopt getpass gettext glob graphlib grp gzip hashlib heapq
    hmac html http idlelib imaplib imghdr imp importlib inspect io ipaddress itertools json
    keyword lib2to3 linecache locale logging lzma mailbox mailcap marshal math mimetypes mmap
    modulefinder msilib msvcrt multiprocessing netrc nntplib numbers operator optparse os
    ossaudiodev pathlib pdb pickle pickletools pipes pkgutil platform plistlib poplib posix
    posixpath pprint profile pstats pty pwd py_compile pyclbr pydoc queue quopri random re
    readline reprlib resource rlcompleter runpy sched secrets select selectors shelve shlex
    shutil signal site smtpd smtplib sndhdr socket socketserver spwd sqlite3 ssl stat
    statistics string stringprep struct subprocess sunau symtable sys sysconfig syslog
    tabnanny tarfile telnetlib tempfile termios textwrap threading time timeit tkinter token
    tokenize tomllib trace traceback tracemalloc tty turtle types typing typing_extensions
    unicodedata unittest urllib uu uuid venv warnings wave weakref webbrowser winreg
    winsound wsgiref xdrlib xml xmlrpc zipapp zipfile zipimport zlib zoneinfo""".split()
)


def is_stdlib(module: str) -> bool:
    top = (module or "").split(".")[0]
    names = getattr(sys, "stdlib_module_names", None)
    return top in _STDLIB or bool(names and top in names)


def pip_requirement(import_name: str) -> Optional[tuple[str, str]]:
    """(distribution, spec) for a top-level import name, or None if unknown."""
    return PIP.get((import_name or "").split(".")[0])


# ── what agents are told ─────────────────────────────────────────────────────
def npm_vocabulary(include_server: bool, include_tests: bool) -> str:
    names = sorted(
        n for n in {**NPM_FRAMEWORK, **NPM}
        if include_server or n not in _SERVER_ONLY
    )
    if include_tests:
        names += sorted(NPM_DEV)
    return ", ".join(names)


def pip_vocabulary(include_tests: bool) -> str:
    names = sorted(
        {dist.split("[")[0] + f" (import {imp})" if dist.split("[")[0].lower() != imp else imp
         for imp, (dist, _spec) in PIP.items()
         if include_tests or dist not in PIP_DEV}
    )
    return ", ".join(names)


_SERVER_ONLY = frozenset(
    {
        "express", "cors", "helmet", "morgan", "body-parser", "cookie-parser",
        "express-validator", "express-rate-limit", "express-session", "compression", "multer",
        "nodemailer", "socket.io", "ws", "winston", "pino", "node-cron", "pg", "mysql2",
        "sqlite3", "better-sqlite3", "mongoose", "mongodb", "redis", "ioredis", "prisma",
        "sequelize", "knex", "typeorm", "drizzle-orm", "@nestjs/common", "@nestjs/core",
        "@nestjs/platform-express", "reflect-metadata", "rxjs", "passport", "passport-jwt",
        "passport-local", "bcrypt", "jsonwebtoken", "stripe", "joi",
    }
)

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def is_identifier(name: str) -> bool:
    return bool(_IDENT.match(name or ""))
