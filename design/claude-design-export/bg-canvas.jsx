/* global React */
const { useEffect, useRef } = React;

// Flow-field canvas background: subtle moving particles that drift along noise gradients.
// Mouse pulls particles toward it. Persistent low-alpha trails create painterly streaks.
function FlowFieldBG({ accent = "#FF6E3D", accent2 = "#D4FF3A", intensity = 1 }) {
  const ref = useRef(null);
  const stateRef = useRef({ mx: 0.5, my: 0.5, t: 0, particles: [], w: 0, h: 0 });

  useEffect(() => {
    const cvs = ref.current;
    const ctx = cvs.getContext("2d");
    let raf;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);

    function resize() {
      const w = cvs.clientWidth;
      const h = cvs.clientHeight;
      cvs.width = w * dpr;
      cvs.height = h * dpr;
      ctx.scale(dpr, dpr);
      stateRef.current.w = w;
      stateRef.current.h = h;
    }
    resize();
    window.addEventListener("resize", resize);

    // particles
    const N = 220;
    stateRef.current.particles = Array.from({ length: N }, () => ({
      x: Math.random() * stateRef.current.w,
      y: Math.random() * stateRef.current.h,
      vx: 0,
      vy: 0,
      life: Math.random() * 200,
      hue: Math.random() > 0.7 ? 1 : 0, // 30% accent2
    }));

    function onMove(e) {
      stateRef.current.mx = e.clientX / window.innerWidth;
      stateRef.current.my = e.clientY / window.innerHeight;
    }
    window.addEventListener("pointermove", onMove);

    // pseudo-noise field
    function noise(x, y, t) {
      return (
        Math.sin(x * 0.0042 + t * 0.0005) * 0.5 +
        Math.cos(y * 0.0036 - t * 0.0007) * 0.5 +
        Math.sin((x + y) * 0.0025 + t * 0.001) * 0.3
      );
    }

    function tick() {
      const s = stateRef.current;
      s.t += 1;
      // soft fade for trails
      ctx.fillStyle = "rgba(6,6,7,0.08)";
      ctx.fillRect(0, 0, s.w, s.h);

      const mxp = s.mx * s.w;
      const myp = s.my * s.h;

      for (const p of s.particles) {
        const a = noise(p.x, p.y, s.t) * Math.PI * 2;
        p.vx = p.vx * 0.92 + Math.cos(a) * 0.5 * intensity;
        p.vy = p.vy * 0.92 + Math.sin(a) * 0.5 * intensity;

        // mouse pull
        const dx = mxp - p.x;
        const dy = myp - p.y;
        const d = Math.hypot(dx, dy);
        if (d < 260) {
          const f = ((260 - d) / 260) * 0.06;
          p.vx += (dx / (d + 1)) * f * 4;
          p.vy += (dy / (d + 1)) * f * 4;
        }

        p.x += p.vx;
        p.y += p.vy;
        p.life -= 1;

        if (p.x < 0 || p.x > s.w || p.y < 0 || p.y > s.h || p.life <= 0) {
          p.x = Math.random() * s.w;
          p.y = Math.random() * s.h;
          p.vx = p.vy = 0;
          p.life = 180 + Math.random() * 200;
        }

        const color = p.hue ? accent2 : accent;
        ctx.fillStyle = color;
        ctx.globalAlpha = 0.5;
        ctx.fillRect(p.x, p.y, 1, 1);
        ctx.globalAlpha = 0.12;
        ctx.fillRect(p.x - 0.5, p.y - 0.5, 2, 2);
      }
      ctx.globalAlpha = 1;
      raf = requestAnimationFrame(tick);
    }
    raf = requestAnimationFrame(tick);

    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", resize);
      window.removeEventListener("pointermove", onMove);
    };
  }, [accent, accent2, intensity]);

  return <canvas ref={ref} className="bgcanvas" />;
}

window.FlowFieldBG = FlowFieldBG;
