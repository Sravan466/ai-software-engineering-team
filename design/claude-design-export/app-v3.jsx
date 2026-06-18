/* global React, ReactDOM, FlowFieldBG, TweaksPanel, useTweaks, TweakSection, TweakRadio, TweakColor, DiffusionCanvas, SeedFingerprint */
const { useState, useEffect, useRef, useMemo, useCallback } = React;

const STYLES = [
  { key: "none",         label: "FREE",        sub: "NO LORA",       num: "01", hue: 28,  color: "G" },
  { key: "anime",        label: "ANIME",       sub: "STYLISED",      num: "02", hue: 320, color: "R" },
  { key: "scientific",   label: "SCIENTIFIC",  sub: "DIAGRAMMATIC",  num: "03", hue: 200, color: "B" },
  { key: "character",    label: "CHARACTER",   sub: "CONCEPT ART",   num: "04", hue: 18,  color: "R" },
  { key: "ghibli",       label: "GHIBLI",      sub: "PAINTERLY",     num: "05", hue: 150, color: "G" },
  { key: "realistic",    label: "REALISTIC",   sub: "PHOTO / 8K",    num: "06", hue: 40,  color: "G" },
  { key: "movie_poster", label: "POSTER",      sub: "CINEMATIC",     num: "07", hue: 0,   color: "R" },
];

const QUALITY = [
  { key: "Fast",     steps: 15, cfg: 7.0, sec: 6  },
  { key: "Balanced", steps: 20, cfg: 7.5, sec: 9  },
  { key: "Quality",  steps: 25, cfg: 7.5, sec: 14 },
  { key: "Max",      steps: 30, cfg: 8.0, sec: 20 },
];

const ASPECTS = [
  { key: "1:1",  w: 512, h: 512, label: "SQUARE" },
  { key: "3:4",  w: 432, h: 576, label: "PORTRAIT" },
  { key: "4:3",  w: 576, h: 432, label: "LANDSCAPE" },
  { key: "9:16", w: 384, h: 640, label: "STORY" },
  { key: "16:9", w: 640, h: 384, label: "WIDE" },
];

const EXAMPLES = {
  none:        ["cabin in the mountains, snow, warm light", "futuristic cityscape at night, neon, rain", "a peaceful garden with a stone fountain"],
  anime:       ["a girl with blue hair, detailed, colorful", "anime warrior with glowing sword, dynamic pose", "cherry blossom landscape, sunset"],
  scientific:  ["detailed illustration of a human heart, labeled", "botanical illustration of a rose, accurate", "DNA double helix, molecular detail"],
  character:   ["fantasy warrior, full body, detailed armor", "cyberpunk assassin, neon, full plate", "medieval knight, dual-wielding, hooded"],
  ghibli:      ["magical forest with spirits and glowing mushrooms", "flying castle above the clouds, birds", "seaside village, red rooftops"],
  realistic:   ["portrait of a woman, freckles, soft bokeh", "mountain lake at sunrise, mist, reflections", "vintage car on a European street, golden hour"],
  movie_poster:["lone astronaut on Mars, dust storm approaching", "noir detective in rainy alley, smoke", "epic fantasy battle, dragons, dramatic sky"],
};

// fake tokenizer — splits on whitespace + punctuation, assigns a stable mock ID
function tokenize(text) {
  const parts = text.split(/(\s+|[,.])/).filter(t => t && !/^\s+$/.test(t));
  return parts.map((t) => {
    let h = 5381;
    for (let i = 0; i < t.length; i++) h = ((h << 5) + h + t.charCodeAt(i)) | 0;
    return { text: t, id: Math.abs(h) % 49152, punc: /[,.]/.test(t) };
  });
}

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
  useEffect(() => { const id = setInterval(() => setT(new Date()), 1000); return () => clearInterval(id); }, []);
  return t;
}

