# aiteam-connect

Lets the AI Software Engineering Team website use the models running on **your**
computer — Ollama, LM Studio, llama.cpp, vLLM, or any OpenAI-compatible server —
without opening a port on it.

```
┌──────────── your computer ──────────────┐                     ┌─ the server ─┐
│ runtime ◄──► adapter ◄──► connector ────┼── wss, outbound ───►│   Backend    │
└─────────────────────────────────────────┘                     └──────────────┘
```

## Use

Copy the pinned command from the website's **Setup** tab. It looks like:

```
pipx run --spec aiteam-connect==0.2.0 aiteam-connect --server https://your-server
```

It asks for the pairing code the website shows (never on the command line, where
other programs could read it), tells you which account and server it's about to
connect to, and waits for you to approve the computer on the website.

A runtime that detection doesn't find, or one with an API key:

```
aiteam-connect add-source http://127.0.0.1:5000 --api-key
```

An address that isn't this computer has to be confirmed here, by typing `yes`.
The website can never add one.

## What it will and won't do

It answers `hello`, `list_models`, `model_info`, `ping`, `chat`, `embed` and
`cancel`, and nothing else. It never downloads, deletes or loads a model, never runs
a command or writes a file for the server, and never calls a URL or path the server
sends. A model's answer goes back to the server as data; nothing here acts on it.
Every refusal is logged in `~/.aiteam-connect/connector.log`. It listens on no
port: `lsof -iTCP -sTCP:LISTEN` shows nothing from it.

While a build runs, the terminal says what it's answering ("Answering Backend
Engineer for build 'Todo app' with qwen2.5:7b…"), and `~/.aiteam-connect/activity.log`
keeps the time, operation, model and token counts of every call — never the prompt
or the answer.

## Limits, pause and quit

This computer decides how much of it a build may use. The server can't change any
of this:

```
aiteam-connect limits                               # show them
aiteam-connect limits --concurrency 1 --requests-per-minute 60 \
  --max-prompt-chars 600000 --max-output-tokens 16384 --timeout-seconds 900
aiteam-connect limits --gpu-layers 20 --threads 8 --keep-alive 5m
```

A call over a limit is refused, and the website shows which limit and this command.
A request for more output tokens than allowed is clamped. GPU layers, threads and
keep-alive are read only from here: a request from the server that names one is
refused.

`aiteam-connect pause` refuses model calls until `aiteam-connect resume`; a build
using this computer waits meanwhile. Ctrl+C quits. Closing the connector — or the
laptop going to sleep — pauses the build, and it carries on from the last finished
phase when the connector is back. It reconnects by itself after a Wi-Fi drop.

Its private key stays on this computer — in the OS keychain with
`pipx run --spec "aiteam-connect[keychain]==0.2.0" …`, otherwise in a file only you
can read, with a warning each time it starts.

## Verify the package

Releases are published only from this repository's GitHub Actions, through PyPI
Trusted Publishing, with attestations:

```
pip download --no-deps aiteam-connect==0.2.0
pipx run pypi-attestations verify pypi \
  --repository https://github.com/Sravan466/ai-software-engineering-team \
  pypi:aiteam_connect-0.2.0-py3-none-any.whl
```
