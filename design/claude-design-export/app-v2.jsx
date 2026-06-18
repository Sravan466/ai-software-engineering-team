/* global React, ReactDOM, FlowFieldBG, TweaksPanel, useTweaks, TweakSection, TweakRadio, TweakColor */
const { useState, useEffect, useRef, useMemo, useCallback } = React;

// ─────────────────────────────────────────────────────────────
// Data
// ─────────────────────────────────────────────────────────────
const STYLES = [
  { key: "none",         label: "FREE",        sub: "NO LORA",       num: "01", hue: 28 },
  { key: "anime",        label: "ANIME",       sub: "STYLISED",      num: "02", hue: 320 },
  { key: "scientific",   label: "SCIENTIFIC",  sub: "DIAGRAMMATIC",  num: "03", hue: 200 },
  { key: "character",    label: "CHARACTER",   sub: "CONCEPT ART",   num: "04", hue: 18 },
  { key: "ghibli",       label: "GHIBLI",      sub: "PAINTERLY",     num: "05", hue: 150 },
  { key: "realistic",    label: "REALISTIC",   sub: "PHOTO / 8K",    num: "06", hue: 40 },
  { key: "movie_poster", label: "POSTER",      sub: "CINEMATIC",     num: "07", hue: 0 },
];

const QUALITY = [
  { key: "Fast",     steps: 15, cfg: 7.0, sec: 6  },
  { key: "Balanced", steps: 20, cfg: 7.5, sec: 9  },
  { key: "Quality",  steps: 25, cfg: 7.5, sec: 14 },
  { key: "Max",      steps: 30, cfg: 8.0, sec: 20 },
];

const EXAMPLES = {
  none:        ["cabin in the mountains, snow, warm light in windows", "futuristic cityscape at night, neon, rain", "a peaceful garden with a stone fountain"],
  anime:       ["a girl with blue hair, detailed, colorful", "anime warrior with glowing sword, dynamic pose", "cherry blossom landscape, sunset"],
  scientific:  ["detailed illustration of a human heart, labeled diagram", "botanical illustration of a rose, accurate", "DNA double helix, molecular detail"],
  character:   ["fantasy warrior, full body, detailed armor", "cyberpunk assassin, neon city, full plate", "medieval knight, dual-wielding, hooded"],
  ghibli:      ["magical forest with spirits and glowing mushrooms", "flying castle above the clouds, birds", "seaside village, red rooftops"],
  realistic:   ["portrait of a woman, freckles, soft bokeh", "mountain lake at sunrise, mist, reflections", "vintage car on a European street, golden hour"],
  movie_poster:["lone astronaut on Mars, dust storm approaching", "noir detective in rainy alley, smoke", "epic fantasy battle, dragons, dramatic sky"],
};

// ─────────────────────────────────────────────────────────────
// Hooks
// ─────────────────────────────────────────────────────────────
function useTickingNumber(initial, opts = {}) {
  const { min = 0, max = 100, jitter = 30, interval = 1400 } = opts;
  const [v, setV] = useState(initial);
  useEffect(() => {
    const id = setInterval(() => {
      setV((cur) => {
        const next = cur + (Math.random() - 0.5) * jitter;
        return Math.max(min, Math.min(max, Math.round(next)));
      });
    }, interval);
    return () => clearInterval(id);
  }, [min, max, jitter, interval]);
  return v;
}

function useClock() {
  const [t, setT] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setT(new Date()), 1000);
    return () => clearInterval(id);
  }, []);
  return t;
}

function useMouse() {
  const [m, setM] = useState({ x: -100, y: -100 });
  useEffect(() => {
    const h = (e) => setM({ x: e.clientX, y: e.clientY });
    window.addEventListener("pointermove", h);
    return () => window.removeEventListener("pointermove", h);
  }, []);
  return m;
}

