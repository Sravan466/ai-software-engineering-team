---
name: secure-secrets
title: Handling secrets and credentials
description: Use whenever code or configuration touches API keys, passwords, tokens or connection strings.
agents: [backend_engineer, devops_engineer, security_engineer]
keywords: [secret, credential, api key, password, token, env, environment variable, encryption, vault, auth, authentication, login, payment]
---

No secret is ever a literal in source. Not a default, not a fallback, not in a comment,
not in a test fixture, not in a docker-compose file that ships. Anything committed once
is compromised and has to be rotated, not deleted.

Read secrets from the environment at startup, through one settings object, and fail
loudly at boot when a required one is missing. A service that starts without its
signing key and discovers that on the first request fails somewhere much worse.

Ship a `.env.example` with every key listed and every value blank, and keep the real
`.env` out of version control. The example is how the next person knows what to set;
the ignore rule is what stops them committing it.

Store passwords with a slow, salted hash built for the job — argon2 or bcrypt — never a
general-purpose digest, and never encryption you can reverse. Compare tokens and
signatures with a constant-time function.

Keep secrets out of anything that gets written down: no keys in URLs or query strings,
no credentials in log lines, no tokens in error payloads, and a redacting step in the
logger for the field names you know carry them.

Give each environment its own credentials, and each service the narrowest rights it can
do its job with. Production keys on a developer laptop make every laptop a production
system.

Make rotation possible before it is needed: nothing hard-coded to one key, and both the
old and new value accepted during a changeover. A credential that cannot be rotated
without downtime will not be rotated.
