"use client";

import { Suspense, useEffect, useId, useRef, useState, type FormEvent } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import "@/components/agents/agents.css";
import "@/components/auth/auth.css";
import AgentSprite from "@/components/agents/AgentSprite";
import { AGENTS } from "@/components/agents/personas";
import { Icon } from "@/components/shell/icons";
import { api, ApiError, type AuthStatus } from "@/lib/api";

type Mode = "signin" | "setup" | "signup";

const MIN_PASSWORD = 10;

/**
 * Only ever go back to a page of this app — never an address someone put in the link.
 * Resolved the way the browser will resolve it, and kept only if it stays here:
 * `/\evil.com` looks like a path and is another site.
 */
function safeNext(raw: string | null): string {
  if (typeof window === "undefined") return "/";
  if (!raw || !raw.startsWith("/") || /[\\\u0000-\u001f]/.test(raw)) return "/";
  try {
    const here = window.location.origin;
    const url = new URL(raw, here);
    if (url.origin !== here || url.pathname.startsWith("/signin")) return "/";
    return url.pathname + url.search + url.hash;
  } catch {
    return "/";
  }
}

/**
 * The door to the workspace. One screen, three jobs, decided by the backend:
 *
 *   • set up — no account can sign in yet, so the first one made owns the install
 *     (and, on an install from before accounts, every build already on it);
 *   • sign in — the usual case;
 *   • create an account — only when the install takes new ones.
 *
 * The crew stands behind the form, waiting for their shift to start. They are the
 * product's one piece of character, so the door is where they are met.
 */