// Smooth-follow cursor trail
function CustomCursor() {
  const dotRef = useRef(null);
  const ringRef = useRef(null);
  const overRef = useRef(false);
  useEffect(() => {
    let mx = 0, my = 0, rx = 0, ry = 0;
    let raf;
    const onMove = (e) => { mx = e.clientX; my = e.clientY; };
    const onDown = (el) => () => { if (ringRef.current) ringRef.current.classList.add("is-down"); };
    const onUp = () => { if (ringRef.current) ringRef.current.classList.remove("is-down"); };
    const onOver = (e) => {
      const t = e.target.closest("[data-cursor]");
      if (t) {
        overRef.current = true;
        ringRef.current?.classList.add("is-over");
        ringRef.current?.setAttribute("data-label", t.dataset.cursor);
      } else {
        overRef.current = false;
        ringRef.current?.classList.remove("is-over");
        ringRef.current?.removeAttribute("data-label");
      }
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointermove", onOver);
    window.addEventListener("pointerdown", onDown());
    window.addEventListener("pointerup", onUp);

    function loop() {
      rx += (mx - rx) * 0.16;
      ry += (my - ry) * 0.16;
      if (dotRef.current) dotRef.current.style.transform = `translate3d(${mx}px, ${my}px, 0)`;
      if (ringRef.current) ringRef.current.style.transform = `translate3d(${rx}px, ${ry}px, 0)`;
      raf = requestAnimationFrame(loop);
    }
    raf = requestAnimationFrame(loop);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointermove", onOver);
      window.removeEventListener("pointerdown", onDown());
      window.removeEventListener("pointerup", onUp);
    };
  }, []);
  return (
    <>
      <div ref={dotRef} className="cursor-dot" />
      <div ref={ringRef} className="cursor-ring" />
    </>
  );
}

