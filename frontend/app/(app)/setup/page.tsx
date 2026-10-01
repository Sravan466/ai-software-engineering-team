"use client";

import Link from "next/link";

import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type Device, type OS, type RuntimeCard, type SetupGuide } from "@/lib/api";
import { useChrome } from "@/components/shell/ShellChrome";
import { Icon } from "@/components/shell/icons";
import { SkeletonLines } from "@/components/ui/Skeleton";
import { CopyLine, OSPicker, OS_LABEL, Rich, Step, bytes, guessOS } from "@/components/setup/parts";
import Pairing from "@/components/setup/Pairing";
import Detected from "@/components/setup/Detected";
import Computers from "@/components/setup/Computers";
import Troubleshooting from "@/components/setup/Troubleshooting";
import TestIt from "@/components/setup/TestIt";

type Tab = "setup" | "computers";

const STRUCTURED_LABEL: Record<string, string> = {
  schema: "Held to a JSON schema",
  grammar: "Held to a grammar",
  json: "Valid JSON — checked and repaired",
  none: "Free text — checked and repaired",
};

/**
 * Setup: get a model running on your own computer and connect it, in six steps,
 * and the list of computers already connected.
 *
 * Read-only toward the computer: nothing here can tell it where to connect or what
 * to run. The runtime cards are the backend's adapter table, not text in this file.
 */
