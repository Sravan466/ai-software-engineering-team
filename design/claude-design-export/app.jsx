/* global React, ReactDOM, TweaksPanel, useTweaks, TweakSection, TweakRadio, TweakColor, TweakToggle */
const { useState, useEffect, useRef, useMemo } = React;

// ─────────────────────────────────────────────────────────────
// Data
// ─────────────────────────────────────────────────────────────
const STYLES = [
  { key: "none",         label: "Free",        sub: "no LoRA",            tag: "open",     hue: 28 },
  { key: "anime",        label: "Anime",       sub: "vibrant, stylised",  tag: "lora-01",  hue: 320 },
  { key: "scientific",   label: "Scientific",  sub: "diagrammatic",       tag: "lora-02",  hue: 200 },
  { key: "character",    label: "Character",   sub: "D&D concept",        tag: "lora-03",  hue: 18 },
  { key: "ghibli",       label: "Ghibli",      sub: "painterly",          tag: "lora-04",  hue: 150 },
  { key: "realistic",    label: "Realistic",   sub: "photo, 8k",          tag: "lora-05",  hue: 40 },
  { key: "movie_poster", label: "Poster",      sub: "cinematic grain",    tag: "lora-06",  hue: 0  },
];

const QUALITY = [
  { key: "Fast",     steps: 15, cfg: 7.0, sec: "~6s"  },
  { key: "Balanced", steps: 20, cfg: 7.5, sec: "~9s"  },
  { key: "Quality",  steps: 25, cfg: 7.5, sec: "~14s" },
  { key: "Max",      steps: 30, cfg: 8.0, sec: "~20s" },
];

const EXAMPLES = {
  none:        ["a cabin in the mountains, snow, warm light", "futuristic cityscape at night, neon, rain", "a peaceful garden with a stone fountain"],
  anime:       ["a girl with blue hair, detailed, colorful", "anime warrior with glowing sword, dynamic", "cherry blossom landscape, sunset"],
  scientific:  ["detailed illustration of a human heart, labeled", "botanical illustration of a rose, accurate", "DNA double helix, molecular detail"],
  character:   ["fantasy warrior, full body, detailed armor", "cyberpunk assassin, neon, full plate", "medieval knight character sheet"],
  ghibli:      ["magical forest with spirits and glowing mushrooms", "flying castle above the clouds, birds", "seaside village, red rooftops, fishing boats"],
  realistic:   ["portrait of a woman, freckles, soft bokeh", "mountain lake at sunrise, mist, reflections", "vintage car on a European street, golden hour"],
  movie_poster:["lone astronaut on Mars, dust storm", "noir detective, rainy alley, smoke", "epic fantasy battle, dragons, dramatic sky"],
};