function SignIn() {
  const router = useRouter();
  const next = safeNext(useSearchParams().get("next"));
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [unreachable, setUnreachable] = useState(false);
  const [mode, setMode] = useState<Mode>("signin");

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [token, setToken] = useState("");
  const [reveal, setReveal] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<{ email?: string; password?: string }>({});
  const errorRef = useRef<HTMLDivElement>(null);

  const ids = {
    email: useId(),
    password: useId(),
    name: useId(),
    token: useId(),
    emailErr: useId(),
    passwordErr: useId(),
    passwordHint: useId(),
    title: useId(),
  };

  useEffect(() => {
    let live = true;
    api
      .authStatus()
      .then((s) => {
        if (!live) return;
        if (s.user) {
          router.replace(next);
          return;
        }
        setStatus(s);
        setMode(s.needs_setup ? "setup" : "signin");
      })
      .catch(() => live && setUnreachable(true));
    return () => {
      live = false;
    };
  }, [next, router]);

  // A failed submit moves focus to what went wrong, so it is heard, not just seen.
  useEffect(() => {
    if (error) errorRef.current?.focus();
  }, [error]);

  const creating = mode !== "signin";

  const validate = () => {
    const found: { email?: string; password?: string } = {};
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email.trim())) found.email = "Enter an email address like name@example.com.";
    if (!password) found.password = "Enter your password.";
    else if (creating && password.length < MIN_PASSWORD)
      found.password = `Use at least ${MIN_PASSWORD} characters.`;
    setFieldErrors(found);
    return Object.keys(found).length === 0;
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    if (!validate()) return;
    setBusy(true);
    try {
      if (creating) {
        await api.signUp({
          email: email.trim(),
          password,
          display_name: name.trim() || undefined,
          setup_token: mode === "setup" && token.trim() ? token.trim() : undefined,
        });
      } else {
        await api.signIn({ email: email.trim(), password });
      }
      router.replace(next);
    } catch (err) {
      setBusy(false);
      if (err instanceof ApiError) setError(err.message);
      else setError("The backend isn't answering. Check it's running on :8000, then try again.");
    }
  };

  const switchTo = (m: Mode) => {
    setMode(m);
    setError(null);
    setFieldErrors({});
  };

  const heading =
    mode === "setup" ? "Set up this install" : mode === "signup" ? "Create your account" : "Sign in";
  const lede =
    mode === "setup"
      ? status?.has_unclaimed_work
        ? "You'll own this install. The builds, keys and model choices already on it move into your account."
        : "The first account owns this install: its keys, its runtimes and the shared skill library."
      : mode === "signup"
        ? "Your builds, keys and model choices are yours alone. Nobody else on this install sees them."
        : "Your builds and settings are waiting where you left them.";

  return (
    <div className="door">
      <section className="door-crew" aria-hidden="true">
        <p className="door-shout">
          The crew is <em>on standby</em>
        </p>
        <div className="door-floor">
          {AGENTS.map((a, i) => (
            <span className="door-agent" key={a.key} style={{ ["--i" as string]: i }}>
              <AgentSprite agent={a} size={56} state="queued" ground />
              <span className="door-codename" style={{ color: a.accent }}>
                {a.codename}
              </span>
            </span>
          ))}
        </div>
      </section>

      <main className="door-panel" id="main">
        <div className="door-brand">
          <span className="logo-mark" aria-hidden="true">
            AI
          </span>
          <span className="wordmark">
            SWE&nbsp;<span>Team</span>
          </span>
        </div>

        {unreachable ? (
          <div className="notice notice-bad" role="alert">
            {Icon.alert}
            <div className="notice-body">
              <span className="notice-title">The backend isn&apos;t answering</span>
              <span className="notice-text">
                Nothing answered at the API address. Start the backend on :8000, then reload this page.
              </span>
              <div className="notice-actions">
                <button className="btn btn-sm" onClick={() => window.location.reload()}>
                  {Icon.refresh} Try again
                </button>
              </div>
            </div>
          </div>
        ) : status === null ? (
          <div className="door-wait" role="status">
            <span className="btn-spinner" aria-hidden="true" /> Checking this install…
          </div>
        ) : (
          <form className="door-form" onSubmit={submit} noValidate aria-labelledby={ids.title}>
            <div className="door-head">
              <h1 id={ids.title}>{heading}</h1>
              <p className="door-lede">{lede}</p>
            </div>

            {error && (
              <div className="notice notice-bad door-error" role="alert" tabIndex={-1} ref={errorRef}>
                {Icon.alert}
                <div className="notice-body">
                  <span className="notice-text">{error}</span>
                </div>
              </div>
            )}

            {mode !== "signin" && (
              <div className="field">
                <label htmlFor={ids.name}>
                  Your name <span className="door-optional">optional</span>
                </label>
                <input
                  id={ids.name}
                  className="input"
                  autoComplete="name"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  maxLength={120}
                />
              </div>
            )}

            <div className="field">
              <label htmlFor={ids.email}>Email</label>
              <input
                id={ids.email}
                className="input"
                type="email"
                inputMode="email"
                autoComplete={creating ? "email" : "username"}
                autoCapitalize="none"
                spellCheck={false}
                autoFocus
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                aria-invalid={fieldErrors.email ? true : undefined}
                aria-describedby={fieldErrors.email ? ids.emailErr : undefined}
              />
              {fieldErrors.email && (
                <span className="door-field-error" id={ids.emailErr}>
                  {fieldErrors.email}
                </span>
              )}
            </div>

            <div className="field">
              <label htmlFor={ids.password}>Password</label>
              <div className="door-secret">
                <input
                  id={ids.password}
                  className="input"
                  type={reveal ? "text" : "password"}
                  autoComplete={creating ? "new-password" : "current-password"}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  aria-invalid={fieldErrors.password ? true : undefined}
                  aria-describedby={
                    [fieldErrors.password ? ids.passwordErr : "", creating ? ids.passwordHint : ""]
                      .filter(Boolean)
                      .join(" ") || undefined
                  }
                />
                <button
                  type="button"
                  className="door-reveal"
                  onClick={() => setReveal((r) => !r)}
                  aria-label={reveal ? "Hide password" : "Show password"}
                  aria-pressed={reveal}
                >
                  {reveal ? Icon.eyeOff : Icon.eye}
                </button>
              </div>
              {fieldErrors.password && (
                <span className="door-field-error" id={ids.passwordErr}>
                  {fieldErrors.password}
                </span>
              )}
              {creating && (
                <span className="field-hint" id={ids.passwordHint}>
                  At least {MIN_PASSWORD} characters. A phrase is easier to remember than symbols.
                </span>
              )}
            </div>

            {mode === "setup" && status.setup_needs_token && (
              <div className="field">
                <label htmlFor={ids.token}>Setup token</label>
                <input
                  id={ids.token}
                  className="input input-mono"
                  autoComplete="off"
                  spellCheck={false}
                  value={token}
                  onChange={(e) => setToken(e.target.value)}
                />
                <span className="field-hint">
                  You&apos;re not on the machine this backend runs on, so setting it up needs a
                  setup token: SETUP_TOKEN from its configuration, or the one-time token the
                  backend printed in its log when it started.
                </span>
              </div>
            )}

            <button className="btn btn-primary btn-lg door-submit" type="submit" disabled={busy}>
              {busy && <span className="btn-spinner" aria-hidden="true" />}
              {mode === "setup" ? "Set up and sign in" : mode === "signup" ? "Create account" : "Sign in"}
              {!busy && Icon.arrowRight}
            </button>

            {mode === "signin" && status.signup_open && (
              <p className="door-switch">
                New here?{" "}
                <button type="button" className="door-link" onClick={() => switchTo("signup")}>
                  Create an account
                </button>
              </p>
            )}
            {mode === "signup" && (
              <p className="door-switch">
                Already have one?{" "}
                <button type="button" className="door-link" onClick={() => switchTo("signin")}>
                  Sign in
                </button>
              </p>
            )}
            {mode === "signin" && !status.signup_open && (
              <p className="door-switch door-quiet">
                No account? This install isn&apos;t taking new ones. Its owner can open sign-ups.
              </p>
            )}
          </form>
        )}
      </main>
    </div>
  );
}

export default function SignInPage() {
  return (
    <Suspense fallback={null}>
      <SignIn />
    </Suspense>
  );
}
