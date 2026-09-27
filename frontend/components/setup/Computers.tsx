"use client";

import { useState } from "react";
import { api, type Device } from "@/lib/api";
import { Icon } from "@/components/shell/icons";
import { StatusDot, ago, modelLine, statusText, useNow } from "./parts";

/** Every computer paired to this account, with Rename / Disconnect / Forget. */
export default function Computers({
  devices,
  onChanged,
  onRemoved,
  onPair,
}: {
  devices: Device[] | null;
  onChanged: (d: Device) => void;
  onRemoved: (id: string) => void;
  onPair: () => void;
}) {
  const now = useNow(15000);
  if (devices === null) return <p className="su-p dim">Loading…</p>;
  if (!devices.length) {
    return (
      <div className="su-empty">
        <span className="su-empty-icon" aria-hidden="true">
          {Icon.laptop}
        </span>
        <p className="su-p">
          No computers yet. Pair one and the crew can run on the model it serves — the model stays on your
          computer, and you can disconnect it here at any time.
        </p>
        <button className="btn btn-primary" onClick={onPair}>
          {Icon.laptop} Connect my computer
        </button>
      </div>
    );
  }
  return (
    <ul className="su-devices" aria-label="Your computers">
      {devices.map((d) => (
        <DeviceRow key={d.id} device={d} now={now} onChanged={onChanged} onRemoved={onRemoved} />
      ))}
    </ul>
  );
}

function DeviceRow({
  device,
  now,
  onChanged,
  onRemoved,
}: {
  device: Device;
  now: number;
  onChanged: (d: Device) => void;
  onRemoved: (id: string) => void;
}) {
  const [mode, setMode] = useState<"" | "rename" | "forget">("");
  const [name, setName] = useState(device.name);
  const [busy, setBusy] = useState<"" | "rename" | "disconnect" | "forget" | "approve">("");
  const [note, setNote] = useState("");
  const line = modelLine(device);

  async function run(kind: typeof busy, fn: () => Promise<void>) {
    setBusy(kind);
    setNote("");
    try {
      await fn();
    } catch (e: any) {
      setNote(e?.message || "That didn't work.");
    } finally {
      setBusy("");
    }
  }

  return (
    <li className="su-device">
      <div className="su-device-main">
        <span className="su-device-icon" aria-hidden="true">
          {Icon.laptop}
        </span>
        <div className="su-device-text">
          {mode === "rename" ? (
            <form
              className="su-rename"
              onSubmit={(e) => {
                e.preventDefault();
                run("rename", async () => {
                  onChanged(await api.updateDevice(device.id, { name }));
                  setMode("");
                });
              }}
            >
              <label className="sr-only" htmlFor={`name-${device.id}`}>
                Name for this computer
              </label>
              <input
                id={`name-${device.id}`}
                className="input"
                value={name}
                maxLength={120}
                autoFocus
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => e.key === "Escape" && (setMode(""), setName(device.name))}
              />
              <button className="btn btn-sm btn-primary" disabled={busy !== "" || !name.trim()}>
                Save
              </button>
              <button type="button" className="btn btn-sm btn-ghost" onClick={() => (setMode(""), setName(device.name))}>
                Cancel
              </button>
            </form>
          ) : (
            <span className="su-device-name">{device.name}</span>
          )}
          <span className="su-device-status">
            <StatusDot device={device} /> {statusText(device, now)}
          </span>
        </div>
      </div>

      <dl className="su-device-facts">
        <div>
          <dt>Model</dt>
          <dd className="mono">{line ? `${line.model} via ${line.runtime}` : "—"}</dd>
        </div>
        <div>
          <dt>Last seen</dt>
          <dd>{device.online ? "Now" : ago(device.last_seen_at, now)}</dd>
        </div>
        <div>
          <dt>Connector</dt>
          <dd>
            <span className="mono">{device.connector_version ?? "—"}</span>
            {device.outdated && <span className="badge badge-warn su-badge">Out of date</span>}
          </dd>
        </div>
        <div>
          <dt>System</dt>
          <dd>{device.hello?.os_version || device.os || "—"}</dd>
        </div>
      </dl>

      {mode === "forget" ? (
        <div className="su-confirm" role="group" aria-label={`Forget ${device.name}?`}>
          <span>Forget {device.name}? Its key stops working now; pairing again needs a new code.</span>
          <button className="btn btn-sm" autoFocus onClick={() => setMode("")} disabled={busy !== ""}>
            Keep
          </button>
          <button
            className="btn btn-sm btn-danger"
            disabled={busy !== ""}
            onClick={() =>
              run("forget", async () => {
                await api.forgetDevice(device.id);
                onRemoved(device.id);
              })
            }
          >
            {busy === "forget" && <span className="btn-spinner" aria-hidden="true" />}
            Forget
          </button>
        </div>
      ) : (
        <div className="su-device-acts">
          {device.status === "pending" && (
            <button
              className="btn btn-sm btn-accent"
              disabled={busy !== ""}
              onClick={() => run("approve", async () => onChanged(await api.approveDevice(device.id)))}
            >
              {busy === "approve" ? <span className="btn-spinner" aria-hidden="true" /> : Icon.check} Approve
            </button>
          )}
          <button className="btn btn-sm" onClick={() => setMode("rename")} disabled={busy !== ""}>
            {Icon.pen} Rename
          </button>
          <button
            className="btn btn-sm"
            disabled={busy !== "" || !device.online}
            title={device.online ? "Stop the connector now; running it again reconnects" : "It isn't connected"}
            onClick={() =>
              run("disconnect", async () => {
                await api.disconnectDevice(device.id);
                onChanged({ ...device, online: false, connected_since: null, last_seen_at: new Date().toISOString() });
              })
            }
          >
            {busy === "disconnect" ? <span className="btn-spinner" aria-hidden="true" /> : Icon.stop} Disconnect
          </button>
          <button className="btn btn-sm btn-danger" onClick={() => setMode("forget")} disabled={busy !== ""}>
            {Icon.trash} Forget
          </button>
        </div>
      )}
      {note && (
        <p className="su-error" role="alert">
          {note}
        </p>
      )}
    </li>
  );
}