// Smooth-follow cursor
function CustomCursor() {
  const dotRef = useRef(null), ringRef = useRef(null);
  useEffect(() => {
    let mx = 0, my = 0, rx = 0, ry = 0, raf;
    const onMove = (e) => { mx = e.clientX; my = e.clientY; };
    const onDown = () => ringRef.current?.classList.add("is-down");
    const onUp = () => ringRef.current?.classList.remove("is-down");
    const onOver = (e) => {
      const t = e.target.closest("[data-cursor]");
      if (t) {
        ringRef.current?.classList.add("is-over");
        ringRef.current?.setAttribute("data-label", t.dataset.cursor);
      } else {
        ringRef.current?.classList.remove("is-over");
        ringRef.current?.removeAttribute("data-label");
      }
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointermove", onOver);
    window.addEventListener("pointerdown", onDown);
    window.addEventListener("pointerup", onUp);
    function loop() {
      rx += (mx - rx) * 0.18; ry += (my - ry) * 0.18;
      if (dotRef.current) dotRef.current.style.transform = `translate3d(${mx}px, ${my}px, 0)`;
      if (ringRef.current) ringRef.current.style.transform = `translate3d(${rx}px, ${ry}px, 0)`;
      raf = requestAnimationFrame(loop);
    }
    raf = requestAnimationFrame(loop);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointermove", onOver);
      window.removeEventListener("pointerdown", onDown);
      window.removeEventListener("pointerup", onUp);
    };
  }, []);
  return (<><div ref={dotRef} className="cursor-dot" /><div ref={ringRef} className="cursor-ring" /></>);
}

