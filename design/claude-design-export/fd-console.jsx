/* fd-console.jsx — Direction B: Console. Dark terminal / ops stream, mono-forward. */

function ConsoleDir(){
  const p = window.usePipeline(window.EXAMPLES[0]);
  const running = p.status !== 'idle';
  const logRef = React.useRef(null);

  const stateFor = (i) => {
    if (p.results.find(r => r.idx === i)) return 'done';
    if (i === p.current && (p.status === 'thinking')) return 'active';
    if (i === p.current && p.status === 'redo') return 'redo';
    if (i === p.current && p.status === 'gate') return 'gate';
    return 'pending';
  };

  // build log lines from state
  const log = [];
  if (running){
    log.push({ t:'cmd', a:'$', b:'swe build', c:'"' + p.idea + '"' });
    log.push({ t:'sys', b:'thread 0xA13F created · LangGraph StateGraph compiled (8 nodes)' });
    log.push({ t:'sys', b:'router mode = ' + p.mode + (p.approvals ? ' · human-in-the-loop ON' : ' · auto-run') });
  }
  p.results.forEach(r => {
    const ph = p.PHASES[r.idx];
    if (ph.debate){
      log.push({ t:'debate', b:'agent debate · ' + window.DEBATE.topic + ' → ' + window.DEBATE.verdict });
    }
    log.push({ t:'phase', n:ph.n, key:ph.key, model:r.model, tok:window.fmtTok(ph.tok),
               cost:window.fmtCost(r.cost), status:r.status });
    log.push({ t:'out', b:ph.out });
    if (r.status === 'approved') log.push({ t:'gate-ok', b:'‹ approved by you — graph resumed ›' });
    if (r.status === 'auto')     log.push({ t:'gate-ok', b:'‹ auto-approved ›' });
  });
  if (p.status === 'thinking' || p.status === 'redo'){
    const ph = p.PHASES[p.current];
    if (ph.debate && p.status==='thinking') log.push({ t:'debate', b:'running agent debate · ' + window.DEBATE.topic + '…' });
    log.push({ t:'active', n:ph.n, key:ph.key, redo:p.status==='redo' });
  }
  if (p.status === 'done') log.push({ t:'done', b:'build complete · deliverables stored · summary → long-term memory' });

  React.useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [p.results.length, p.status, p.current]);

  const gatePh = p.status === 'gate' ? p.PHASES[p.current] : null;

  return (
    <div className="dir dir-console fx-skin">
      <div className="cs-chrome">
        <span className="cs-dots"><i></i><i></i><i></i></span>
        <span className="cs-title">swe-team — run #0xA13F — {p.mode}</span>
        <span className="cs-chrome-meta">{p.results.length}/8 · {window.fmtCost(p.totals.cost)}</span>
      </div>
      <div className="cs-body">
        {/* sidebar state machine */}
        <aside className="cs-aside">
          <div className="cs-aside-h">// state graph</div>
          <ul className="cs-sm">
            {p.PHASES.map((ph,i) => {
              const s = stateFor(i);
              return (
                <li key={ph.key} className={'cs-sm-row cs-' + s}>
                  <span className="cs-led"></span>
                  <span className="cs-sm-n">{ph.n}</span>
                  <span className="cs-sm-name">{ph.key}</span>
                  <span className="cs-sm-s">
                    {s==='done'?'✓':s==='active'?'▸':s==='redo'?'↻':s==='gate'?'⏸':'·'}
                  </span>
                </li>
              );
            })}
          </ul>
          <div className="cs-hud">
            <div className="cs-hud-row"><span>tok</span><b>{window.fmtTok(p.totals.tok)}</b></div>
            <div className="cs-hud-row"><span>cost</span><b>{window.fmtCost(p.totals.cost)}</b></div>
            <div className="cs-hud-row"><span>local</span><b>{p.totals.localPct}%</b></div>
            <div className="cs-hud-bar"><span style={{width:(p.totals.progress*100)+'%'}}></span></div>
          </div>
        </aside>

        {/* main */}
        <div className="cs-main">
          <div className="cs-tagline">
            <span className="cs-tag-k">// build anything from a sentence</span>
            <h1 className="cs-h1">A whole eng team<br/>in your terminal.</h1>
          </div>

          <div className="cs-promptbar">
            <span className="cs-prompt-sym">swe&nbsp;❯</span>
            <input className="cs-prompt-in" value={p.idea} disabled={running}
              onChange={(e)=>p.setIdea(e.target.value)} spellCheck={false} />
            {!running
              ? <button className="cs-run" onClick={p.run}>run ⏎</button>
              : <button className="cs-run cs-reset" onClick={p.reset}>reset</button>}
          </div>

          <div className="cs-ctlrow">
            <div className="cs-seg">
              {['auto','local','manual'].map(m => (
                <button key={m} disabled={running} className={'cs-seg-b'+(p.mode===m?' on':'')}
                  onClick={()=>p.setMode(m)}>{m}</button>
              ))}
            </div>
            <button className={'cs-chk'+(p.approvals?' on':'')} disabled={running}
              onClick={()=>p.setApprovals(a=>!a)}>[{p.approvals?'x':' '}] approval gates</button>
          </div>

          <div className="cs-log" ref={logRef}>
            {!running && (
              <div className="cs-idle">
                <span>›_ waiting for input</span>
                <span className="cs-idle-sub">type an idea above and hit run — watch 8 agents build it, phase by phase.</span>
              </div>
            )}
            {log.map((l,idx) => {
              if (l.t==='cmd')   return <div key={idx} className="cs-l cs-l-cmd"><span className="cs-a">{l.a}</span> {l.b} <span className="cs-arg">{l.c}</span></div>;
              if (l.t==='sys')   return <div key={idx} className="cs-l cs-l-sys">→ {l.b}</div>;
              if (l.t==='debate')return <div key={idx} className="cs-l cs-l-debate">⚖ {l.b}</div>;
              if (l.t==='phase') return (
                <div key={idx} className="cs-l cs-l-phase">
                  <span className="cs-ph-n">[{l.n}]</span>
                  <span className="cs-ph-key">{l.key}</span>
                  <span className="cs-ph-ok">ok</span>
                  <span className="cs-ph-model">{l.model}</span>
                  <span className="cs-ph-tok">{l.tok} tok</span>
                  <span className="cs-ph-cost">{l.cost}</span>
                </div>
              );
              if (l.t==='out')   return <div key={idx} className="cs-l cs-l-out">{l.b}</div>;
              if (l.t==='gate-ok')return <div key={idx} className="cs-l cs-l-gateok">{l.b}</div>;
              if (l.t==='active')return (
                <div key={idx} className="cs-l cs-l-active">
                  <span className="cs-ph-n">[{l.n}]</span>
                  <span className="cs-ph-key">{l.key}</span>
                  <span className="cs-spin"></span>
                  <span>{l.redo ? 're-running with your feedback…' : 'running…'}</span>
                </div>
              );
              if (l.t==='done')  return <div key={idx} className="cs-l cs-l-done">✓ {l.b}</div>;
              return null;
            })}
            {gatePh && (
              <div className="cs-gate">
                <div className="cs-gate-l">‹ phase {gatePh.n} paused — approval required ›</div>
                <div className="cs-gate-actions">
                  <button className="cs-gate-ok" onClick={p.approve}>approve ⏎</button>
                  <button className="cs-gate-no" onClick={p.reject}>reject + feedback</button>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
window.ConsoleDir = ConsoleDir;