// ─────────────────────────────────────────────────────────────
// Visual placeholder ("forged" image) — generative SVG
// Deterministic-looking from seed; subtle, editorial, no slop.
// ─────────────────────────────────────────────────────────────
function ForgePlaceholder({ seed = 0, hue = 28, label = "" , size = 480 }) {
  // simple PRNG
  const rng = useMemo(() => {
    let s = seed >>> 0;
    return () => {
      s = (s * 1664525 + 1013904223) >>> 0;
      return s / 4294967296;
    };
  }, [seed]);

  const bands = useMemo(() => {
    const out = [];
    for (let i = 0; i < 14; i++) {
      out.push({
        y: rng() * 100,
        h: 0.5 + rng() * 3,
        o: 0.04 + rng() * 0.14,
        x: rng() * 100,
        w: 30 + rng() * 70,
      });
    }
    return out;
  }, [rng]);

  const dots = useMemo(() => {
    const out = [];
    for (let i = 0; i < 30; i++) {
      out.push({ x: rng() * 100, y: rng() * 100, r: 0.3 + rng() * 1.2, o: 0.05 + rng() * 0.25 });
    }
    return out;
  }, [rng]);

  const cx = 20 + rng() * 60;
  const cy = 30 + rng() * 40;
  const cr = 18 + rng() * 22;

  const id = `g${seed}`;
  return (
    <svg viewBox="0 0 100 100" width={size} height={size} preserveAspectRatio="xMidYMid slice" style={{ display: "block" }}>
      <defs>
        <linearGradient id={`bg-${id}`} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%"  stopColor={`oklch(0.22 0.05 ${hue})`} />
          <stop offset="60%" stopColor={`oklch(0.14 0.04 ${hue})`} />
          <stop offset="100%" stopColor={`oklch(0.09 0.03 ${hue})`} />
        </linearGradient>
        <radialGradient id={`glow-${id}`} cx="50%" cy="50%" r="50%">
          <stop offset="0%"  stopColor={`oklch(0.78 0.16 ${hue})`} stopOpacity="0.6" />
          <stop offset="60%" stopColor={`oklch(0.5  0.10 ${hue})`} stopOpacity="0.18"/>
          <stop offset="100%" stopColor="transparent" stopOpacity="0"/>
        </radialGradient>
        <pattern id={`grain-${id}`} width="2" height="2" patternUnits="userSpaceOnUse">
          <rect width="2" height="2" fill="transparent"/>
          <circle cx="1" cy="1" r="0.25" fill="#fff" opacity="0.04"/>
        </pattern>
      </defs>
      <rect width="100" height="100" fill={`url(#bg-${id})`} />
      <circle cx={cx} cy={cy} r={cr} fill={`url(#glow-${id})`} />
      {bands.map((b, i) => (
        <rect key={i} x={b.x} y={b.y} width={b.w} height={b.h} fill={`oklch(0.85 0.08 ${hue})`} opacity={b.o} />
      ))}
      {dots.map((d, i) => (
        <circle key={i} cx={d.x} cy={d.y} r={d.r} fill={`oklch(0.95 0.05 ${hue})`} opacity={d.o} />
      ))}
      <rect width="100" height="100" fill={`url(#grain-${id})`} />
      {label && (
        <g>
          <rect x="2" y="93" width="40" height="5" fill="rgba(0,0,0,0.5)" />
          <text x="4" y="96.6" fontSize="2.4" fill="#ede6d6" fontFamily="ui-monospace, monospace" letterSpacing="0.2">{label}</text>
        </g>
      )}
    </svg>
  );
}

// ─────────────────────────────────────────────────────────────
// Mini style preview (used inside chips)
// ─────────────────────────────────────────────────────────────
function StyleSwatch({ hue }) {
  return (
    <svg viewBox="0 0 40 40" width={40} height={40}>
      <defs>
        <linearGradient id={`s${hue}`} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%"  stopColor={`oklch(0.7 0.14 ${hue})`} />
          <stop offset="100%" stopColor={`oklch(0.32 0.08 ${hue})`} />
        </linearGradient>
      </defs>
      <rect width="40" height="40" rx="6" fill={`url(#s${hue})`} />
      <rect x="0" y="26" width="40" height="14" fill="rgba(0,0,0,0.35)" />
      <circle cx="28" cy="14" r="6" fill="rgba(255,255,255,0.18)" />
    </svg>
  );
}

// ─────────────────────────────────────────────────────────────
// Status bar / ticker
// ─────────────────────────────────────────────────────────────
function StatusBar({ vram, queue, time }) {
  return (
    <header className="topbar">
      <div className="brand">
        <span className="brand-mark">◐</span>
        <span className="brand-word">Yumira</span>
        <span className="brand-sub">Labs · Forge</span>
      </div>
      <nav className="topbar-nav">
        <a className="navlink is-active">Forge</a>
        <a className="navlink">Library</a>
        <a className="navlink">Training</a>
        <a className="navlink">Models</a>
      </nav>
      <div className="topbar-status">
        <span className="stat"><span className="stat-k">model</span><span className="stat-v">SD 1.5</span></span>
        <span className="stat"><span className="stat-k">gpu</span><span className="stat-v">RTX 3050</span></span>
        <span className="stat"><span className="stat-k">vram</span><span className="stat-v">{vram} MB</span></span>
        <span className="stat"><span className="stat-k">queue</span><span className="stat-v">{queue}</span></span>
        <span className="stat stat-time"><span className="dot"></span><span className="stat-v">{time}</span></span>
      </div>
    </header>
  );
}

