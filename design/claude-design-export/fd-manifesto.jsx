/* fd-manifesto.jsx — Direction C: Manifesto. Editorial serif, minimal, negative space. */

function Manifesto(){
  const p = window.usePipeline(window.EXAMPLES[0]);
  const running = p.status !== 'idle';
  const stateFor = (i) => {
    if (p.results.find(r => r.idx === i)) return 'done';
    if (i === p.current && p.status === 'thinking') return 'active';
    if (i === p.current && p.status === 'redo') return 'redo';
    if (i === p.current && p.status === 'gate') return 'gate';
    return 'pending';
  };
  const cur = p.current >= 0 && p.current < p.PHASES.length ? p.PHASES[p.current] : null;

  return (
    <div className="dir dir-manifesto fx-skin">
      <header className="mf-head">
        <span className="mf-wm">AI Software <span className="mf-wm-it">Engineering Team</span></span>
        <span className="mf-head-meta">
          <span className="mf-dot" data-on={running}></span>
          {running ? 'building · ' + p.results.length + ' of 8' : 'eight agents, one idea'}
        </span>
      </header>

      <div className="mf-stage">
        {/* main editorial column */}
        <div className="mf-main">
          {!running && (
            <React.Fragment>
              <p className="mf-kicker">The autonomous software engineering org</p>
              <h1 className="mf-h1">
                One idea in.<br/>
                <span className="mf-h1-it">A built product</span><br/>
                out.
              </h1>
              <p className="mf-lede">
                Eight specialist agents — product, design, backend, frontend, QA, security, ops and cost —
                debate, build and review your idea into production-ready software. You hold the pen at every gate.
              </p>

              <div className="mf-input">
                <label className="mf-input-k">Describe what to build</label>
                <div className="mf-input-field">
                  <input value={p.idea} onChange={(e)=>p.setIdea(e.target.value)} spellCheck={false} />
                  <button className="mf-shuffle" title="surprise me"
                    onClick={()=>p.setIdea(window.EXAMPLES[Math.floor(Math.random()*window.EXAMPLES.length)])}>↻</button>
                </div>
              </div>

              <div className="mf-begin-row">
                <button className="mf-begin" onClick={p.run}>
                  <span>Begin the build</span>
                  <span className="mf-begin-line"></span>
                  <span className="mf-begin-arrow">→</span>
                </button>
                <div className="mf-opts">
                  <div className="mf-seg">
                    {['auto','local','manual'].map(m=>(
                      <button key={m} className={'mf-seg-b'+(p.mode===m?' on':'')} onClick={()=>p.setMode(m)}>{m}</button>
                    ))}
                  </div>
                  <button className={'mf-toggle'+(p.approvals?' on':'')} onClick={()=>p.setApprovals(a=>!a)}>
                    approval gates {p.approvals?'on':'off'}
                  </button>
                </div>
              </div>
            </React.Fragment>
          )}

          {running && cur && (
            <div className="mf-live" key={p.current + p.status}>
              <p className="mf-kicker">Phase {cur.n} — {cur.role}</p>
              <h2 className="mf-live-name">{cur.name}</h2>
              {(p.status==='thinking' || p.status==='redo') && (
                <p className="mf-live-status">
                  {p.status==='redo' ? 'Reconsidering with your feedback' : (cur.debate ? 'Agents are debating the architecture' : 'Working')}
                  <span className="mf-ell"><i></i><i></i><i></i></span>
                </p>
              )}
              {p.status==='gate' && (
                <React.Fragment>
                  {cur.debate && (
                    <p className="mf-debate">
                      <span className="mf-debate-k">Debate · {window.DEBATE.topic}.</span> {window.DEBATE.verdict}
                    </p>
                  )}
                  <p className="mf-quote">“{cur.out}”</p>
                  <div className="mf-gate-actions">
                    <button className="mf-approve" onClick={p.approve}>Approve, continue →</button>
                    <button className="mf-reject" onClick={p.reject}>Send back</button>
                  </div>
                </React.Fragment>
              )}
            </div>
          )}

          {running && p.status==='done' && (
            <div className="mf-live">
              <p className="mf-kicker">Complete</p>
              <h2 className="mf-live-name">Your build is ready.</h2>
              <p className="mf-quote">“Eight phases shipped for {window.fmtCost(p.totals.cost)} — {p.totals.localPct}% of it ran on local models. Deliverables stored, a summary written to memory for next time.”</p>
              <button className="mf-reject" onClick={p.reset}>Build something else</button>
            </div>
          )}
        </div>

        {/* right rail — pipeline index */}
        <aside className="mf-rail">
          <div className="mf-rail-h">The pipeline</div>
          <ol className="mf-list">
            {p.PHASES.map((ph,i) => {
              const s = stateFor(i);
              return (
                <li key={ph.key} className={'mf-li mf-'+s}>
                  <span className="mf-li-n">{ph.n}</span>
                  <span className="mf-li-name">{ph.name}</span>
                  <span className="mf-li-mark">
                    {s==='done'?'✓':s==='active'?'·':s==='redo'?'↻':s==='gate'?'?':''}
                  </span>
                </li>
              );
            })}
          </ol>
          <div className="mf-rail-foot">
            <div className="mf-rail-stat"><span>{window.fmtTok(p.totals.tok)}</span><label>tokens</label></div>
            <div className="mf-rail-stat"><span>{window.fmtCost(p.totals.cost)}</span><label>spent</label></div>
            <div className="mf-rail-stat"><span>{p.totals.localPct}%</span><label>local</label></div>
          </div>
        </aside>
      </div>
    </div>
  );
}
window.Manifesto = Manifesto;
