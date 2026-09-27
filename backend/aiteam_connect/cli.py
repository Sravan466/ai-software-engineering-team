"""`aiteam-connect` — pair this computer, then keep it connected.

    aiteam-connect --server https://your-server           pair (first time) and connect
    aiteam-connect add-source http://127.0.0.1:5000        use a runtime detection misses
    aiteam-connect remove-source http://127.0.0.1:5000
    aiteam-connect status
    aiteam-connect forget --server https://your-server    delete this computer's key

The pairing code is read from a prompt, never from the command line: arguments are
visible to every other process on this computer (CWE-214).
"""
from __future__ import annotations

import argparse
import getpass
import sys

from app.router.runtimes import detect
from aiteam_connect import store
from aiteam_connect.client import ConnectError, credential, forget, pair, run, server_for


def _say(text: str) -> None:
    print(text, flush=True)


def _server_arg(p: argparse.ArgumentParser, top: bool = False) -> None:
    # Suppressed below the top level, so `--server X connect` isn't reset by the
    # subcommand's own default.
    p.add_argument(
        "--server",
        default=None if top else argparse.SUPPRESS,
        help="The website's server, e.g. https://example.com",
    )


def _remembered_server() -> str | None:
    servers = list((store.load_state().get("servers") or {}).keys())
    return servers[0] if len(servers) == 1 else None


def cmd_connect(args) -> int:
    url = args.server or _remembered_server()
    if not url:
        raise ConnectError("Pass --server — the website's Setup tab shows the exact command.")
    server = server_for(url)
    if not server.origin.startswith("https://"):
        _say(f"⚠ {server.origin} isn't encrypted. That's only allowed because it's this computer.")
    if credential(server) is None:
        pair(server, input, _say)
    return run(server, _say)


def cmd_add_source(args) -> int:
    url = args.url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ConnectError("Give the address as http://127.0.0.1:<port>.")
    entry = {"url": url, "runtime": args.runtime}
    if not detect.is_loopback(url):
        _say(f"⚠ {url} isn't this computer. The website's requests would reach it through this connector,")
        _say("  and anyone on the network between here and there could read them.")
        if input("Type 'yes' to use it anyway: ").strip().lower() != "yes":
            raise ConnectError("Not added.")
        entry["confirmed_remote"] = True
    if args.api_key:
        key = getpass.getpass("API key for this server (not shown): ").strip()
        if key:
            store.put_secret(store.source_key_name(url), key)
    state = store.load_state()
    sources = [s for s in state.get("sources") or [] if s.get("url") != url]
    sources.append(entry)
    state["sources"] = sources
    store.save_state(state)
    _say(f"Added {url}. It's used the next time the connector describes this computer.")
    return 0


def cmd_remove_source(args) -> int:
    url = args.url.strip().rstrip("/")
    state = store.load_state()
    state["sources"] = [s for s in state.get("sources") or [] if s.get("url") != url]
    store.save_state(state)
    store.delete_secret(store.source_key_name(url))
    _say(f"Removed {url}.")
    return 0


def cmd_status(args) -> int:
    state = store.load_state()
    servers = state.get("servers") or {}
    if not servers:
        _say("Not paired with any server.")
    for origin, entry in servers.items():
        _say(f"Paired with {origin} as {entry.get('account')} (key in {entry.get('key_store')}).")
    for source in state.get("sources") or []:
        _say(f"Added source: {source.get('url')}{' (remote, confirmed)' if source.get('confirmed_remote') else ''}")
    return 0


def cmd_forget(args) -> int:
    url = args.server or _remembered_server()
    if not url:
        raise ConnectError("Pass --server to say which pairing to forget.")
    if forget(server_for(url)):
        _say("Forgotten on this computer. Also press Forget on the website's My computers list.")
    else:
        _say("This computer wasn't paired with that server.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aiteam-connect", description=__doc__.split("\n")[0])
    _server_arg(parser, top=True)
    sub = parser.add_subparsers(dest="command")
    _server_arg(sub.add_parser("connect", help="pair if needed, then stay connected"))
    add = sub.add_parser("add-source", help="use a runtime at an address you choose")
    add.add_argument("url")
    add.add_argument("--runtime", help="what it is, if detection can't tell (e.g. vllm)")
    add.add_argument("--api-key", action="store_true", help="prompt for the runtime's API key")
    rm = sub.add_parser("remove-source", help="stop using an address you added")
    rm.add_argument("url")
    sub.add_parser("status", help="what this computer is paired with")
    _server_arg(sub.add_parser("forget", help="delete this computer's key for a server"))
    args = parser.parse_args(argv)
    handler = {
        None: cmd_connect,
        "connect": cmd_connect,
        "add-source": cmd_add_source,
        "remove-source": cmd_remove_source,
        "status": cmd_status,
        "forget": cmd_forget,
    }[args.command]
    try:
        return handler(args)
    except ConnectError as e:
        _say(str(e))
        return 1
    except (KeyboardInterrupt, EOFError):
        _say("\nStopped.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