// ─────────────────────────────────────────────────────────────
// Prompt block — the editorial hero
// ─────────────────────────────────────────────────────────────
function PromptBlock({ prompt, setPrompt, onForge, onSurprise, busy }) {
  const ref = useRef(null);
  return (
    <section className="prompt-block">
      <div className="prompt-meta">
        <span className="kbd">01</span>
        <span className="prompt-meta-label">PROMPT</span>
        <span className="prompt-meta-rule"></span>
        <span className="prompt-meta-hint">⏎ to forge</span>
      </div>
      <div className="prompt-field">
        <textarea
          ref={ref}
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              onForge();
            }
          }}
          placeholder="Describe what to forge —&#10;a lone astronaut on Mars, dust storm approaching, cinematic"
          rows={3}
        />
        <div className="prompt-counter">{prompt.length}<span> chars</span></div>
      </div>
      <div className="prompt-actions">
        <button className="btn btn-primary" onClick={onForge} disabled={busy}>
          {busy ? <><span className="spinner"></span> forging</> : <>Forge<span className="btn-arrow">↗</span></>}
        </button>
        <button className="btn btn-ghost" onClick={onSurprise} disabled={busy}>Surprise me</button>
        <span className="prompt-flex"></span>
      </div>
    </section>
  );
}

// ─────────────────────────────────────────────────────────────
// Style picker
// ─────────────────────────────────────────────────────────────
function StylePicker({ value, onChange }) {
  return (
    <section className="style-block">
      <div className="section-head">
        <span className="kbd">02</span>
        <span className="section-label">STYLE</span>
        <span className="section-rule"></span>
        <span className="section-hint">{STYLES.find(s => s.key === value)?.sub}</span>
      </div>
      <div className="style-grid">
        {STYLES.map((s) => (
          <button
            key={s.key}
            className={`style-chip ${value === s.key ? "is-active" : ""}`}
            onClick={() => onChange(s.key)}
          >
            <span className="style-swatch"><StyleSwatch hue={s.hue} /></span>
            <span className="style-text">
              <span className="style-label">{s.label}</span>
              <span className="style-tag">{s.tag}</span>
            </span>
            {value === s.key && <span className="style-check">●</span>}
          </button>
        ))}
      </div>
    </section>
  );
}

// ─────────────────────────────────────────────────────────────
// Quality preset segmented control
// ─────────────────────────────────────────────────────────────
function QualityBlock({ value, onChange }) {
  return (
    <section className="quality-block">
      <div className="section-head">
        <span className="kbd">03</span>
        <span className="section-label">QUALITY</span>
        <span className="section-rule"></span>
      </div>
      <div className="seg">
        {QUALITY.map((q) => (
          <button key={q.key} className={`seg-btn ${value === q.key ? "is-active" : ""}`} onClick={() => onChange(q.key)}>
            <span className="seg-name">{q.key}</span>
            <span className="seg-meta">{q.steps} steps · {q.sec}</span>
          </button>
        ))}
      </div>
    </section>
  );
}

// ─────────────────────────────────────────────────────────────
// Advanced — collapsible
// ─────────────────────────────────────────────────────────────
function AdvancedBlock({ adv, setAdv }) {
  const [open, setOpen] = useState(false);
  return (
    <section className={`adv-block ${open ? "is-open" : ""}`}>
      <button className="adv-toggle" onClick={() => setOpen(o => !o)}>
        <span className="kbd">04</span>
        <span className="section-label">ADVANCED</span>
        <span className="section-rule"></span>
        <span className="adv-chev">{open ? "−" : "+"}</span>
      </button>
      {open && (
        <div className="adv-grid">
          <div className="adv-row">
            <label>Seed</label>
            <div className="adv-input-wrap">
              <input
                type="number"
                value={adv.seed}
                onChange={(e) => setAdv({ ...adv, seed: parseInt(e.target.value || -1) })}
              />
              <button className="adv-mini" onClick={() => setAdv({ ...adv, seed: -1 })}>random</button>
            </div>
          </div>
          <div className="adv-row">
            <label>Inference steps <span className="num">{adv.steps}</span></label>
            <input type="range" min="4" max="30" value={adv.steps} onChange={(e) => setAdv({ ...adv, steps: parseInt(e.target.value) })} />
          </div>
          <div className="adv-row">
            <label>CFG scale <span className="num">{adv.cfg.toFixed(1)}</span></label>
            <input type="range" min="1" max="15" step="0.5" value={adv.cfg} onChange={(e) => setAdv({ ...adv, cfg: parseFloat(e.target.value) })} />
          </div>
          <div className="adv-row adv-row-toggles">
            <button
              className={`pill ${adv.lcm ? "on" : ""}`}
              onClick={() => setAdv({ ...adv, lcm: !adv.lcm })}
            >
              <span className="pill-dot"></span> LCM fast mode <span className="pill-meta">4 steps</span>
            </button>
            <button
              className={`pill ${adv.enhance ? "on" : ""}`}
              onClick={() => setAdv({ ...adv, enhance: !adv.enhance })}
            >
              <span className="pill-dot"></span> Auto-enhance prompt
            </button>
            <button
              className={`pill ${adv.adetailer ? "on" : ""}`}
              onClick={() => setAdv({ ...adv, adetailer: !adv.adetailer })}
            >
              <span className="pill-dot"></span> ADetailer (faces)
            </button>
            <button
              className={`pill ${adv.faceRestore ? "on" : ""}`}
              onClick={() => setAdv({ ...adv, faceRestore: !adv.faceRestore })}
            >
              <span className="pill-dot"></span> Face restore <span className="pill-meta">{adv.faceMethod}</span>
            </button>
          </div>
          <div className="adv-row">
            <label>Negative prompt</label>
            <textarea
              value={adv.negative}
              onChange={(e) => setAdv({ ...adv, negative: e.target.value })}
              rows={2}
            />
          </div>
        </div>
      )}
    </section>
  );
}