// ─────────────────────────────────────────────────────────────
// Marquees
// ─────────────────────────────────────────────────────────────
function Marquee({ items, dir = "left", speed = 60, divider = "✦" }) {
  return (
    <div className={`marquee marquee-${dir}`} style={{ "--speed": `${speed}s` }}>
      <div className="marquee-track">
        {Array.from({ length: 3 }).map((_, k) => (
          <span key={k} className="marquee-set">
            {items.map((t, i) => (
              <span key={i} className="marquee-item">
                <span>{t}</span>
                <span className="marquee-div">{divider}</span>
              </span>
            ))}
          </span>
        ))}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Big animated wordmark with circular glyph
// ─────────────────────────────────────────────────────────────
function WordmarkHero() {
  const chars = ["Y", "U", "M", "I", "R", "A"];
  return (
    <div className="wordmark">
      {chars.map((c, i) => (
        <span key={i} className="wm-char" style={{ "--i": i }}>{c}</span>
      ))}
      <span className="wm-glyph" aria-hidden="true">
        <svg viewBox="0 0 80 80" width="100%" height="100%">
          <circle cx="40" cy="40" r="36" fill="none" stroke="currentColor" strokeWidth="1" strokeDasharray="3 4" className="wm-orbit" />
          <circle cx="40" cy="40" r="22" fill="currentColor" />
          <circle cx="40" cy="40" r="22" fill="#000" style={{ mixBlendMode: "destination-out" }} />
        </svg>
      </span>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Magnetic Forge button
// ─────────────────────────────────────────────────────────────
function ForgeButton({ onClick, busy, disabled }) {
  const wrapRef = useRef(null);
  const labelRef = useRef(null);
  useEffect(() => {
    const wrap = wrapRef.current;
    if (!wrap) return;
    function onMove(e) {
      const r = wrap.getBoundingClientRect();
      const cx = r.left + r.width / 2;
      const cy = r.top + r.height / 2;
      const dx = e.clientX - cx;
      const dy = e.clientY - cy;
      const d = Math.hypot(dx, dy);
      const strength = Math.max(0, 1 - d / 220);
      const mx = (dx / r.width) * 26 * strength;
      const my = (dy / r.height) * 26 * strength;
      wrap.style.transform = `translate(${mx}px, ${my}px)`;
      if (labelRef.current) labelRef.current.style.transform = `translate(${mx * 0.4}px, ${my * 0.4}px)`;
    }
    function onLeave() {
      wrap.style.transform = "";
      if (labelRef.current) labelRef.current.style.transform = "";
    }
    window.addEventListener("pointermove", onMove);
    wrap.addEventListener("pointerleave", onLeave);
    return () => {
      window.removeEventListener("pointermove", onMove);
      wrap.removeEventListener("pointerleave", onLeave);
    };
  }, []);
  return (
    <button
      ref={wrapRef}
      className={`forge-btn ${busy ? "is-busy" : ""}`}
      onClick={onClick}
      disabled={disabled}
      data-cursor="FORGE"
    >
      <span className="forge-btn-inner">
        <span className="forge-btn-ring"></span>
        <span ref={labelRef} className="forge-btn-label">
          {busy ? <span className="forge-spinner"></span> : <>FORGE<span className="forge-btn-arrow">↗</span></>}
        </span>
        <span className="forge-btn-stitch"></span>
      </span>
    </button>
  );
}

// ─────────────────────────────────────────────────────────────
// Style deck — 3D tilt card carousel
// ─────────────────────────────────────────────────────────────
function StyleDeck({ value, onChange }) {
  return (
    <div className="deck">
      {STYLES.map((s) => (
        <button
          key={s.key}
          className={`deck-card ${value === s.key ? "is-active" : ""}`}
          onClick={() => onChange(s.key)}
          data-cursor={`SELECT ${s.label}`}
        >
          <span className="deck-num">{s.num}</span>
          <div className="deck-preview">
            <DeckPreview hue={s.hue} k={s.key} />
          </div>
          <div className="deck-text">
            <span className="deck-label">{s.label}</span>
            <span className="deck-sub">{s.sub}</span>
          </div>
          <span className="deck-arrow">→</span>
        </button>
      ))}
    </div>
  );
}

function DeckPreview({ hue, k }) {
  // unique abstract preview per style
  const seed = k.length + hue;
  return (
    <svg viewBox="0 0 100 100" width="100%" height="100%">
      <defs>
        <linearGradient id={`dp-${k}`} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%"  stopColor={`oklch(0.72 0.18 ${hue})`} />
          <stop offset="100%" stopColor={`oklch(0.28 0.08 ${hue})`} />
        </linearGradient>
      </defs>
      <rect width="100" height="100" fill={`url(#dp-${k})`} />
      <g style={{ mixBlendMode: "overlay" }}>
        {Array.from({ length: 14 }).map((_, i) => {
          const y = (i / 14) * 100 + ((seed * 3) % 5);
          return <rect key={i} x="0" y={y} width="100" height="0.6" fill="white" opacity="0.18" />;
        })}
      </g>
      <circle cx="65" cy="40" r="18" fill="rgba(255,255,255,0.18)" />
      <rect x="0" y="68" width="100" height="32" fill="rgba(0,0,0,0.35)" />
    </svg>
  );
}

// ─────────────────────────────────────────────────────────────
// Result frame with scan-line generation
// ─────────────────────────────────────────────────────────────
function ResultFrame({ state, hue }) {
  const { phase, seed, steps, cfg, label, prompt } = state;
  return (
    <div className="resultframe">
      <div className="rf-corners" aria-hidden="true">
        <span className="c c-tl"></span>
        <span className="c c-tr"></span>
        <span className="c c-bl"></span>
        <span className="c c-br"></span>
      </div>
      <div className="rf-stage">
        {phase === "idle" && (
          <div className="rf-idle">
            <svg viewBox="0 0 80 80" width="80" height="80" className="rf-idle-mark">
              <circle cx="40" cy="40" r="36" fill="none" stroke="currentColor" strokeWidth="1" strokeOpacity=".35" strokeDasharray="2 4">
                <animateTransform attributeName="transform" type="rotate" from="0 40 40" to="360 40 40" dur="22s" repeatCount="indefinite" />
              </circle>
              <circle cx="40" cy="40" r="24" fill="none" stroke="currentColor" strokeWidth="1" strokeOpacity=".18" />
              <circle cx="40" cy="40" r="3" fill="currentColor" />
            </svg>
            <div className="rf-idle-text">
              <em>awaiting input</em>
              <small>type a prompt and strike <span className="kbd-inline">⏎</span></small>
            </div>
          </div>
        )}
        {phase === "busy" && <BusyState steps={steps} cfg={cfg} />}
        {phase === "done" && (
          <div className="rf-done">
            <ResultArt seed={seed} hue={hue} />
            <span className="rf-scan rf-scan-out"></span>
          </div>
        )}
      </div>
      {phase === "done" && (
        <div className="rf-tags">
          <span className="tag mono">SEED · {seed}</span>
          <span className="tag mono">{steps}STP / CFG{cfg.toFixed(1)}</span>
          <span className="tag mono">{label.toUpperCase()}</span>
          <span className="tag mono">512²</span>
        </div>
      )}
    </div>
  );
}

function ResultArt({ seed, hue }) {
  const id = `r${seed}`;
  const rng = useMemo(() => {
    let s = (seed >>> 0) || 1;
    return () => (s = (s * 1664525 + 1013904223) >>> 0) / 4294967296;
  }, [seed]);
  const blobs = useMemo(() => Array.from({ length: 5 }, () => ({
    cx: rng() * 100, cy: rng() * 100, r: 10 + rng() * 40, op: 0.15 + rng() * 0.35, hueOff: rng() * 30 - 15,
  })), [rng]);
  const lines = useMemo(() => Array.from({ length: 36 }, () => ({
    x: rng() * 100, y: rng() * 100, w: 10 + rng() * 80, h: 0.3 + rng() * 1.2, op: 0.04 + rng() * 0.16,
  })), [rng]);
  return (
    <svg viewBox="0 0 100 100" width="100%" height="100%" preserveAspectRatio="xMidYMid slice">
      <defs>
        <linearGradient id={`bg-${id}`} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%"  stopColor={`oklch(0.22 0.05 ${hue})`} />
          <stop offset="50%" stopColor={`oklch(0.13 0.04 ${hue + 10})`} />
          <stop offset="100%" stopColor={`oklch(0.08 0.03 ${hue - 10})`} />
        </linearGradient>
        <radialGradient id={`hl-${id}`}>
          <stop offset="0%"  stopColor={`oklch(0.85 0.16 ${hue})`} stopOpacity="0.55" />
          <stop offset="60%" stopColor={`oklch(0.5  0.10 ${hue})`} stopOpacity="0.18" />
          <stop offset="100%" stopColor="transparent" stopOpacity="0" />
        </radialGradient>
      </defs>
      <rect width="100" height="100" fill={`url(#bg-${id})`} />
      {blobs.map((b, i) => (
        <circle key={i} cx={b.cx} cy={b.cy} r={b.r} fill={`url(#hl-${id})`} opacity={b.op} />
      ))}
      {lines.map((l, i) => (
        <rect key={i} x={l.x} y={l.y} width={l.w} height={l.h} fill={`oklch(0.92 0.07 ${hue})`} opacity={l.op} />
      ))}
    </svg>
  );
}

function BusyState({ steps, cfg }) {
  const [pct, setPct] = useState(0);
  const [stp, setStp] = useState(0);
  useEffect(() => {
    let p = 0;
    const id = setInterval(() => {
      p += 4 + Math.random() * 6;
      if (p > 100) p = 100;
      setPct(p);
      setStp(Math.floor((p / 100) * steps));
      if (p >= 100) clearInterval(id);
    }, 60);
    return () => clearInterval(id);
  }, [steps]);
  return (
    <div className="rf-busy">
      <div className="rf-busy-grid" aria-hidden="true">
        {Array.from({ length: 8 * 8 }).map((_, i) => (
          <span key={i} style={{ animationDelay: `${(i % 8) * 0.05 + Math.floor(i / 8) * 0.07}s` }}></span>
        ))}
      </div>
      <span className="rf-scan"></span>
      <div className="rf-busy-meta mono">
        <div className="rf-busy-line">
          <span>FORGING</span>
          <span>{pct.toFixed(0).padStart(3, "0")}%</span>
        </div>
        <div className="rf-busy-bar"><span style={{ width: `${pct}%` }} /></div>
        <div className="rf-busy-line subtle">
          <span>STEP {stp}/{steps}</span>
          <span>CFG {cfg.toFixed(1)}</span>
        </div>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Telemetry column
// ─────────────────────────────────────────────────────────────
function Telemetry({ accent }) {
  const vram = useTickingNumber(2840, { min: 2200, max: 7600, jitter: 280, interval: 1800 });
  const gpu = useTickingNumber(38, { min: 12, max: 96, jitter: 22, interval: 900 });
  const fps = useTickingNumber(60, { min: 56, max: 60, jitter: 4, interval: 500 });
  const tokens = useTickingNumber(48128, { min: 30000, max: 60000, jitter: 2400, interval: 1300 });
  const items = [
    { k: "VRAM",   v: `${vram} MB`,        b: vram / 8000 },
    { k: "GPU",    v: `${gpu}%`,            b: gpu / 100 },
    { k: "FPS",    v: `${fps}`,             b: fps / 60 },
    { k: "TOKENS", v: tokens.toLocaleString(), b: tokens / 60000 },
  ];
  return (
    <div className="telemetry">
      {items.map((it) => (
        <div key={it.k} className="telem-row">
          <div className="telem-head">
            <span className="telem-k">{it.k}</span>
            <span className="telem-v">{it.v}</span>
          </div>
          <div className="telem-bar"><span style={{ width: `${it.b * 100}%`, background: accent }}></span></div>
        </div>
      ))}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Big digital clock
// ─────────────────────────────────────────────────────────────
function BigClock() {
  const t = useClock();
  const hh = String(t.getHours()).padStart(2, "0");
  const mm = String(t.getMinutes()).padStart(2, "0");
  const ss = String(t.getSeconds()).padStart(2, "0");
  return (
    <div className="bigclock mono">
      <span>{hh}</span><span className="bc-sep">:</span><span>{mm}</span><span className="bc-sep">:</span><span className="bc-ms">{ss}</span>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Main
// ─────────────────────────────────────────────────────────────
const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "accent": "#FF6E3D",
  "accent2": "#D4FF3A",
  "cursor": true,
  "field": true
}/*EDITMODE-END*/;

function App() {
  const [t, setTweak] = useTweaks(TWEAK_DEFAULTS);
  const [prompt, setPrompt] = useState("a lone astronaut on Mars, dust storm approaching, cinematic");
  const [style, setStyle] = useState("movie_poster");
  const [preset, setPreset] = useState("Balanced");
  const [seed, setSeed] = useState(-1);
  const [output, setOutput] = useState({ phase: "idle", seed: 0, prompt: "", style: "", steps: 20, cfg: 7.5, label: "" });
  const [history, setHistory] = useState([]);
  const currentStyle = STYLES.find(s => s.key === style);
  const q = QUALITY.find(qq => qq.key === preset);

  useEffect(() => {
    document.documentElement.style.setProperty("--accent", t.accent);
    document.documentElement.style.setProperty("--accent-2", t.accent2);
    document.documentElement.dataset.cursor = t.cursor ? "on" : "off";
  }, [t.accent, t.accent2, t.cursor]);

  function doForge() {
    if (!prompt.trim() || output.phase === "busy") return;
    const sd = seed >= 0 ? seed : Math.floor(Math.random() * 4294967295);
    setOutput({ phase: "busy", seed: sd, prompt, style, steps: q.steps, cfg: q.cfg, label: currentStyle.label });
    setTimeout(() => {
      setOutput({ phase: "done", seed: sd, prompt, style, steps: q.steps, cfg: q.cfg, label: currentStyle.label });
      setHistory(h => [{ id: Date.now(), seed: sd, prompt, style, steps: q.steps, cfg: q.cfg, label: currentStyle.label, hue: currentStyle.hue }, ...h].slice(0, 12));
    }, 1800);
  }

  function doSurprise() {
    const list = EXAMPLES[style] || EXAMPLES.none;
    setPrompt(list[Math.floor(Math.random() * list.length)]);
  }

  const topItems = [
    "YUMIRA LABS",
    "IMAGE FOUNDRY",
    "SD 1.5 + 7 LORAS",
    "LOCAL · OFFLINE",
    "RTX 3050 · CUDA",
    "V2.1.0",
    "RAW OUTPUT",
    "NO TELEMETRY",
  ];
  const bottomItems = [
    `SEED — ${output.seed || "0000"}`,
    `STYLE — ${(currentStyle?.label || "FREE")}`,
    `STEPS — ${q.steps}`,
    `CFG — ${q.cfg.toFixed(1)}`,
    "OUTPUT 512²",
    "VAE FP16",
    "EULER A",
  ];

  return (
    <>
      {t.field && <FlowFieldBG accent={t.accent} accent2={t.accent2} />}
      {t.cursor && <CustomCursor />}

      <div className="page">
        {/* TOP MARQUEE */}
        <Marquee items={topItems} dir="left" speed={70} divider="◇" />

        {/* HERO */}
        <header className="hero">
          <div className="hero-meta">
            <span className="hero-meta-k">INDEX</span>
            <span className="hero-meta-v mono">F-001 / IMAGE FOUNDRY</span>
            <span className="hero-meta-rule"></span>
            <BigClock />
          </div>
          <WordmarkHero />
          <div className="hero-bottom">
            <div className="hero-tag">
              <span className="hero-tag-italic">a foundry, not a feed.</span>
              <span className="hero-tag-sub">type one sentence. press <span className="kbd-inline">⏎</span>. hold the line.</span>
            </div>
            <div className="hero-orbs">
              <span className="orb"><span></span></span>
              <span className="orb-text mono">SYSTEM READY · GPU IDLE</span>
            </div>
          </div>
        </header>

        {/* MAIN GRID */}
        <main className="main">
          {/* LEFT — PROMPT + CONTROLS */}
          <section className="panel panel-prompt">
            <div className="panel-head">
              <span className="panel-tag mono">// PROMPT_INPUT</span>
              <span className="panel-rule"></span>
              <span className="panel-tag mono subtle">{prompt.length} CHAR</span>
            </div>
            <div className="terminal">
              <span className="term-caret">▸</span>
              <textarea
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    doForge();
                  }
                }}
                placeholder="describe what to forge…"
                rows={3}
                data-cursor="TYPE"
              />
            </div>
            <div className="suggest">
              <span className="suggest-k mono">TRY</span>
              {(EXAMPLES[style] || EXAMPLES.none).slice(0, 3).map((ex, i) => (
                <button key={i} className="suggest-chip" onClick={() => setPrompt(ex)} data-cursor="USE">{ex.slice(0, 38)}{ex.length > 38 ? "…" : ""}</button>
              ))}
            </div>

            <div className="ctl-row">
              <div className="ctl-block">
                <div className="ctl-head"><span className="ctl-k mono">// QUALITY</span><span className="ctl-rule"></span></div>
                <div className="quality">
                  {QUALITY.map(qq => (
                    <button
                      key={qq.key}
                      className={`q-btn ${preset === qq.key ? "is-active" : ""}`}
                      onClick={() => setPreset(qq.key)}
                      data-cursor={qq.key.toUpperCase()}
                    >
                      <span className="q-name">{qq.key}</span>
                      <span className="q-meta mono">{qq.steps}s · ~{qq.sec}s</span>
                    </button>
                  ))}
                </div>
              </div>
              <div className="ctl-block">
                <div className="ctl-head"><span className="ctl-k mono">// SEED</span><span className="ctl-rule"></span></div>
                <div className="seed">
                  <input
                    type="number"
                    value={seed}
                    onChange={(e) => setSeed(parseInt(e.target.value || -1))}
                    className="seed-input mono"
                    data-cursor="SEED"
                  />
                  <button className="seed-rand" onClick={() => setSeed(-1)} data-cursor="RANDOM">↻ RAND</button>
                </div>
              </div>
            </div>

            <div className="forge-row">
              <ForgeButton onClick={doForge} busy={output.phase === "busy"} disabled={!prompt.trim()} />
              <button className="ghost-btn" onClick={doSurprise} data-cursor="SHUFFLE">
                <span>SURPRISE ME</span><span className="ghost-arrow">↺</span>
              </button>
            </div>
          </section>

          {/* RIGHT — RESULT */}
          <section className="panel panel-result">
            <div className="panel-head">
              <span className="panel-tag mono">// OUTPUT_FRAME</span>
              <span className="panel-rule"></span>
              <span className={`panel-tag mono ${output.phase === "busy" ? "blink" : ""}`}>{output.phase.toUpperCase()}</span>
            </div>
            <ResultFrame state={output} hue={currentStyle.hue} />
            <div className="panel-foot">
              <div className="prompt-echo">
                <span className="pe-k mono">PROMPT</span>
                <span className="pe-v">{output.prompt || prompt || "—"}</span>
              </div>
            </div>
          </section>
        </main>

        {/* STYLE DECK */}
        <section className="section">
          <div className="section-head">
            <span className="section-num mono">02</span>
            <h2 className="section-title">Choose a <em>style</em>.</h2>
            <span className="section-rule"></span>
            <span className="section-hint mono">{STYLES.length} LORAS · CACHED</span>
          </div>
          <StyleDeck value={style} onChange={setStyle} />
        </section>

        {/* TELEMETRY + RECENT */}
        <section className="section two-col">
          <div>
            <div className="section-head">
              <span className="section-num mono">03</span>
              <h2 className="section-title">Live <em>signal</em>.</h2>
              <span className="section-rule"></span>
            </div>
            <div className="panel">
              <Telemetry accent={t.accent} />
            </div>
          </div>
          <div>
            <div className="section-head">
              <span className="section-num mono">04</span>
              <h2 className="section-title">Recent <em>forges</em>.</h2>
              <span className="section-rule"></span>
              <span className="section-hint mono">{history.length} ITEMS</span>
            </div>
            <div className="recent">
              {history.length === 0 ? (
                <div className="recent-empty">
                  <span className="re-mark">∅</span>
                  <span>no forges yet — press FORGE to begin.</span>
                </div>
              ) : history.map((h) => (
                <button key={h.id} className="recent-card" data-cursor="OPEN"
                  onClick={() => setOutput({ phase: "done", seed: h.seed, prompt: h.prompt, style: h.style, steps: h.steps, cfg: h.cfg, label: h.label })}>
                  <div className="recent-thumb"><ResultArt seed={h.seed} hue={h.hue} /></div>
                  <div className="recent-meta">
                    <span className="rc-label mono">#{String(h.seed).slice(-6).padStart(6, "0")}</span>
                    <span className="rc-style mono">{h.label}</span>
                  </div>
                </button>
              ))}
            </div>
          </div>
        </section>

        {/* BOTTOM MARQUEE */}
        <Marquee items={bottomItems} dir="right" speed={50} divider="·" />

        <footer className="footer">
          <span className="mono">YUMIRA LABS — IMAGE FOUNDRY · V2.1.0</span>
          <span className="footer-flex"></span>
          <span className="mono subtle">localhost:7860 · CUDA 12.1 · TORCH 2.3</span>
        </footer>
      </div>

      <TweaksPanel title="Tweaks">
        <TweakSection label="Primary accent">
          <TweakColor value={t.accent} onChange={(v) => setTweak("accent", v)}
            options={["#FF6E3D", "#FF3B6B", "#7C5BFF", "#26E1A4", "#FFC845"]} />
        </TweakSection>
        <TweakSection label="Data accent">
          <TweakColor value={t.accent2} onChange={(v) => setTweak("accent2", v)}
            options={["#D4FF3A", "#94E5FF", "#FF7AC6", "#FFFFFF", "#FFD43A"]} />
        </TweakSection>
        <TweakSection label="Custom cursor">
          <TweakRadio value={t.cursor ? "on" : "off"} onChange={(v) => setTweak("cursor", v === "on")} options={[
            { value: "on", label: "On" }, { value: "off", label: "Off" }]} />
        </TweakSection>
        <TweakSection label="Flow field background">
          <TweakRadio value={t.field ? "on" : "off"} onChange={(v) => setTweak("field", v === "on")} options={[
            { value: "on", label: "On" }, { value: "off", label: "Off" }]} />
        </TweakSection>
      </TweaksPanel>
    </>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
