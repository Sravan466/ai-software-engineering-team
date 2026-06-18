/* fd-blueprint.jsx — Direction A: Blueprint. Swiss / technical, wired pipeline. */

function BlueprintNode({ phase, state, result, gate, onApprove, onReject, mode }){
  // state: 'pending' | 'active' | 'gate' | 'redo' | 'done'
  return (
    <div className={'bp-node bp-' + state}>
      <div className="bp-node-spine">
        <span className="bp-node-dot"></span>
      </div>
      <div className="bp-node-body">
        <div className="bp-node-top">
          <span className="bp-node-n">{phase.n}</span>
          <span className="bp-node-name">{phase.name}</span>
          <span className="bp-node-role">{phase.role}</span>
          {phase.debate && <span className="bp-node-badge">debate</span>}
          <span className="bp-node-status">
            {state === 'pending' && 'queued'}
            {state === 'active'  && 'running…'}
            {state === 'redo'    && 're-running'}
            {state === 'gate'    && 'awaiting approval'}
            {state === 'done'    && (result ? result.model : 'done')}
          </span>
        </div>
        {(state === 'active' || state === 'redo') && (
          <div className="bp-node-prog"><span></span></div>
        )}
        {state === 'done' && (
          <div className="bp-node-out">
            <span className="bp-node-deliver">{phase.deliver}</span>
            <span className="bp-node-outtext">{phase.out}</span>
          </div>
        )}
        {state === 'gate' && (
          <div className="bp-gate">
            {phase.debate && (
              <div className="bp-debate">
                <span className="bp-debate-k">agent debate · {window.DEBATE.topic}</span>
                <span className="bp-debate-v">→ {window.DEBATE.verdict}</span>
              </div>
            )}
            <div className="bp-gate-text">{phase.out}</div>
            <div className="bp-gate-actions">
              <button className="bp-btn bp-btn-ok" onClick={onApprove}>Approve →</button>
              <button className="bp-btn bp-btn-no" onClick={onReject}>Reject & regenerate</button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function Blueprint(){
  const p = window.usePipeline(window.EXAMPLES[0]);
  const stateFor = (i) => {
    if (p.results.find(r => r.idx === i)) return 'done';
    if (i === p.current && p.status === 'thinking') return 'active';
    if (i === p.current && p.status === 'redo') return 'redo';
    if (i === p.current && p.status === 'gate') return 'gate';
    return 'pending';
  };
  const running = p.status !== 'idle';

  return (
    <div className="dir dir-blueprint fx-skin">
      <div className="bp-grid">
        {/* LEFT */}
        <div className="bp-left">
          <header className="bp-head">
            <div className="bp-wm">
              <span className="bp-wm-mark"></span>
              <span className="bp-wm-text">AI<span className="bp-wm-thin">SWE TEAM</span></span>
            </div>
            <div className="bp-head-meta">
              <span className="bp-mono-k">v4 · blueprint</span>
              <span className="bp-head-dot" data-on={running}></span>
              <span className="bp-mono-k">{running ? 'pipeline live' : 'idle'}</span>
            </div>
          </header>

          <div className="bp-hero">
            <h1 className="bp-h1">Ship the whole<br/>engineering team.<br/><span className="bp-h1-em">Not just a tool.</span></h1>
            <p className="bp-sub">Eight specialist agents take one idea to production-ready software — product, design, backend, frontend, QA, security, ops, cost — and pause for your approval at every phase.</p>
          </div>

          <div className="bp-prompt">
            <div className="bp-prompt-head">
              <span className="bp-mono-k">project idea</span>
              <span className="bp-prompt-rule"></span>
              <button className="bp-shuffle" onClick={() => p.setIdea(window.EXAMPLES[Math.floor(Math.random()*window.EXAMPLES.length)])} disabled={running}>shuffle</button>
            </div>
            <div className="bp-prompt-field">
              <span className="bp-caret">›</span>
              <textarea value={p.idea} disabled={running}
                onChange={(e) => p.setIdea(e.target.value)} rows={2} />
            </div>
          </div>

          <div className="bp-controls">
            <div className="bp-seg">
              <span className="bp-seg-k">routing</span>
              {['auto','local','manual'].map(m => (
                <button key={m} className={'bp-seg-btn' + (p.mode===m?' on':'')}
                  disabled={running} onClick={() => p.setMode(m)}>{m}</button>
              ))}
            </div>
            <button className={'bp-approve-toggle' + (p.approvals?' on':'')}
              disabled={running} onClick={() => p.setApprovals(a => !a)}>
              <span className="bp-toggle-box"></span>
              approval gates
            </button>
          </div>

          <div className="bp-cta">
            {!running && <button className="bp-run" onClick={p.run}>Run the pipeline <span className="bp-run-arrow">→</span></button>}
            {running && <button className="bp-run bp-run-reset" onClick={p.reset}>Reset</button>}
            <div className="bp-meter">
              <div className="bp-meter-row">
                <span className="bp-mono-k">tokens</span><span className="bp-meter-v">{window.fmtTok(p.totals.tok)}</span>
              </div>
              <div className="bp-meter-row">
                <span className="bp-mono-k">cost</span><span className="bp-meter-v">{window.fmtCost(p.totals.cost)}</span>
              </div>
              <div className="bp-meter-row">
                <span className="bp-mono-k">on local</span><span className="bp-meter-v">{p.totals.localPct}%</span>
              </div>
            </div>
          </div>
        </div>

        {/* RIGHT — pipeline */}
        <div className="bp-right">
          <div className="bp-right-head">
            <span className="bp-mono-k">pipeline</span>
            <span className="bp-prompt-rule"></span>
            <span className="bp-mono-k">{p.results.length}/8 phases</span>
          </div>
          <div className="bp-pipe">
            {p.PHASES.map((ph, i) => (
              <BlueprintNode key={ph.key} phase={ph} state={stateFor(i)} mode={p.mode}
                result={p.results.find(r => r.idx === i)}
                onApprove={p.approve} onReject={p.reject} />
            ))}
            <div className={'bp-end' + (p.status==='done'?' on':'')}>
              <span className="bp-node-dot"></span>
              <span>{p.status==='done' ? 'Build complete — deliverables stored, summary written to memory.' : 'END'}</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
window.Blueprint = Blueprint;
