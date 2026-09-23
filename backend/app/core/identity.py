"""Who a piece of work is being done for.

Every request is signed in, and everything it touches belongs to that account: its
projects, its keys, its model sources, its choice of model for each agent. Most of
that is checked where it is read — a route asks for the signed-in user and filters
by it. What cannot be passed down that way is the model router: an agent eight
calls deep asks `router.complete(...)` and has no user in hand, and threading one
through every agent, the debate, the mockup builder and the embedding function
would be a user argument on dozens of signatures that only ever pass it along.

So the account is carried in a context variable. The auth middleware binds it for
a request; a background run binds its project's owner before it does any work
(`acting_as`), and a thread started from either copies the binding (`carry`).
Code that needs an account and finds none raises `NoAccount` rather than falling
back to anyone's — a missing binding is a bug, and the wrong user's keys paying for
a build is not an acceptable way for it to show.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Callable, Iterator, Optional, TypeVar

T = TypeVar("T")

_current: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("account", default=None)


class NoAccount(RuntimeError):
    """Work that needs an account ran with none bound to it."""


def current_user_id() -> Optional[str]:
    """The account this work is being done for, or None outside of any."""
    return _current.get()


def require_user_id() -> str:
    uid = _current.get()
    if not uid:
        raise NoAccount(
            "No account is attached to this work, so there is no one's models or keys to "
            "use. This is a bug in the platform, not in the build."
        )
    return uid


def bind(user_id: Optional[str]) -> contextvars.Token:
    """Bind `user_id` for the rest of this context. Undo with `unbind(token)`."""
    return _current.set(user_id)


def unbind(token: contextvars.Token) -> None:
    _current.reset(token)


@contextmanager
def acting_as(user_id: Optional[str]) -> Iterator[None]:
    """Do the enclosed work for `user_id` — a project's owner, in a background run."""
    token = _current.set(user_id)
    try:
        yield
    finally:
        _current.reset(token)


def carry(fn: Callable[..., T]) -> Callable[..., T]:
    """`fn`, run in a copy of the caller's context — for a thread or a pool worker.

    A new thread starts with an empty context, so a mockup section drawn in a pool
    would otherwise have no account at all. The copy is taken now, once per call to
    `carry`: one context cannot be entered by two threads at the same time, so every
    submission to a pool needs its own.
    """
    ctx = contextvars.copy_context()
    return lambda *args, **kwargs: ctx.run(fn, *args, **kwargs)