// ─────────────────────────────────────────────────────────────
// Output canvas + metadata
// ─────────────────────────────────────────────────────────────
function OutputCanvas({ state, hue, onRoll, onClear }) {
  const { phase, seed, prompt, style, steps, cfg, label } = state;
  return (
    <section className="output-block">
      <div className="output-stage">
        <div className="output-frame">
          {phase === "idle" && (
            <div className="output-empty">
              <div className="output-empty-mark">
                <svg width="44" height="44" viewBox="0 0 44 44">
                  <circle cx="22" cy="22" r="21" fill="none" stroke="currentColor" strokeOpacity=".25" strokeDasharray="2 3"/>
                  <circle cx="22" cy="22" r="3" fill="currentColor" fillOpacity=".4"/>
                </svg>
              </div>
              <div className="output-empty-text">
                <span>The forge is cold.</span>
                <span className="muted">Write a prompt and press <kbd className="inlinekbd">⏎</kbd></span>
              </div>
            </div>
          )}
          {phase === "busy" && (
            <div className="output-busy">
              <div className="busy-grid">
                {Array.from({ length: 8 }).map((_, i) => (
                  <span key={i} style={{ animationDelay: `${i * 0.08}s` }}></span>
                ))}
              </div>
              <div className="busy-meta">
                <span>FORGING</span>
                <span className="mono">step {steps}/{steps} · cfg {cfg.toFixed(1)}</span>
              </div>
            </div>
          )}
          {phase === "done" && (
            <div className="output-image">
              <ForgePlaceholder seed={seed} hue={hue} label={`SEED ${seed}`} size={520} />
              <div className="corner tl">YUM-{String(seed).padStart(6, "0").slice(-6)}</div>
              <div className="corner tr">512 × 512</div>
              <div className="corner bl">{style}</div>
              <div className="corner br">{steps}s · cfg {cfg.toFixed(1)}</div>
            </div>
          )}
        </div>
      </div>

      <div className="output-meta">
        <div className="meta-row">
          <span className="meta-k">prompt</span>
          <span className="meta-v meta-v-wrap">{prompt || "—"}</span>
        </div>
        <div className="meta-row">
          <span className="meta-k">style</span>
          <span className="meta-v">{label || "—"}</span>
          <span className="meta-k">seed</span>
          <span className="meta-v mono">{phase === "done" ? seed : "—"}</span>
        </div>
        <div className="meta-row">
          <span className="meta-k">steps</span>
          <span className="meta-v mono">{phase === "done" ? steps : "—"}</span>
          <span className="meta-k">cfg</span>
          <span className="meta-v mono">{phase === "done" ? cfg.toFixed(1) : "—"}</span>
          <span className="meta-k">model</span>
          <span className="meta-v mono">SD1.5</span>
        </div>
      </div>

      <div className="output-actions">
        <button className="btn btn-line" disabled={phase !== "done"} onClick={onRoll}>
          <span className="ico">↻</span> Reroll
        </button>
        <button className="btn btn-line" disabled={phase !== "done"}>
          <span className="ico">↧</span> Download
        </button>
        <button className="btn btn-line" disabled={phase !== "done"}>
          <span className="ico">★</span> Save to library
        </button>
        <button className="btn btn-line btn-line-quiet" disabled={phase !== "done"} onClick={onClear}>
          Clear
        </button>
      </div>
    </section>
  );
}