export default function SetupPage() {
  useChrome({ sub: "Setup" }, []);
  const [tab, setTab] = useState<Tab>("setup");
  const [guide, setGuide] = useState<SetupGuide | null>(null);
  const [guideError, setGuideError] = useState("");
  const [devices, setDevices] = useState<Device[] | null>(null);
  const [os, setOS] = useState<OS>("macos");
  const [runtimeId, setRuntimeId] = useState<string | null>(null);
  const [open, setOpen] = useState<Set<number> | null>(null);
  /** The last test prompt's outcome, this visit: what marks step 6 done. */
  const [tested, setTested] = useState<{ ok: boolean; seconds: number } | null>(null);

  useEffect(() => {
    setOS(guessOS());
    const fromHash = () => setTab(window.location.hash === "#computers" ? "computers" : "setup");
    fromHash();
    window.addEventListener("hashchange", fromHash);
    return () => window.removeEventListener("hashchange", fromHash);
  }, []);

  const loadDevices = useCallback(async () => {
    try {
      setDevices((await api.listDevices()).devices);
    } catch {
      setDevices((d) => d ?? []);
    }
  }, []);

  useEffect(() => {
    api.setupGuide().then(setGuide).catch((e) => setGuideError(e?.message || "Couldn't load the setup guide."));
    loadDevices();
    // Presence and what each computer reports change on their own; keep them true.
    const t = setInterval(loadDevices, 5000);
    return () => clearInterval(t);
  }, [loadDevices]);

  const replace = useCallback((d: Device) => {
    setDevices((list) => (list ?? []).map((x) => (x.id === d.id ? d : x)));
  }, []);

  const approved = (devices ?? []).filter((d) => d.status === "approved");
  const reporting = approved.find((d) => d.online && d.hello) ?? approved.find((d) => d.hello) ?? null;
  const reportedSources = reporting?.hello?.sources.filter((s) => s.reachable) ?? [];

  // Preselect what the computer already runs, else the first card.
  const runtime: RuntimeCard | null = useMemo(() => {
    const cards = guide?.runtimes ?? [];
    const id = runtimeId ?? reportedSources[0]?.runtime ?? cards[0]?.id;
    return cards.find((c) => c.id === id) ?? cards.find((c) => c.generic) ?? cards[0] ?? null;
  }, [guide, runtimeId, reportedSources]);

  const done = {
    1: runtimeId !== null || reportedSources.length > 0,
    2: reportedSources.some((s) => s.models.some((m) => m.kind !== "embedding" && m.is_local)),
    3: reportedSources.length > 0,
    4: approved.length > 0,
    5: approved.some((d) => d.online && d.chat_model),
    6: tested?.ok === true,
  } as Record<number, boolean>;
  const current = [1, 2, 3, 4, 5, 6].find((n) => !done[n]) ?? 6;

  // Open the first step that still needs doing, once the facts are in.
  useEffect(() => {
    if (open === null && devices !== null && guide !== null) setOpen(new Set([current]));
  }, [open, devices, guide, current]);

  const toggle = (n: number) =>
    setOpen((s) => {
      const next = new Set(s ?? []);
      next.has(n) ? next.delete(n) : next.add(n);
      return next;
    });
  const isOpen = (n: number) => open?.has(n) ?? false;
  const state = (n: number) => (done[n] ? "done" : n === current ? "current" : "todo");

  function goTab(next: Tab) {
    setTab(next);
    history.replaceState(null, "", next === "computers" ? "#computers" : "#setup");
  }

  // What the computer already has, so step 2 never asks for a download it doesn't need.
  const ownModels = reportedSources.flatMap((src) =>
    src.models
      .filter((m) => m.kind !== "embedding" && m.is_local)
      .map((m) => ({ ...m, spec: `${src.id}:${m.name}`, runtime: src.label })),
  );
  const haveModels = ownModels.length > 0;

  const installFor = runtime?.install[os];
  const supported = runtime ? (Object.keys(runtime.install) as OS[]) : undefined;

  const download = runtime && (
    <>
      <p className="su-p">
        <Rich text={runtime.download} />
        {runtime.library && (
          <>
            {" "}
            <a className="link" href={runtime.library} target="_blank" rel="noreferrer">
              Browse models {Icon.external}
            </a>
          </>
        )}
      </p>
      {reporting?.advice ? (
        <dl className="su-caps su-advice">
          <div>
            <dt>This computer’s memory</dt>
            <dd className="mono">{reporting.advice.ram_gib} GB</dd>
          </div>
          <div>
            <dt>Model size that fits</dt>
            <dd>{reporting.advice.size}</dd>
          </div>
          <div>
            <dt>Quantization</dt>
            <dd className="mono">{reporting.advice.quantization}</dd>
          </div>
        </dl>
      ) : (
        <p className="su-fine">Once your computer is connected (step 4), this says what size of model fits its memory.</p>
      )}
      {reporting?.advice && <p className="su-fine">{reporting.advice.note}</p>}
      <h3 className="su-h3">Embeddings</h3>
      <p className="su-p">
        <Rich text={runtime.embeddings} />
      </p>
    </>
  );

  return (
    <div className="settings-wrap su-wrap">
      <h1 style={{ fontSize: "var(--t-2xl)" }}>Set up your computer</h1>
      <p className="prose-lede" style={{ marginTop: 10 }}>
        Run the crew on a model on your own computer. The model runs on your computer. Your project and its
        files are stored on our server.
      </p>
      <p className="field-hint" style={{ marginTop: 8, maxWidth: "70ch" }}>
        &ldquo;The connector&rdquo; here is the small program that lends this app your computer&apos;s models.
        Services your generated apps use — Stripe, Resend, Clerk, OpenAI — are on the{" "}
        <Link className="link" href="/connectors">
          Connectors
        </Link>{" "}
        page instead.
      </p>

      <div className="tabs su-tabs" role="tablist" aria-label="Setup">
        <button role="tab" className="tab" aria-selected={tab === "setup"} onClick={() => goTab("setup")}>
          Setup
        </button>
        <button role="tab" className="tab" aria-selected={tab === "computers"} onClick={() => goTab("computers")}>
          My computers
          {devices && devices.length > 0 && <span className="su-tab-count">{devices.length}</span>}
        </button>
      </div>

      {/* Both panels stay mounted, so switching tabs never throws away a pairing
          code that is still counting down. */}
      <div role="tabpanel" aria-label="My computers" className="su-panel" hidden={tab !== "computers"}>
          <Computers
            devices={devices}
            onChanged={replace}
            onRemoved={(id) => setDevices((list) => (list ?? []).filter((d) => d.id !== id))}
            onPair={() => {
              goTab("setup");
              setOpen(new Set([4]));
            }}
          />
      </div>
      <div role="tabpanel" aria-label="Setup steps" className="su-panel" hidden={tab !== "setup"}>
          {guideError && (
            <div className="notice notice-bad" role="alert">
              <span className="notice-body">
                <span className="notice-title">The setup guide didn’t load</span>
                <span className="notice-text">{guideError}</span>
              </span>
            </div>
          )}
          {!guide && !guideError ? (
            <SkeletonLines lines={5} />
          ) : guide && runtime ? (
            <ol className="su-steps">
              <Step n={1} title="Pick a runtime" summary={runtime.label} state={state(1)} open={isOpen(1)} onToggle={() => toggle(1)}>
                <p className="su-p">The program that runs the model. Every one of these works the same way here — pick the one you
                  already use, or the one that suits your computer.</p>
                <div className="su-runtimes" role="radiogroup" aria-label="Runtime">
                  {guide.runtimes.map((card) => (
                    <button
                      key={card.id}
                      role="radio"
                      aria-checked={card.id === runtime.id}
                      className="su-runtime"
                      onClick={() => setRuntimeId(card.id)}
                    >
                      <span className="su-runtime-name">{card.label}</span>
                      <span className="su-runtime-meta mono">{card.port ? `:${card.port}` : card.generic ? "any port" : "port varies"}</span>
                    </button>
                  ))}
                </div>
                <div className="su-detail">
                  <div className="su-detail-head">
                    <OSPicker value={os} onChange={setOS} available={supported} />
                    {runtime.home && (
                      <a className="link su-home" href={runtime.home} target="_blank" rel="noreferrer">
                        Official download {Icon.external}
                      </a>
                    )}
                  </div>
                  <p className="su-p">
                    {installFor ? (
                      <Rich text={installFor} />
                    ) : (
                      <>
                        {runtime.label} doesn’t run on {OS_LABEL[os]}. Pick another runtime, or install it on a
                        computer that runs {supported?.map((o) => OS_LABEL[o]).join(" or ")}.
                      </>
                    )}
                  </p>
                  {runtime.facts && (
                    <dl className="su-caps su-facts" aria-label={`What ${runtime.label} reports and takes`}>
                      <div>
                        <dt>Context window</dt>
                        <dd>
                          {runtime.facts.context_reported === true
                            ? "Reported"
                            : runtime.facts.context_reported === false
                              ? "Not reported — you set it"
                              : "On some versions — else you set it"}
                        </dd>
                      </div>
                      <div>
                        <dt>Structured output</dt>
                        <dd>{STRUCTURED_LABEL[runtime.facts.structured]}</dd>
                      </div>
                      <div>
                        <dt>Embeddings</dt>
                        <dd>{runtime.facts.embeddings ? "Yes" : "No — use a second runtime"}</dd>
                      </div>
                      <div>
                        <dt>Listens on</dt>
                        <dd>
                          {runtime.facts.listens_everywhere ? (
                            <span className="su-warn-text">Every interface, unless told not to</span>
                          ) : (
                            "This computer only"
                          )}
                        </dd>
                      </div>
                    </dl>
                  )}
                </div>
              </Step>

              <Step
                n={2}
                title={haveModels ? "Your models" : "Get a model"}
                summary={
                  haveModels
                    ? `${ownModels.length} ready on ${reporting?.name ?? "your computer"}`
                    : undefined
                }
                state={state(2)}
                open={isOpen(2)}
                onToggle={() => toggle(2)}
              >
                {haveModels ? (
                  <>
                    <p className="su-p">
                      {reporting?.name ?? "Your computer"} already has {ownModels.length === 1 ? "a model" : `${ownModels.length} models`} that
                      can write the crew’s work — nothing to download. You choose which one to use in step 5.
                    </p>
                    <ul className="su-have" aria-label="Models already on your computer">
                      {ownModels.slice(0, 6).map((m) => (
                        <li key={m.spec}>
                          <span className="su-have-mark" aria-hidden="true">
                            {Icon.check}
                          </span>
                          <span className="mono su-have-name">{m.name}</span>
                          <span className="su-have-meta">
                            {m.runtime}
                            {m.size_bytes ? ` · ${bytes(m.size_bytes)}` : ""}
                          </span>
                        </li>
                      ))}
                    </ul>
                    {ownModels.length > 6 && (
                      <p className="su-fine">And {ownModels.length - 6} more — all of them are listed in step 5.</p>
                    )}
                  </>
                ) : (
                  <p className="su-p">
                    {reporting
                      ? `The connector didn’t find a model on ${reporting.name} that can write yet. `
                      : "Already have a model? Skip this step — once your computer is connected, step 5 lists what it has. "}
                    Otherwise, get one:
                  </p>
                )}
                {haveModels ? (
                  <details className="su-more su-another">
                    <summary>Get another model in {runtime.label}</summary>
                    {download}
                  </details>
                ) : (
                  download
                )}
              </Step>

              <Step n={3} title="Start the runtime’s server" state={state(3)} open={isOpen(3)} onToggle={() => toggle(3)}>
                <p className="su-p">
                  <Rich text={runtime.serve} />
                </p>
                <p className="su-fine">Check it on that computer — an answer means it’s running:</p>
                <CopyLine command={runtime.check.split("  →  ")[0]} label={`Check that ${runtime.label} is running`} />
                <div className="notice notice-warn su-check" role="note">
                  {Icon.alert}
                  <span className="notice-body">
                    <span className="notice-title">Keep it on this computer only</span>
                    <span className="notice-text">
                      Never bind a runtime to <code className="su-code">0.0.0.0</code> or set{" "}
                      <code className="su-code">OLLAMA_ORIGINS=*</code> — the connector doesn’t need either, and both
                      let anyone on your network use your model.
                      {runtime.exposure && (
                        <>
                          {" "}
                          <Rich text={runtime.exposure} />
                        </>
                      )}
                    </span>
                  </span>
                </div>
              </Step>

              <Step
                n={4}
                title="Install and pair the connector"
                summary={approved.length ? `${approved.length} paired` : undefined}
                state={state(4)}
                open={isOpen(4)}
                onToggle={() => toggle(4)}
              >
                <Pairing connector={guide.connector} os={os} onOS={setOS} onChanged={loadDevices} />
              </Step>

              <Step n={5} title="See what was detected" state={state(5)} open={isOpen(5)} onToggle={() => toggle(5)}>
                <Detected devices={devices ?? []} onChanged={replace} />
              </Step>

              <Step
                n={6}
                title="Test it"
                summary={tested?.ok ? `${tested.seconds.toFixed(tested.seconds < 10 ? 2 : 1)} s round trip` : undefined}
                state={state(6)}
                open={isOpen(6)}
                onToggle={() => toggle(6)}
              >
                <TestIt devices={devices ?? []} onResult={(ok, seconds) => setTested({ ok, seconds })} />
              </Step>
            </ol>
          ) : null}

          <Troubleshooting connector={guide?.connector ?? null} />
      </div>
    </div>
  );
}
