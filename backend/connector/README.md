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
pipx run --spec aiteam-connect==0.1.0 aiteam-connect --server https://your-server
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

It answers `hello`, `list_models`, `model_info` and `ping`, and nothing else. It
never downloads, deletes or loads a model, never runs a command or writes a file
for the server, and never calls a URL or path the server sends. Every refusal is
logged in `~/.aiteam-connect/connector.log`. It listens on no port:
`lsof -iTCP -sTCP:LISTEN` shows nothing from it.

Its private key stays on this computer — in the OS keychain with
`pipx run --spec "aiteam-connect[keychain]==0.1.0" …`, otherwise in a file only you
can read, with a warning each time it starts.

## Verify the package

Releases are published only from this repository's GitHub Actions, through PyPI
Trusted Publishing, with attestations:

```
pip download --no-deps aiteam-connect==0.1.0
pipx run pypi-attestations verify pypi \
  --repository https://github.com/Sravan466/ai-software-engineering-team \
  pypi:aiteam_connect-0.1.0-py3-none-any.whl
```