// ─────────────────────────────────────────────────────────────
// Recent strip
// ─────────────────────────────────────────────────────────────
function RecentStrip({ history, onPick, currentHue }) {
  if (history.length === 0) return null;
  return (
    <section className="recent-block">
      <div className="section-head">
        <span className="section-label">RECENT</span>
        <span className="section-rule"></span>
        <span className="section-hint">{history.length} this session</span>
      </div>
      <div className="recent-grid">
        {history.map((h, i) => (
          <button key={h.id} className="recent-card" onClick={() => onPick(h)}>
            <div className="recent-thumb">
              <ForgePlaceholder seed={h.seed} hue={h.hue} size={140} />
            </div>
            <div className="recent-meta">
              <div className="recent-label">{h.label}</div>
              <div className="recent-prompt">{h.prompt.slice(0, 38)}{h.prompt.length > 38 ? "…" : ""}</div>
              <div className="recent-tags mono">#{h.seed} · {h.steps}s</div>
            </div>
          </button>
        ))}
      </div>
    </section>
  );
}

// ─────────────────────────────────────────────────────────────
// Examples row
// ─────────────────────────────────────────────────────────────
function Examples({ style, setPrompt }) {
  const list = EXAMPLES[style] || EXAMPLES.none;
  return (
    <div className="examples">
      <span className="examples-label">try</span>
      {list.map((ex, i) => (
        <button key={i} className="example-chip" onClick={() => setPrompt(ex)} title={ex}>
          {ex.length > 42 ? ex.slice(0, 42) + "…" : ex}
        </button>
      ))}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Clock
// ─────────────────────────────────────────────────────────────
function useClock() {
  const [t, setT] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setT(new Date()), 1000);
    return () => clearInterval(id);
  }, []);
  return t.toTimeString().slice(0, 8);
}

// ─────────────────────────────────────────────────────────────
// Main app
// ─────────────────────────────────────────────────────────────
const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "theme": "obsidian",
  "accent": "#E8714E",
  "density": "comfy",
  "display": "instrument"
}/*EDITMODE-END*/;

