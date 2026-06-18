/* global React */
const { useEffect, useRef } = React;

// Diffusion-style canvas: starts as pure RGB noise, denoises into a coherent abstract image.
// phase: "idle" | "busy" | "done"
// seed: number — deterministic structure
// hue: number — base hue for the final image
// steps: number — total denoising steps to simulate
function DiffusionCanvas({ phase, seed = 0, hue = 28, steps = 20 }) {
  const ref = useRef(null);
  const rafRef = useRef(0);
  const stateRef = useRef({ phase, seed, hue, steps, t: 0, progress: 0, completed: false });

  // Keep latest props in stateRef so the long-running loop sees them
  useEffect(() => {
    stateRef.current.phase = phase;
    stateRef.current.seed = seed;
    stateRef.current.hue = hue;
    stateRef.current.steps = steps;
    if (phase === "busy") {
      stateRef.current.progress = 0;
      stateRef.current.completed = false;
    } else if (phase === "done") {
      stateRef.current.progress = 1;
      stateRef.current.completed = true;
    } else {
      stateRef.current.progress = 0;
      stateRef.current.completed = false;
    }
  }, [phase, seed, hue, steps]);

  useEffect(() => {
    const cvs = ref.current;
    if (!cvs) return;
    const SIZE = 192; // internal noise resolution
    cvs.width = SIZE; cvs.height = SIZE;
    const ctx = cvs.getContext("2d");
    const img = ctx.createImageData(SIZE, SIZE);

    // Deterministic mulberry32
    function makeRng(seed) {
      let a = (seed >>> 0) || 1;
      return () => {
        a |= 0; a = (a + 0x6D2B79F5) | 0;
        let t = Math.imul(a ^ (a >>> 15), 1 | a);
        t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
      };
    }

    // Pre-compute "target" image: a few blurred radial blobs (the "coherent" image)
    let target = null;
    function buildTarget(seed, hue) {
      const rng = makeRng(seed);
      const t = new Uint8ClampedArray(SIZE * SIZE * 4);
      const blobs = [];
      const N = 3 + Math.floor(rng() * 3);
      for (let i = 0; i < N; i++) {
        blobs.push({
          x: rng() * SIZE,
          y: rng() * SIZE,
          r: 18 + rng() * 50,
          hue: hue + (rng() - 0.5) * 80,
          int: 0.5 + rng() * 0.5,
        });
      }
      // gradient base
      for (let y = 0; y < SIZE; y++) {
        for (let x = 0; x < SIZE; x++) {
          let r = 0, g = 0, b = 0;
          // diagonal base gradient in chosen hue
          const t01 = (x + y) / (SIZE * 2);
          const baseHue = hue;
          const baseRGB = hslToRgb(baseHue / 360, 0.45, 0.15 + t01 * 0.25);
          r += baseRGB[0]; g += baseRGB[1]; b += baseRGB[2];

          // add blobs
          for (const bl of blobs) {
            const dx = x - bl.x, dy = y - bl.y;
            const d = Math.sqrt(dx * dx + dy * dy);
            const w = Math.max(0, 1 - d / bl.r);
            const f = w * w * bl.int;
            const c = hslToRgb(((bl.hue % 360) + 360) % 360 / 360, 0.7, 0.5);
            r += c[0] * f;
            g += c[1] * f;
            b += c[2] * f;
          }
          const idx = (y * SIZE + x) * 4;
          t[idx]     = Math.min(255, r);
          t[idx + 1] = Math.min(255, g);
          t[idx + 2] = Math.min(255, b);
          t[idx + 3] = 255;
        }
      }
      return t;
    }

    function hslToRgb(h, s, l) {
      let r, g, b;
      if (s === 0) { r = g = b = l; }
      else {
        const hue2rgb = (p, q, t) => {
          if (t < 0) t += 1;
          if (t > 1) t -= 1;
          if (t < 1/6) return p + (q - p) * 6 * t;
          if (t < 1/2) return q;
          if (t < 2/3) return p + (q - p) * (2/3 - t) * 6;
          return p;
        };
        const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
        const p = 2 * l - q;
        r = hue2rgb(p, q, h + 1/3);
        g = hue2rgb(p, q, h);
        b = hue2rgb(p, q, h - 1/3);
      }
      return [r * 255, g * 255, b * 255];
    }

    let lastSeed = -1, lastHue = -1;

    function frame() {
      const st = stateRef.current;
      if (st.seed !== lastSeed || st.hue !== lastHue) {
        target = buildTarget(st.seed || 1, st.hue);
        lastSeed = st.seed; lastHue = st.hue;
      }
      if (!target) target = buildTarget(st.seed || 1, st.hue);

      st.t += 1;
      // progress logic
      if (st.phase === "busy") {
        st.progress = Math.min(1, st.progress + 1 / (st.steps * 6));
      } else if (st.phase === "done") {
        st.progress = 1;
      } else if (st.phase === "idle") {
        st.progress = 0;
      }

      const p = st.progress;
      const noiseAmp = (1 - p) * 255;     // noise intensity
      const signalMix = p;                // how much of target shows

      const data = img.data;
      const len = SIZE * SIZE;
      const rng = Math.random;

      // animate noise even when idle
      for (let i = 0; i < len; i++) {
        const idx = i * 4;
        const n = (rng() - 0.5);
        const r = target[idx]     * signalMix + n * noiseAmp + 30 * (1 - signalMix);
        const g = target[idx + 1] * signalMix + (rng() - 0.5) * noiseAmp + 30 * (1 - signalMix);
        const b = target[idx + 2] * signalMix + (rng() - 0.5) * noiseAmp + 30 * (1 - signalMix);
        data[idx]     = Math.max(0, Math.min(255, r));
        data[idx + 1] = Math.max(0, Math.min(255, g));
        data[idx + 2] = Math.max(0, Math.min(255, b));
        data[idx + 3] = 255;
      }
      ctx.putImageData(img, 0, 0);

      rafRef.current = requestAnimationFrame(frame);
    }
    rafRef.current = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(rafRef.current);
  }, []);

  return <canvas ref={ref} className="dc-canvas" />;
}

