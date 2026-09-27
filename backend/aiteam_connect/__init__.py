"""aiteam-connect: lets the AI Software Engineering Team website use the models on
your own computer, without opening a single port on it.

It dials out to the server over a WebSocket and answers four typed questions —
`hello`, `list_models`, `model_info`, `ping` — using the same runtime adapters the
server uses. It never downloads a model, runs a command, writes a file, or calls
an address the server names. See `local.py` for what it will and won't do.
"""
from app.connector.protocol import CONNECTOR_VERSION as __version__  # noqa: F401