function App() {
  const [t, setTweak] = useTweaks(TWEAK_DEFAULTS);
  const [prompt, setPrompt] = useState("a lone astronaut on Mars, dust storm approaching, cinematic");
  const [style, setStyle] = useState("movie_poster");
  const [preset, setPreset] = useState("Balanced");
  const [adv, setAdv] = useState({
    seed: -1, steps: 20, cfg: 7.5, lcm: false, enhance: true,
    adetailer: false, faceRestore: false, faceMethod: "gfpgan",
    negative: "lowres, bad anatomy, text, watermark, blurry, deformed",
  });
  const [output, setOutput] = useState({ phase: "idle", seed: 0, prompt: "", style: "", steps: 20, cfg: 7.5, label: "" });
  const [history, setHistory] = useState([]);
  const [vram, setVram] = useState(2840);
  const clock = useClock();

  // Apply preset when changed
  useEffect(() => {
    const q = QUALITY.find(q => q.key === preset);
    if (q) setAdv(a => ({ ...a, steps: q.steps, cfg: q.cfg }));
  }, [preset]);

  // Theme application
  useEffect(() => {
    document.documentElement.dataset.theme = t.theme;
    document.documentElement.dataset.density = t.density;
    document.documentElement.dataset.display = t.display;
    document.documentElement.style.setProperty("--accent", t.accent);
  }, [t.theme, t.density, t.display, t.accent]);

  const currentStyle = STYLES.find(s => s.key === style);
  const accentHue = useMemo(() => {
    // parse hex to hue (rough)
    const hex = (t.accent || "#E8714E").replace("#", "");
    const r = parseInt(hex.slice(0,2), 16) / 255;
    const g = parseInt(hex.slice(2,4), 16) / 255;
    const b = parseInt(hex.slice(4,6), 16) / 255;
    const max = Math.max(r,g,b), min = Math.min(r,g,b);
    let h = 0; const d = max - min;
    if (d !== 0) {
      if (max === r) h = ((g - b) / d) % 6;
      else if (max === g) h = (b - r) / d + 2;
      else h = (r - g) / d + 4;
      h *= 60; if (h < 0) h += 360;
    }
    return h;
  }, [t.accent]);

  const hueForOutput = currentStyle ? currentStyle.hue : accentHue;

  function doForge() {
    if (!prompt.trim()) return;
    const seed = adv.seed >= 0 ? adv.seed : Math.floor(Math.random() * 4294967295);
    setOutput({ phase: "busy", seed, prompt, style, steps: adv.steps, cfg: adv.cfg, label: currentStyle.label });
    setVram(v => Math.min(7800, v + Math.floor(Math.random() * 400 + 100)));
    setTimeout(() => {
      setOutput({ phase: "done", seed, prompt, style, steps: adv.steps, cfg: adv.cfg, label: currentStyle.label });
      setHistory(h => [{ id: Date.now(), seed, prompt, style, steps: adv.steps, cfg: adv.cfg, label: currentStyle.label, hue: hueForOutput }, ...h].slice(0, 8));
    }, 1600);
  }

  function doSurprise() {
    const list = EXAMPLES[style] || EXAMPLES.none;
    setPrompt(list[Math.floor(Math.random() * list.length)]);
  }

  function doRoll() {
    setAdv(a => ({ ...a, seed: -1 }));
    setTimeout(doForge, 50);
  }

  return (
    <>
      <StatusBar vram={vram} queue={output.phase === "busy" ? "1 active" : "idle"} time={clock} />

      <main className="shell">
        <div className="hero">
          <div className="hero-eyebrow">
            <span className="dot"></span>
            <span>image foundry</span>
            <span className="muted">/</span>
            <span className="muted">v2.1.0</span>
          </div>
          <h1 className="hero-title">
            Forge images <em>from</em> text.
          </h1>
          <p className="hero-sub">
            Seven trained styles. One local pipeline. Type a sentence, choose a style, hold the line.
          </p>
        </div>

        <div className="grid">
          <div className="col col-controls">
            <PromptBlock prompt={prompt} setPrompt={setPrompt} onForge={doForge} onSurprise={doSurprise} busy={output.phase === "busy"} />
            <Examples style={style} setPrompt={setPrompt} />
            <StylePicker value={style} onChange={setStyle} />
            <QualityBlock value={preset} onChange={setPreset} />
            <AdvancedBlock adv={adv} setAdv={setAdv} />
          </div>
          <div className="col col-output">
            <OutputCanvas
              state={output}
              hue={hueForOutput}
              onRoll={doRoll}
              onClear={() => setOutput({ phase: "idle", seed: 0, prompt: "", style: "", steps: 20, cfg: 7.5, label: "" })}
            />
          </div>
        </div>

        <RecentStrip history={history} currentHue={hueForOutput} onPick={(h) => {
          setPrompt(h.prompt);
          setStyle(h.style);
          setOutput({ phase: "done", seed: h.seed, prompt: h.prompt, style: h.style, steps: h.steps, cfg: h.cfg, label: h.label });
        }} />

        <footer className="footer">
          <span>Yumira Labs</span>
          <span className="muted">·</span>
          <span className="muted">SD 1.5 + LoRA</span>
          <span className="muted">·</span>
          <span className="muted">RTX 3050 optimized</span>
          <span className="footer-flex"></span>
          <span className="mono muted">localhost:7860</span>
        </footer>
      </main>

      <TweaksPanel title="Tweaks">
        <TweakSection label="Theme">
          <TweakRadio value={t.theme} onChange={(v) => setTweak("theme", v)} options={[
            { value: "obsidian", label: "Obsidian" },
            { value: "cream",    label: "Cream" },
            { value: "carbon",   label: "Carbon" },
          ]} />
        </TweakSection>
        <TweakSection label="Accent">
          <TweakColor value={t.accent} onChange={(v) => setTweak("accent", v)}
            options={["#E8714E", "#C49A4E", "#5B8C7B", "#7A6EE0", "#D6443C"]} />
        </TweakSection>
        <TweakSection label="Display font">
          <TweakRadio value={t.display} onChange={(v) => setTweak("display", v)} options={[
            { value: "instrument", label: "Instrument" },
            { value: "serif",      label: "Serif" },
            { value: "sans",       label: "Sans" },
          ]} />
        </TweakSection>
        <TweakSection label="Density">
          <TweakRadio value={t.density} onChange={(v) => setTweak("density", v)} options={[
            { value: "comfy", label: "Comfy" },
            { value: "dense", label: "Dense" },
          ]} />
        </TweakSection>
      </TweaksPanel>
    </>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