window.DiffusionCanvas = DiffusionCanvas;

// ─────────────────────────────────────────────────────────────
// Seed Fingerprint — mandala derived from seed (SVG)
// Every seed = unique geometric "signature"
// ─────────────────────────────────────────────────────────────
function SeedFingerprint({ seed = 0, size = 56, color = "currentColor" }) {
  // deterministic angles + radii from seed
  function makeRng(s) {
    let a = (s >>> 0) || 1;
    return () => {
      a |= 0; a = (a + 0x6D2B79F5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  const rng = makeRng(seed);
  const spokes = 8 + Math.floor(rng() * 6);
  const layers = 3 + Math.floor(rng() * 3);
  const lines = [];
  for (let L = 0; L < layers; L++) {
    const r = 6 + L * 6 + rng() * 3;
    for (let i = 0; i < spokes; i++) {
      const a = (i / spokes) * Math.PI * 2;
      const r2 = r + rng() * 4;
      const a2 = a + ((rng() - 0.5) * 0.6);
      lines.push({
        x1: 28 + Math.cos(a) * r,
        y1: 28 + Math.sin(a) * r,
        x2: 28 + Math.cos(a2) * r2,
        y2: 28 + Math.sin(a2) * r2,
        op: 0.4 + rng() * 0.5,
      });
    }
  }
  const dots = [];
  for (let i = 0; i < spokes; i++) {
    const a = (i / spokes) * Math.PI * 2;
    const r = 22 + rng() * 4;
    dots.push({ x: 28 + Math.cos(a) * r, y: 28 + Math.sin(a) * r, r: 0.6 + rng() * 1.2 });
  }
  return (
    <svg viewBox="0 0 56 56" width={size} height={size} className="seed-fp">
      <circle cx="28" cy="28" r="26" fill="none" stroke={color} strokeOpacity="0.2" strokeDasharray="1 2" />
      <circle cx="28" cy="28" r="2" fill={color} />
      {lines.map((l, i) => (
        <line key={i} x1={l.x1} y1={l.y1} x2={l.x2} y2={l.y2} stroke={color} strokeOpacity={l.op} strokeWidth="0.6" />
      ))}
      {dots.map((d, i) => (
        <circle key={i} cx={d.x} cy={d.y} r={d.r} fill={color} />
      ))}
    </svg>
  );
}

window.SeedFingerprint = SeedFingerprint;
