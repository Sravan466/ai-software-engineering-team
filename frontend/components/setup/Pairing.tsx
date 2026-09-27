"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, type ConnectorInfo, type Device, type OS, type Pairing as PairingT, type PairingState } from "@/lib/api";
import { Icon } from "@/components/shell/icons";
import { CopyLine, OSPicker, OS_LABEL, ago, useNow } from "./parts";

/**
 * Step 4: a code, a pinned command, and the approval.
 *
 * The code lives ten minutes and is used once. While it waits, this polls for the
 * connector claiming it; once one has, the computer is shown — name, OS, the network
 * it came from, when — and nothing is sent to it until someone here approves it.
 */
export default function Pairing({
  connector,
  os,
  onOS,
  onChanged,
}: {
  connector: ConnectorInfo | null;
  os: OS;
  onOS: (os: OS) => void;
  onChanged: () => void;
}) {
  const [pairing, setPairing] = useState<PairingT | null>(null);
  const [state, setState] = useState<PairingState | null>(null);
  const [busy, setBusy] = useState<"" | "code" | "approve" | "reject">("");
  const [error, setError] = useState("");
  const [approved, setApproved] = useState<Device | null>(null);
  const now = useNow(1000);
  const live = useRef(true);
  useEffect(() => () => void (live.current = false), []);

  const start = useCallback(async () => {
    setBusy("code");
    setError("");
    setApproved(null);
    setState(null);
    try {
      setPairing(await api.startPairing());
    } catch (e: any) {
      setError(e?.message || "Couldn't make a code.");
    } finally {
      setBusy("");
    }
  }, []);

  // Poll while there is something to wait for: a claim, or an approval.
  const waiting = pairing && !approved && (!state || state.state === "waiting" || (state.state === "claimed" && state.device?.status === "pending"));
  useEffect(() => {
    if (!pairing || !waiting) return;
    let stop = false;
    const tick = async () => {
      try {
        const next = await api.pairingState(pairing.id);
        if (!stop && live.current) setState(next);
      } catch {
        // A blip; the next tick asks again.
      }
    };
    tick();
    const t = setInterval(tick, 2000);
    return () => {
      stop = true;
      clearInterval(t);
    };
  }, [pairing, waiting]);

  const cancel = useCallback(async () => {
    if (pairing) api.cancelPairing(pairing.id).catch(() => {});
    setPairing(null);
    setState(null);
  }, [pairing]);

  const device = state?.device ?? null;

  async function approve() {
    if (!device) return;
    setBusy("approve");
    setError("");
    try {
      const d = await api.approveDevice(device.id);
      setApproved(d);
      onChanged();
    } catch (e: any) {
      setError(e?.message || "Couldn't approve it.");
    } finally {
      setBusy("");
    }
  }

  async function reject() {
    if (!device) return;
    setBusy("reject");
    try {
      await api.forgetDevice(device.id);
      setPairing(null);
      setState(null);
      setError("");
      onChanged();
    } catch (e: any) {
      setError(e?.message || "Couldn't remove it.");
    } finally {
      setBusy("");
    }
  }

  const info = pairing?.connector ?? connector;
  const left = pairing ? Math.max(0, Math.round((+new Date(pairing.expires_at) - now) / 1000)) : 0;
  const expired = pairing && (left === 0 || state?.state === "expired");
  const fraction = pairing ? Math.min(1, left / pairing.ttl_seconds) : 0;

  return (
    <div className="su-pair">
      {!pairing && (
        <>
          <p className="su-p">
            The connector is a small program that runs on your computer. It dials out to this server and
            keeps the line open, so it never opens a port and your model is never exposed to the internet.
          </p>
          <button className="btn btn-primary btn-lg" onClick={start} disabled={busy === "code"}>
            {busy === "code" ? <span className="btn-spinner" aria-hidden="true" /> : Icon.laptop}
            Connect my computer
          </button>
        </>
      )}

      {pairing && !device && !approved && (
        <div className="su-pair-grid">
          <div className="su-codebox" data-expired={expired || undefined}>
            <span className="label">Your pairing code</span>
            <output className="su-bigcode mono" aria-label={`Pairing code ${pairing.code.split("").join(" ")}`}>
              {pairing.code.split("-").map((half, i) => (
                <span key={i}>
                  {i > 0 && <span className="su-bigcode-sep">-</span>}
                  {half}
                </span>
              ))}
            </output>
            <div className="su-meter" aria-hidden="true">
              <span style={{ transform: `scaleX(${expired ? 0 : fraction})` }} data-low={left < 60 || undefined} />
            </div>
            <span className="su-countdown">
              {expired ? (
                "This code expired."
              ) : (
                <>
                  Expires in <span className="mono">{Math.floor(left / 60)}:{String(left % 60).padStart(2, "0")}</span>
                  {" · "}single use
                </>
              )}
            </span>
            <div className="su-codebox-actions">
              {expired ? (
                <button className="btn btn-sm btn-primary" onClick={start}>
                  {Icon.refresh} Make a new code
                </button>
              ) : (
                <button className="btn btn-sm btn-ghost" onClick={cancel}>
                  Cancel
                </button>
              )}
            </div>
          </div>

          <div className="su-pair-steps">
            <p className="su-p">
              <strong>Run this in a terminal on the computer with the model.</strong> It asks for the code —
              type it there, not on the command line.
            </p>
            <OSPicker value={os} onChange={onOS} />
            {info && <CopyLine command={info.commands[os]} label={`Install and run the connector on ${OS_LABEL[os]}`} />}
            <p className="su-fine">
              Pinned to <span className="mono">{info?.package}=={info?.version}</span>. Needs Python 3.9+ and{" "}
              <a className="link" href="https://pipx.pypa.io/stable/installation/" target="_blank" rel="noreferrer">
                pipx
              </a>
              .
            </p>
            <div className="notice notice-warn su-check" role="note">
              <span className="notice-body">
                <span className="notice-title">Check that your terminal shows {pairing.account}</span>
                <span className="notice-text">
                  Before it connects, the connector asks “This connects &lt;your computer&gt; to{" "}
                  <strong>{pairing.account}</strong> on <span className="mono">{hostOf(info?.server)}</span>. Continue?”
                  If it names another account, answer no: someone sent you their code.
                </span>
              </span>
            </div>
            <details className="su-more">
              <summary>Running this install from its source code?</summary>
              <p className="su-fine">Until the package is published, run the connector from the repository instead:</p>
              {info && <CopyLine command={info.source_command} label="Run the connector from source" />}
            </details>
            <p className="su-waiting" aria-live="polite">
              {!expired && (
                <>
                  <span className="dot dot-run dot-pulse" aria-hidden="true" /> Waiting for your computer…
                </>
              )}
            </p>
          </div>
        </div>
      )}

      {device && device.status === "pending" && !approved && (
        <section className="su-approve" aria-labelledby="approve-title" aria-live="polite">
          <h3 id="approve-title">Approve this computer?</h3>
          <p className="su-p">Nothing is sent to it until you do. Only approve a computer you just set up yourself.</p>
          <dl className="su-facts">
            <div>
              <dt>Name</dt>
              <dd>{device.name}</dd>
            </div>
            <div>
              <dt>System</dt>
              <dd>{device.os || "Not reported"}</dd>
            </div>
            <div>
              <dt>Connected from</dt>
              <dd>
                <span className="mono">{device.paired_from || "unknown"}</span>
                <span className={device.same_network ? "su-ok-text" : "su-warn-text"}>
                  {device.same_network ? " — the same network as this browser" : " — a different network from this browser"}
                </span>
              </dd>
            </div>
            <div>
              <dt>When</dt>
              <dd>{ago(device.created_at, now)}</dd>
            </div>
          </dl>
          <div className="su-row">
            <button className="btn btn-accent" onClick={approve} disabled={busy !== ""}>
              {busy === "approve" ? <span className="btn-spinner" aria-hidden="true" /> : Icon.check}
              Approve {device.name}
            </button>
            <button className="btn btn-danger" onClick={reject} disabled={busy !== ""}>
              {busy === "reject" && <span className="btn-spinner" aria-hidden="true" />}
              This isn’t my computer
            </button>
          </div>
        </section>
      )}

      {approved && (
        <div className="notice notice-ok" role="status">
          <span className="notice-body">
            <span className="notice-title">{approved.name} is approved.</span>
            <span className="notice-text">
              {approved.online
                ? "It's connected. Step 5 shows what it found."
                : "It will connect as soon as the connector is running."}
            </span>
            <span className="notice-actions">
              <button className="btn btn-sm" onClick={start}>
                {Icon.plus} Connect another computer
              </button>
            </span>
          </span>
        </div>
      )}

      {error && (
        <p className="su-error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

function hostOf(url: string | undefined): string {
  if (!url) return "this server";
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}