// Marquee
function Marquee({ items, dir = "left", speed = 60, divider = "✦" }) {
  return (
    <div className={`marquee marquee-${dir}`} style={{ "--speed": `${speed}s` }}>
      <div className="marquee-track">
        {Array.from({ length: 3 }).map((_, k) => (
          <span key={k} className="marquee-set">
            {items.map((t, i) => (
              <span key={i} className="marquee-item">
                <span>{t}</span><span className="marquee-div">{divider}</span>
              </span>
            ))}
          </span>
        ))}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// RGB chromatic wordmark
// ─────────────────────────────────────────────────────────────
function ChromaticWordmark({ text = "YUMIRA" }) {
  return (
    <div className="cwm" data-text={text}>
      <span className="cwm-layer cwm-r" aria-hidden="true">{text}</span>
      <span className="cwm-layer cwm-g" aria-hidden="true">{text}</span>
      <span className="cwm-layer cwm-b" aria-hidden="true">{text}</span>
      <span className="cwm-layer cwm-w">{text}</span>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Token Strip — visualises prompt as embedded tokens
// ─────────────────────────────────────────────────────────────
function TokenStrip({ prompt }) {
  const tokens = useMemo(() => tokenize(prompt), [prompt]);
  return (
    <div className="tokens">
      <span className="tokens-k mono">TOKENS · {tokens.filter(t => !t.punc).length}</span>
      <div className="tokens-row">
        {tokens.map((t, i) => (
          <span key={i} className={`tok ${t.punc ? "tok-punc" : ""} ${i % 3 === 0 ? "tok-r" : i % 3 === 1 ? "tok-g" : "tok-b"}`}>
            <span className="tok-text">{t.text}</span>
            {!t.punc && <span className="tok-id">{String(t.id).padStart(5, "0")}</span>}
          </span>
        ))}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Magnetic Forge button
// ─────────────────────────────────────────────────────────────
function ForgeButton({ onClick, busy, disabled }) {
  const wrapRef = useRef(null), labelRef = useRef(null);
  useEffect(() => {
    const wrap = wrapRef.current;
    if (!wrap) return;
    function onMove(e) {
      const r = wrap.getBoundingClientRect();
      const dx = e.clientX - (r.left + r.width / 2);
      const dy = e.clientY - (r.top + r.height / 2);
      const d = Math.hypot(dx, dy);
      const k = Math.max(0, 1 - d / 240);
      const mx = (dx / r.width) * 24 * k;
      const my = (dy / r.height) * 24 * k;
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
    <button ref={wrapRef} className={`forge-btn ${busy ? "is-busy" : ""}`} onClick={onClick} disabled={disabled} data-cursor="FORGE">
      <span className="forge-btn-inner">
        <span className="forge-btn-r" aria-hidden="true">FORGE</span>
        <span className="forge-btn-g" aria-hidden="true">FORGE</span>
        <span className="forge-btn-b" aria-hidden="true">FORGE</span>
        <span ref={labelRef} className="forge-btn-label">
          {busy ? <span className="forge-spinner"></span> : <>FORGE<span className="forge-btn-arrow">↗</span></>}
        </span>
      </span>
    </button>
  );
}

// ─────────────────────────────────────────────────────────────
// Aspect dial — visual canvas size selector
// ─────────────────────────────────────────────────────────────
function AspectDial({ value, onChange }) {
  return (
    <div className="aspect-dial">
      {ASPECTS.map(a => {
        const ratio = a.w / a.h;
        const W = ratio >= 1 ? 28 : 28 * ratio;
        const H = ratio >= 1 ? 28 / ratio : 28;
        return (
          <button
            key={a.key}
            className={`aspect-btn ${value === a.key ? "is-active" : ""}`}
            onClick={() => onChange(a.key)}
            data-cursor={a.label}
          >
            <span className="aspect-vis">
              <span className="aspect-shape" style={{ width: W, height: H }}></span>
            </span>
            <span className="aspect-key mono">{a.key}</span>
            <span className="aspect-dim mono">{a.w}×{a.h}</span>
          </button>
        );
      })}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Style mix — blend 2 LoRAs with weights
// ─────────────────────────────────────────────────────────────
function StyleMix({ a, b, weight, onWeight, onA, onB }) {
  const styles = STYLES.filter(s => s.key !== "none");
  return (
    <div className="mixer">
      <div className="mixer-row">
        <select className="mix-select" value={a} onChange={(e) => onA(e.target.value)} data-cursor="LORA A">
          {styles.map(s => <option key={s.key} value={s.key}>{s.label}</option>)}
        </select>
        <div className="mix-slider-wrap">
          <input type="range" min="0" max="100" value={weight}
            onChange={(e) => onWeight(parseInt(e.target.value))} className="mix-slider" data-cursor="BLEND" />
          <div className="mix-readout mono">
            <span>{(100 - weight).toString().padStart(2, "0")}%</span>
            <span className="mix-readout-sep">/</span>
            <span>{weight.toString().padStart(2, "0")}%</span>
          </div>
        </div>
        <select className="mix-select" value={b} onChange={(e) => onB(e.target.value)} data-cursor="LORA B">
          {styles.map(s => <option key={s.key} value={s.key}>{s.label}</option>)}
        </select>
      </div>
      <div className="mix-bar">
        <span className="mix-bar-a" style={{ width: `${100 - weight}%` }}></span>
        <span className="mix-bar-b" style={{ width: `${weight}%` }}></span>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Latent scatter — past seeds in 2D embedding space
// ─────────────────────────────────────────────────────────────
function LatentScatter({ history, current }) {
  // Project each seed into 2D using two simple hashes
  function project(seed) {
    let a = (seed >>> 0) || 1;
    a = (a * 1664525 + 1013904223) >>> 0;
    const x = (a % 1000) / 1000;
    a = (a * 1664525 + 1013904223) >>> 0;
    const y = (a % 1000) / 1000;
    return { x, y };
  }
  const all = history.length ? history : [];
  return (
    <div className="latent">
      <div className="latent-plot">
        <svg viewBox="0 0 100 100" width="100%" height="100%" preserveAspectRatio="none">
          <defs>
            <pattern id="latent-grid" width="10" height="10" patternUnits="userSpaceOnUse">
              <path d="M 10 0 L 0 0 0 10" fill="none" stroke="currentColor" strokeOpacity="0.08" strokeWidth="0.3"/>
            </pattern>
          </defs>
          <rect width="100" height="100" fill="url(#latent-grid)" />
          <line x1="0" y1="50" x2="100" y2="50" stroke="currentColor" strokeOpacity="0.18" strokeDasharray="0.5 1.5" />
          <line x1="50" y1="0" x2="50" y2="100" stroke="currentColor" strokeOpacity="0.18" strokeDasharray="0.5 1.5" />
          {all.map((h) => {
            const p = project(h.seed);
            const cls = `lp lp-${h.color || "G"}`;
            return (
              <g key={h.id}>
                <circle className={cls} cx={p.x * 100} cy={p.y * 100} r="1.6" />
                <circle className={cls + " lp-halo"} cx={p.x * 100} cy={p.y * 100} r="4" fill="none" strokeWidth="0.3" />
              </g>
            );
          })}
          {current.seed > 0 && (
            <g>
              <circle cx={project(current.seed).x * 100} cy={project(current.seed).y * 100} r="2.6" fill="#fff" />
              <circle cx={project(current.seed).x * 100} cy={project(current.seed).y * 100} r="6" fill="none" stroke="#fff" strokeWidth="0.4">
                <animate attributeName="r" from="3" to="9" dur="1.4s" repeatCount="indefinite" />
                <animate attributeName="opacity" from="1" to="0" dur="1.4s" repeatCount="indefinite" />
              </circle>
            </g>
          )}
        </svg>
        <span className="latent-ax latent-ax-x mono">DIM_0 →</span>
        <span className="latent-ax latent-ax-y mono">DIM_1 →</span>
        <span className="latent-corner mono">latent_space</span>
      </div>
      <div className="latent-legend">
        <span><span className="dot-r"></span>R cluster</span>
        <span><span className="dot-g"></span>G cluster</span>
        <span><span className="dot-b"></span>B cluster</span>
        <span className="latent-meta mono">{all.length} POINTS · {current.seed > 0 ? "CURRENT TRACKED" : "—"}</span>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Result frame — uses DiffusionCanvas
// ─────────────────────────────────────────────────────────────
function ResultFrame({ state, aspect }) {
  const { phase, seed, steps, cfg, label } = state;
  const a = ASPECTS.find(x => x.key === aspect) || ASPECTS[0];
  return (
    <div className="resultframe" style={{ aspectRatio: `${a.w} / ${a.h}` }}>
      <div className="rf-corners" aria-hidden="true">
        <span className="c c-tl"></span><span className="c c-tr"></span>
        <span className="c c-bl"></span><span className="c c-br"></span>
      </div>
      <div className="rf-stage">
        <DiffusionCanvas phase={phase} seed={seed || 1} hue={state.hue || 28} steps={steps} />
        {phase === "idle" && (
          <div className="rf-overlay rf-idle-ov">
            <em>NOISE_INIT</em>
            <small className="mono">awaiting prompt · press ⏎</small>
          </div>
        )}
        {phase === "busy" && (
          <div className="rf-overlay rf-busy-ov">
            <ProgressLine steps={steps} cfg={cfg} />
          </div>
        )}
        {phase === "done" && (
          <div className="rf-tags">
            <span className="tag mono">SEED · {seed}</span>
            <span className="tag mono">{steps}STP / CFG{cfg.toFixed(1)}</span>
            <span className="tag mono">{label.toUpperCase()}</span>
            <span className="tag mono">{a.w}×{a.h}</span>
          </div>
        )}
      </div>
    </div>
  );
}

function ProgressLine({ steps, cfg }) {
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
    <div className="rf-prog mono">
      <div className="rf-prog-line">
        <span className="rf-prog-tag">DENOISING</span>
        <span>{pct.toFixed(0).padStart(3, "0")}%</span>
      </div>
      <div className="rf-prog-bar"><span style={{ width: `${pct}%` }}></span></div>
      <div className="rf-prog-line subtle">
        <span>step {String(stp).padStart(2, "0")}/{steps}</span>
        <span>cfg {cfg.toFixed(1)}</span>
        <span>σ {(Math.random() * 14.6 + 0.8).toFixed(2)}</span>
      </div>
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
  return (<div className="bigclock mono"><span>{hh}</span><span className="bc-sep">:</span><span>{mm}</span><span className="bc-sep">:</span><span className="bc-ms">{ss}</span></div>);
}

// ─────────────────────────────────────────────────────────────
// Style picker — RGB-coded
// ─────────────────────────────────────────────────────────────
function StylePicker({ value, onChange }) {
  return (
    <div className="styles-row">
      {STYLES.map(s => (
        <button key={s.key} className={`style-pill style-${s.color} ${value === s.key ? "is-active" : ""}`}
          onClick={() => onChange(s.key)} data-cursor={s.label}>
          <span className="sp-num mono">{s.num}</span>
          <span className="sp-label">{s.label}</span>
          <span className="sp-sub mono">[{s.color}]</span>
        </button>
      ))}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Telemetry
// ─────────────────────────────────────────────────────────────
function Telemetry() {
  const vram = useTickingNumber(2840, { min: 2200, max: 7600, jitter: 280, interval: 1600 });
  const gpu = useTickingNumber(38, { min: 12, max: 96, jitter: 22, interval: 900 });
  const fps = useTickingNumber(60, { min: 56, max: 60, jitter: 4, interval: 500 });
  const tokens = useTickingNumber(48128, { min: 30000, max: 60000, jitter: 2400, interval: 1300 });
  const items = [
    { k: "VRAM",   v: `${vram} MB`,        b: vram / 8000,   c: "B" },
    { k: "GPU",    v: `${gpu}%`,           b: gpu / 100,     c: "R" },
    { k: "FPS",    v: `${fps}`,            b: fps / 60,      c: "G" },
    { k: "EMBED",  v: tokens.toLocaleString(), b: tokens / 60000, c: "W" },
  ];
  return (
    <div className="telemetry">
      {items.map((it) => (
        <div key={it.k} className="telem-row">
          <div className="telem-head">
            <span className="telem-k">{it.k}</span>
            <span className="telem-v">{it.v}</span>
          </div>
          <div className="telem-bar"><span className={`telem-fill telem-${it.c}`} style={{ width: `${it.b * 100}%` }}></span></div>
        </div>
      ))}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Main App
// ─────────────────────────────────────────────────────────────
const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "palette": "rgb",
  "cursor": true,
  "field": true,
  "grain": true
}/*EDITMODE-END*/;

function App() {
  const [t, setTweak] = useTweaks(TWEAK_DEFAULTS);
  const [prompt, setPrompt] = useState("a lone astronaut on Mars, dust storm approaching, cinematic, dramatic lighting");
  const [style, setStyle] = useState("movie_poster");
  const [preset, setPreset] = useState("Balanced");
  const [seed, setSeed] = useState(-1);
  const [aspect, setAspect] = useState("1:1");
  const [mixA, setMixA] = useState("realistic");
  const [mixB, setMixB] = useState("anime");
  const [mixW, setMixW] = useState(35);
  const [output, setOutput] = useState({ phase: "idle", seed: 0, prompt: "", style: "", steps: 20, cfg: 7.5, label: "", hue: 28, color: "G" });
  const [history, setHistory] = useState([]);
  const currentStyle = STYLES.find(s => s.key === style);
  const q = QUALITY.find(qq => qq.key === preset);

  useEffect(() => {
    document.documentElement.dataset.palette = t.palette;
    document.documentElement.dataset.cursor = t.cursor ? "on" : "off";
    document.documentElement.dataset.grain = t.grain ? "on" : "off";
  }, [t.palette, t.cursor, t.grain]);

  function doForge() {
    if (!prompt.trim() || output.phase === "busy") return;
    const sd = seed >= 0 ? seed : Math.floor(Math.random() * 4294967295);
    setOutput({ phase: "busy", seed: sd, prompt, style, steps: q.steps, cfg: q.cfg, label: currentStyle.label, hue: currentStyle.hue, color: currentStyle.color });
    setTimeout(() => {
      setOutput({ phase: "done", seed: sd, prompt, style, steps: q.steps, cfg: q.cfg, label: currentStyle.label, hue: currentStyle.hue, color: currentStyle.color });
      setHistory(h => [{ id: Date.now(), seed: sd, prompt, style, steps: q.steps, cfg: q.cfg, label: currentStyle.label, hue: currentStyle.hue, color: currentStyle.color }, ...h].slice(0, 18));
    }, 2200);
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
    "RTX 3050 · CUDA 12.1",
    "V3.0.0",
    "RAW LATENT OUTPUT",
    "DIFFUSION_ACTIVE",
  ];
  const bottomItems = [
    `SEED ${String(output.seed || 0).padStart(10, "0")}`,
    `STYLE — ${(currentStyle?.label || "FREE")}`,
    `STEPS ${q.steps}`,
    `CFG ${q.cfg.toFixed(1)}`,
    `LATENT 64×64`,
    "VAE FP16",
    "SCHED · EULER A",
    "DEVICE · CUDA",
  ];

  return (
    <>
      {t.field && <FlowFieldBG accent="#FF2E4D" accent2="#2E7BFF" intensity={0.8} />}
      {t.cursor && <CustomCursor />}

      <div className="page">
        <Marquee items={topItems} dir="left" speed={68} divider="◇" />

        {/* HERO */}
        <header className="hero">
          <div className="hero-meta">
            <span className="hero-meta-k">INDEX</span>
            <span className="hero-meta-v mono">F-001 / IMAGE FOUNDRY</span>
            <span className="hero-meta-rule"></span>
            <span className="hero-meta-rgb mono"><span className="r-pt">R</span><span className="g-pt">G</span><span className="b-pt">B</span></span>
            <BigClock />
          </div>
          <ChromaticWordmark text="YUMIRA" />
          <div className="hero-bottom">
            <div className="hero-tag">
              <span className="hero-tag-italic">a <em>foundry</em>, not a feed.</span>
              <span className="hero-tag-sub mono">describe one image. seven LoRAs. one local diffusion pipeline.</span>
            </div>
            <div className="hero-orbs">
              <span className="orb r-pt"><span></span></span>
              <span className="orb g-pt"><span></span></span>
              <span className="orb b-pt"><span></span></span>
              <span className="orb-text mono">RGB · READY · GPU IDLE</span>
            </div>
          </div>
        </header>

        {/* MAIN */}
        <main className="main">
          {/* LEFT */}
          <section className="panel panel-prompt">
            <div className="panel-head">
              <span className="panel-tag mono r-pt">// PROMPT_INPUT</span>
              <span className="panel-rule"></span>
              <span className="panel-tag mono subtle">{prompt.length} CHAR</span>
            </div>
            <div className="terminal">
              <span className="term-caret">▸</span>
              <textarea
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); doForge(); } }}
                placeholder="describe what to forge…"
                rows={3}
                data-cursor="TYPE"
              />
            </div>

            <TokenStrip prompt={prompt} />

            <div className="suggest">
              <span className="suggest-k mono">TRY</span>
              {(EXAMPLES[style] || EXAMPLES.none).slice(0, 3).map((ex, i) => (
                <button key={i} className="suggest-chip" onClick={() => setPrompt(ex)} data-cursor="USE">
                  {ex.slice(0, 38)}{ex.length > 38 ? "…" : ""}
                </button>
              ))}
            </div>

            <div className="ctl-head" style={{ marginTop: 24 }}>
              <span className="ctl-k mono g-pt">// STYLE</span>
              <span className="ctl-rule"></span>
              <span className="ctl-k mono subtle">{currentStyle.label} · CH {currentStyle.color}</span>
            </div>
            <StylePicker value={style} onChange={setStyle} />

            <div className="ctl-head" style={{ marginTop: 24 }}>
              <span className="ctl-k mono b-pt">// STYLE_MIX</span>
              <span className="ctl-rule"></span>
              <span className="ctl-k mono subtle">EXPERIMENTAL</span>
            </div>
            <StyleMix a={mixA} b={mixB} weight={mixW} onWeight={setMixW} onA={setMixA} onB={setMixB} />

            <div className="ctl-row">
              <div className="ctl-block">
                <div className="ctl-head"><span className="ctl-k mono r-pt">// QUALITY</span><span className="ctl-rule"></span></div>
                <div className="quality">
                  {QUALITY.map(qq => (
                    <button key={qq.key} className={`q-btn ${preset === qq.key ? "is-active" : ""}`} onClick={() => setPreset(qq.key)} data-cursor={qq.key.toUpperCase()}>
                      <span className="q-name">{qq.key}</span>
                      <span className="q-meta mono">{qq.steps}s · ~{qq.sec}s</span>
                    </button>
                  ))}
                </div>
              </div>
              <div className="ctl-block">
                <div className="ctl-head"><span className="ctl-k mono g-pt">// ASPECT</span><span className="ctl-rule"></span></div>
                <AspectDial value={aspect} onChange={setAspect} />
              </div>
            </div>

            <div className="ctl-head" style={{ marginTop: 24 }}>
              <span className="ctl-k mono b-pt">// SEED</span>
              <span className="ctl-rule"></span>
              <SeedFingerprint seed={seed >= 0 ? seed : (output.seed || 1)} size={28} color="rgba(255,255,255,0.6)" />
            </div>
            <div className="seed">
              <input type="number" value={seed} onChange={(e) => setSeed(parseInt(e.target.value || -1))} className="seed-input mono" data-cursor="SEED" />
              <button className="seed-rand mono" onClick={() => setSeed(-1)} data-cursor="RANDOMIZE">↻ RAND</button>
              <button className="seed-rand mono" onClick={() => setSeed(output.seed || 0)} data-cursor="LOCK" disabled={!output.seed}>⊡ LOCK</button>
            </div>

            <div className="forge-row">
              <ForgeButton onClick={doForge} busy={output.phase === "busy"} disabled={!prompt.trim()} />
              <button className="ghost-btn" onClick={doSurprise} data-cursor="SHUFFLE">
                <span>SURPRISE ME</span><span className="ghost-arrow">↺</span>
              </button>
            </div>
          </section>

          {/* RIGHT */}
          <section className="panel panel-result">
            <div className="panel-head">
              <span className="panel-tag mono g-pt">// OUTPUT_FRAME</span>
              <span className="panel-rule"></span>
              <span className={`panel-tag mono ${output.phase === "busy" ? "blink" : ""}`}>{output.phase.toUpperCase()}</span>
            </div>
            <ResultFrame state={output} aspect={aspect} />
            <div className="panel-foot">
              <div className="pf-fp">
                <SeedFingerprint seed={output.seed || 1} size={48} color="var(--ink)" />
                <div className="pf-fp-meta">
                  <span className="mono pf-k">FINGERPRINT</span>
                  <span className="mono pf-v">SD-{String(output.seed || 0).padStart(10, "0")}</span>
                </div>
              </div>
              <div className="pf-actions">
                <button className="line-btn" disabled={output.phase !== "done"} data-cursor="SAVE">↧ SAVE</button>
                <button className="line-btn" disabled={output.phase !== "done"} data-cursor="REROLL" onClick={() => { setSeed(-1); setTimeout(doForge, 30); }}>↻ REROLL</button>
                <button className="line-btn" disabled={output.phase !== "done"} data-cursor="UPSCALE">⤴ 2X</button>
              </div>
            </div>
          </section>
        </main>

        {/* SECTION 3 — LATENT + RECENT */}
        <section className="section two-col">
          <div>
            <div className="section-head">
              <span className="section-num mono">03</span>
              <h2 className="section-title">Latent <em>space</em>.</h2>
              <span className="section-rule"></span>
              <span className="section-hint mono">EVERY SEED → 2D PROJECTION</span>
            </div>
            <div className="panel">
              <LatentScatter history={history} current={output} />
            </div>
          </div>
          <div>
            <div className="section-head">
              <span className="section-num mono">04</span>
              <h2 className="section-title">Live <em>signal</em>.</h2>
              <span className="section-rule"></span>
            </div>
            <div className="panel">
              <Telemetry />
            </div>
          </div>
        </section>

        {/* SECTION 5 — RECENT */}
        <section className="section">
          <div className="section-head">
            <span className="section-num mono">05</span>
            <h2 className="section-title">Contact <em>sheet</em>.</h2>
            <span className="section-rule"></span>
            <span className="section-hint mono">{history.length} FORGES · SESSION</span>
          </div>
          {history.length === 0 ? (
            <div className="contact-empty">
              <span className="ce-glyph">⌧</span>
              <span>no forges yet — your contact sheet appears here</span>
            </div>
          ) : (
            <div className="contact-grid">
              {history.map(h => (
                <button key={h.id} className={`contact-card contact-${h.color}`}
                  onClick={() => setOutput({ phase: "done", seed: h.seed, prompt: h.prompt, style: h.style, steps: h.steps, cfg: h.cfg, label: h.label, hue: h.hue, color: h.color })}
                  data-cursor="OPEN">
                  <div className="contact-fp"><SeedFingerprint seed={h.seed} size={70} color="currentColor" /></div>
                  <div className="contact-meta">
                    <span className="cc-id mono">#{String(h.seed).padStart(10, "0").slice(-10)}</span>
                    <span className="cc-style mono">{h.label} · CH {h.color}</span>
                    <span className="cc-prompt">{h.prompt.slice(0, 60)}{h.prompt.length > 60 ? "…" : ""}</span>
                  </div>
                </button>
              ))}
            </div>
          )}
        </section>

        <Marquee items={bottomItems} dir="right" speed={52} divider="·" />

        <footer className="footer">
          <span className="mono">YUMIRA LABS — IMAGE FOUNDRY · V3.0.0</span>
          <span className="footer-flex"></span>
          <span className="mono subtle">localhost:7860 · CUDA 12.1 · TORCH 2.3 · SD 1.5</span>
        </footer>
      </div>

      <TweaksPanel title="Tweaks">
        <TweakSection label="Palette">
          <TweakRadio value={t.palette} onChange={(v) => setTweak("palette", v)} options={[
            { value: "rgb",   label: "RGB" },
            { value: "cmyk",  label: "CMYK" },
            { value: "mono",  label: "Mono" },
          ]} />
        </TweakSection>
        <TweakSection label="Custom cursor">
          <TweakRadio value={t.cursor ? "on" : "off"} onChange={(v) => setTweak("cursor", v === "on")} options={[
            { value: "on", label: "On" }, { value: "off", label: "Off" }]} />
        </TweakSection>
        <TweakSection label="Flow field bg">
          <TweakRadio value={t.field ? "on" : "off"} onChange={(v) => setTweak("field", v === "on")} options={[
            { value: "on", label: "On" }, { value: "off", label: "Off" }]} />
        </TweakSection>
        <TweakSection label="Film grain">
          <TweakRadio value={t.grain ? "on" : "off"} onChange={(v) => setTweak("grain", v === "on")} options={[
            { value: "on", label: "On" }, { value: "off", label: "Off" }]} />
        </TweakSection>
      </TweaksPanel>
    </>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
