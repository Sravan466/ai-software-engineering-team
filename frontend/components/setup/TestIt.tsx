"use client";

import { useEffect, useRef, useState } from "react";
import { api, type Device, type DeviceTestResult } from "@/lib/api";
import { Icon } from "@/components/shell/icons";
import { CopyLine } from "./parts";

type Fix = { title: string; text: string; command?: string; label?: string; anchor?: string };

/** What a refusal means, and the one thing to do about it — by the connector's reason. */
function fixFor(result: Extract<DeviceTestResult, { ok: false }>, device: Device): Fix {
  switch (result.code) {
    case "limit":
      return {
        title: "Over one of that computer’s limits",
        text: result.error,
        command: "aiteam-connect limits",
        label: "Show the connector's limits",
      };
    case "paused":
      return {
        title: `Model calls are paused on ${device.name}`,
        text: "Resume them on that computer, then send the test again.",
        command: "aiteam-connect resume",
        label: "Resume the connector",
      };
    case "unreachable":
      return {
        title: "The runtime isn’t answering on that computer",
        text: result.error,
        anchor: "#t-runtime",
      };
    case "refused":
      return { title: `${device.name} refused the request`, text: result.error };
    case "runtime":
      return { title: "The runtime answered with an error", text: result.error, anchor: "#t-model" };
    default:
      return {
        title: "No answer came back",
        text: `${result.error} If the computer went to sleep, wake it; the connector reconnects by itself.`,
        anchor: "#t-sleep",
      };
  }
}

function seconds(s: number): string {
  return s < 10 ? s.toFixed(2) : s < 100 ? s.toFixed(1) : Math.round(s).toString();
}

/**
 * Step 6: a real prompt, sent the way a build sends one — this server, the
 * connector, the runtime, the model — and back, with how long that took.
 */
