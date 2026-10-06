import type { ConnectorInfo } from "@/lib/api";
import { CopyLine } from "./parts";

/** The ways setup goes wrong, each with what to do. Linked from the checklist and the test. */
export default function Troubleshooting({ connector }: { connector: ConnectorInfo | null }) {
  return (
    <section className="su-trouble" aria-labelledby="trouble-title">
      <h2 id="trouble-title" className="su-h2">
        Troubleshooting
      </h2>
      <details id="t-runtime" className="su-more">
        <summary>The runtime isn’t running</summary>
        <p className="su-p">
          The connector only looks on this computer’s own address (127.0.0.1), on each runtime’s usual port.
          Start the runtime (step 3), run its check command, then press <strong>Check again</strong> in step 5.
        </p>
      </details>
      <details id="t-unknown" className="su-more">
        <summary>A port answered but wasn’t recognised</summary>
        <p className="su-p">
          Something is listening on a runtime’s port but didn’t answer like one. If it’s an
          OpenAI-compatible server on another port, add it on that computer.
        </p>
        <CopyLine command="aiteam-connect add-source http://127.0.0.1:<port>" label="Add a source to the connector" />
      </details>
      <details id="t-model" className="su-more">
        <summary>The model isn’t loaded</summary>
        <p className="su-p">
          The runtime answered but lists no model that can write. Download one (step 2) and, for LM Studio,
          load it in the Developer tab. A model that only makes embeddings can’t write the crew’s work.
        </p>
      </details>
      <details id="t-security" className="su-more">
        <summary>The runtime is older than a known security fix</summary>
        <p className="su-p">
          This version has a known vulnerability, and step 5 links the advisory. Update the runtime the way you
          installed it (step 1 links the download), restart it, and press <strong>Check again</strong>. Builds
          keep working in the meantime.
        </p>
      </details>
      <details id="t-exposed" className="su-more">
        <summary>The runtime is reachable from your network</summary>
        <p className="su-p">
          It answers on your network address, not just 127.0.0.1, so anyone on the same Wi-Fi can use it. vLLM,
          KoboldCpp and LocalAI do this by default. Restart it on 127.0.0.1 only (step 3 shows how). The connector
          doesn’t need more. Never bind to <code className="su-code">0.0.0.0</code> or set{" "}
          <code className="su-code">OLLAMA_ORIGINS=*</code>. Either one lets other devices, or any web page you
          open, use it.
        </p>
      </details>
      <details id="t-code" className="su-more">
        <summary>The pairing code expired</summary>
        <p className="su-p">
          Codes last ten minutes and work once. Press <strong>Make a new code</strong> in step 4 and run the
          command again.
        </p>
      </details>
      <details id="t-outdated" className="su-more">
        <summary>The connector is out of date</summary>
        <p className="su-p">
          This server needs connector {connector?.min_version ?? "—"} or newer. Stop the old one (Ctrl+C) and
          run the pinned command again. It fetches {connector?.version ?? "the current version"}.
        </p>
      </details>
      <details id="t-connector" className="su-more">
        <summary>The computer shows as offline</summary>
        <p className="su-p">
          The connector only runs while its terminal is open. Run the pinned command again on that computer;
          it remembers the pairing and reconnects without a new code.
        </p>
      </details>
      <details id="t-sleep" className="su-more">
        <summary>The computer went to sleep</summary>
        <p className="su-p">
          The build pauses and keeps its finished work. Wake the computer and the connector reconnects in a few
          seconds, then the build carries on. If the connector was closed, run the pinned command again. To keep
          a Mac awake during a long build, run <code className="su-code">caffeinate -i</code> in another terminal.
        </p>
      </details>
      <details id="t-limit" className="su-more">
        <summary>A build was refused by a limit</summary>
        <p className="su-p">
          Your computer limits calls at once, calls per minute, prompt and answer size, and call length. The
          build says which limit it hit. Raise it on that computer.
        </p>
        <CopyLine command="aiteam-connect limits --requests-per-minute 120" label="Raise a connector limit" />
      </details>
      {connector && (
        <details className="su-more">
          <summary>What the connector will and won’t do</summary>
          <p className="su-p">
            It answers {connector.ops.length} kinds of request (
            {connector.ops.map((op, i) => (
              <span key={op}>
                {i > 0 && ", "}
                <code className="su-code">{op}</code>
              </span>
            ))}
            ) and refuses the rest, logging each refusal to{" "}
            <code className="su-code">~/.aiteam-connect/connector.log</code>. It never downloads or deletes a model,
            runs a command, writes a file, or calls an address this website sends. Model answers are returned as
            data and nothing runs them. It opens no port, so{" "}
            <code className="su-code">lsof -iTCP -sTCP:LISTEN</code> shows nothing from it.
          </p>
          <p className="su-p">
            Every model call is written to <code className="su-code">~/.aiteam-connect/activity.log</code> with the
            time, model and token counts. Prompts and answers aren’t logged.{" "}
            <code className="su-code">aiteam-connect pause</code> stops it answering until{" "}
            <code className="su-code">aiteam-connect resume</code>.
          </p>
          <p className="su-fine">Verify the package before running it:</p>
          <CopyLine command={connector.verify} label="Verify the connector package" />
        </details>
      )}
    </section>
  );
}