export default function TestIt({
  devices,
  onResult,
}: {
  devices: Device[];
  onResult: (ok: boolean, seconds: number) => void;
}) {
  const ready = devices.filter((d) => d.status === "approved" && d.chat_model);
  const [pick, setPick] = useState<string | null>(null);
  const device = ready.find((d) => d.id === pick) ?? ready.find((d) => d.online) ?? ready[0] ?? null;
  const [sending, setSending] = useState(false);
  const [result, setResult] = useState<DeviceTestResult | null>(null);
  const [error, setError] = useState("");
  const [elapsed, setElapsed] = useState(0);
  const started = useRef(0);

  useEffect(() => {
    if (!sending) return;
    const t = setInterval(() => setElapsed((performance.now() - started.current) / 1000), 100);
    return () => clearInterval(t);
  }, [sending]);

  // A different computer is a different test.
  useEffect(() => {
    setResult(null);
    setError("");
  }, [device?.id]);

  if (!device) {
    return (
      <p className="su-fine">
        Pair a computer (step 4) and pick its model (step 5) to send a test prompt.
      </p>
    );
  }

  const spec = device.chat_model ?? "";
  const sourceId = spec.slice(0, spec.indexOf(":"));
  const modelName = spec.slice(spec.indexOf(":") + 1);
  const runtime = device.hello?.sources.find((s) => s.id === sourceId)?.label ?? sourceId;
  const limits = device.hello?.limits ?? null;

  async function send() {
    if (!device) return;
    // Held to this computer for the whole round trip, so a poll that brings
    // another one online can't put this answer under its name.
    setPick(device.id);
    setSending(true);
    setResult(null);
    setError("");
    setElapsed(0);
    started.current = performance.now();
    try {
      const r = await api.testDevice(device.id);
      setResult(r);
      onResult(r.ok, r.seconds);
    } catch (e: any) {
      setError(e?.message || "The test couldn't be sent.");
      onResult(false, 0);
    } finally {
      setSending(false);
    }
  }

  const trip = sending ? "live" : result?.ok ? "ok" : result || error ? "bad" : "idle";
  const failed = result && !result.ok ? fixFor(result, device) : null;

  return (
    <div className="su-test">
      {ready.length > 1 && (
        <label className="field su-which">
          <span className="label">Computer</span>
          <select className="select" value={device.id} onChange={(e) => setPick(e.target.value)} disabled={sending}>
            {ready.map((d) => (
              <option key={d.id} value={d.id}>
                {d.name}
                {d.online ? "" : " (offline)"}
              </option>
            ))}
          </select>
        </label>
      )}

      <p className="su-p">
        Sends one short prompt the way a build would, and shows how long it took.
      </p>

      <ol className="su-route" data-trip={trip} aria-label="The path the test prompt takes">
        <li>This server</li>
        <li>{device.name}</li>
        <li>{runtime}</li>
        <li className="mono">{modelName}</li>
      </ol>

      <div className="su-test-go">
        <button
          className="btn btn-primary"
          onClick={send}
          disabled={sending || !device.online}
          aria-describedby={!device.online ? "su-test-offline" : undefined}
        >
          {sending ? <span className="btn-spinner" aria-hidden="true" /> : Icon.play}
          {sending ? "Waiting for an answer…" : result ? "Send it again" : "Send a test prompt"}
        </button>
        {sending && (
          <span className="su-test-clock mono" role="timer" aria-label="Seconds waited">
            {elapsed.toFixed(1)} s
          </span>
        )}
        {!device.online && (
          <span id="su-test-offline" className="su-fine">
            {device.name} isn’t connected. Start the connector on it first.
          </span>
        )}
      </div>

      <div aria-live="polite">
        {result?.ok && (
          <div className="su-trip">
            <p className="su-trip-time">
              <span className="su-trip-num">{seconds(result.seconds)}</span>
              <span className="su-trip-unit">seconds, there and back</span>
            </p>
            <blockquote className="su-trip-answer">{result.answer || "(an empty answer)"}</blockquote>
            <dl className="su-trip-facts">
              <div>
                <dt>Tokens</dt>
                <dd className="mono">
                  {result.prompt_tokens.toLocaleString()} in · {result.completion_tokens.toLocaleString()} out
                </dd>
              </div>
              <div>
                <dt>Stopped because</dt>
                <dd>{result.finish_reason === "length" ? "it hit the output limit" : result.finish_reason ?? "—"}</dd>
              </div>
              {result.thought && (
                <div>
                  <dt>Reasoning</dt>
                  <dd>Thought first; kept apart from the answer</dd>
                </div>
              )}
            </dl>
          </div>
        )}
        {(failed || error) && (
          <div className="notice notice-bad" role="alert">
            {Icon.alert}
            <span className="notice-body">
              <span className="notice-title">{failed?.title ?? "The test didn’t go through"}</span>
              <span className="notice-text">
                {failed?.text ?? error}
                {failed?.anchor && (
                  <>
                    {" "}
                    <a className="link" href={failed.anchor}>
                      What to do
                    </a>
                  </>
                )}
              </span>
              {failed?.command && <CopyLine command={failed.command} label={failed.label ?? failed.command} />}
            </span>
          </div>
        )}
      </div>

      {limits && (
        <>
          <h3 className="su-h3">What {device.name} allows</h3>
          <dl className="su-caps">
            <div>
              <dt>At once</dt>
              <dd className="mono">{limits.concurrency}</dd>
            </div>
            <div>
              <dt>Per minute</dt>
              <dd className="mono">{limits.requests_per_minute}</dd>
            </div>
            <div>
              <dt>Longest prompt</dt>
              <dd className="mono">{limits.max_prompt_chars.toLocaleString()} chars</dd>
            </div>
            <div>
              <dt>Longest answer</dt>
              <dd className="mono">{limits.max_output_tokens.toLocaleString()} tokens</dd>
            </div>
            <div>
              <dt>Per call</dt>
              <dd className="mono">{limits.timeout_seconds} s</dd>
            </div>
          </dl>
          <p className="su-fine">
            You set these on that computer. A build that goes over one is refused with the reason.
          </p>
          <CopyLine command="aiteam-connect limits --concurrency 1 --requests-per-minute 60" label="Change the limits" />
        </>
      )}
    </div>
  );
}
