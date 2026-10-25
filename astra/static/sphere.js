/* ============================================================
   Astra — 3D-сфера из точек для голосового режима.

   Прозрачная сфера из голубых точек: точки распределены по поверхности
   (спираль Фибоначчи), вращается в 3D и проецируется на canvas с
   перспективой. Реагирует на громкость голоса — плавно расширяется
   и сжимается, точки немного «дышат» по радиусу.

   Никаких библиотек: чистый Canvas 2D + немного тригонометрии.
   ============================================================ */
"use strict";

(function () {
  const TAU = Math.PI * 2;

  function fibonacciSphere(count) {
    const points = [];
    const golden = Math.PI * (3 - Math.sqrt(5)); // угол золотого сечения
    for (let i = 0; i < count; i += 1) {
      const y = count === 1 ? 0 : 1 - (i / (count - 1)) * 2;
      const radius = Math.sqrt(Math.max(0, 1 - y * y));
      const theta = golden * i;
      points.push({
        x: Math.cos(theta) * radius,
        y,
        z: Math.sin(theta) * radius,
        phase: Math.random() * TAU,
        size: 0.75 + Math.random() * 0.7,
        warm: Math.random() < 0.16,     // часть точек чуть светлее — глубина
      });
    }
    return points;
  }

  class DotSphere3D {
    constructor(canvas, options = {}) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.opts = Object.assign({
        count: 760,
        radius: 0.36,          // доля от половины меньшей стороны
        perspective: 2.9,      // чем больше, тем меньше «рыбий глаз»
        spin: 0.22,            // базовая скорость вращения, рад/с
        color: [110, 200, 255],
        accent: [150, 225, 255],
        deep: [46, 120, 230],
        showHalo: true,
      }, options);

      this.points = fibonacciSphere(this.opts.count);
      this.level = 0;          // сглаженная громкость 0..1
      this.target = 0;         // целевая громкость
      this.mode = "idle";      // idle | listening | thinking | speaking | transcribing
      this.time = 0;
      this.rotY = Math.random() * TAU;
      this.rotX = 0.32;
      this.running = true;
      this.lastTs = 0;
      this.dpr = Math.min(2, window.devicePixelRatio || 1);
      this.width = 0;
      this.height = 0;

      this._loop = this._loop.bind(this);
      this.resize();
      this._raf = requestAnimationFrame(this._loop);

      if (window.ResizeObserver) {
        this._observer = new ResizeObserver(() => this.resize());
        this._observer.observe(this.canvas.parentElement || this.canvas);
      } else {
        window.addEventListener("resize", () => this.resize());
      }
      document.addEventListener("visibilitychange", () => {
        if (document.hidden) this.pause();
        else this.resume();
      });
    }

    /* ------------------------------------------------------- размеры */
    resize() {
      const parent = this.canvas.parentElement || this.canvas;
      const rect = parent.getBoundingClientRect();
      const size = Math.max(80, Math.min(rect.width || 320, rect.height || 320));
      this.width = size;
      this.height = size;
      this.dpr = Math.min(2, window.devicePixelRatio || 1);
      this.canvas.width = Math.round(size * this.dpr);
      this.canvas.height = Math.round(size * this.dpr);
      this.canvas.style.width = size + "px";
      this.canvas.style.height = size + "px";
      this.ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    }

    /* ----------------------------------------------------- управление */
    setLevel(value) {
      const v = Math.max(0, Math.min(1, Number(value) || 0));
      // лёгкое усиление: тихая речь тоже заметна
      this.target = Math.min(1, Math.pow(v, 0.75) * 1.15);
    }

    setMode(mode) {
      this.mode = mode || "idle";
    }

    setPaused(paused) {
      if (paused) this.pause();
      else this.resume();
    }

    pause() {
      this.running = false;
    }

    resume() {
      if (this.running) return;
      this.running = true;
      this.lastTs = 0;
      this._raf = requestAnimationFrame(this._loop);
    }

    destroy() {
      this.running = false;
      cancelAnimationFrame(this._raf);
      if (this._observer) this._observer.disconnect();
    }

    /* --------------------------------------------------------- кадры */
    _loop(ts) {
      if (!this.running) return;
      if (!this.lastTs) this.lastTs = ts;
      const dt = Math.min(0.06, (ts - this.lastTs) / 1000);
      this.lastTs = ts;
      this.time += dt;

      const speed = { idle: 1, listening: 1.15, transcribing: 1.5, thinking: 1.9, speaking: 1.35 }[this.mode] || 1;
      const ease = Math.min(1, dt * 8);
      this.level += (this.target - this.level) * ease;

      this.rotY += dt * this.opts.spin * speed * (1 + this.level * 1.4);
      const tiltTarget = 0.32 + 0.10 * Math.sin(this.time * 0.27) + this.level * 0.16;
      this.rotX += (tiltTarget - this.rotX) * Math.min(1, dt * 2.5);

      this._draw();
      this._raf = requestAnimationFrame(this._loop);
    }

    _draw() {
      const ctx = this.ctx;
      const w = this.width;
      const h = this.height;
      const cx = w / 2;
      const cy = h / 2;
      const level = this.level;
      const t = this.time;

      ctx.clearRect(0, 0, w, h);

      // дышащий радиус: покой + плавная реакция на голос
      const breath = 1 + 0.028 * Math.sin(t * 1.35) + 0.115 * level
        + (this.mode === "speaking" ? 0.03 * Math.sin(t * 6.2) : 0);
      const radius = Math.min(w, h) * this.opts.radius * breath;

      // мягкое свечение позади сферы
      const glow = ctx.createRadialGradient(cx, cy, radius * 0.12, cx, cy, radius * (1.65 + 0.35 * level));
      const glowAlpha = 0.16 + 0.22 * level;
      glow.addColorStop(0, `rgba(90, 175, 255, ${(glowAlpha * 0.85).toFixed(3)})`);
      glow.addColorStop(0.55, `rgba(60, 130, 255, ${(glowAlpha * 0.28).toFixed(3)})`);
      glow.addColorStop(1, "rgba(10, 20, 40, 0)");
      ctx.fillStyle = glow;
      ctx.beginPath();
      ctx.arc(cx, cy, radius * (1.7 + 0.35 * level), 0, TAU);
      ctx.fill();

      const cosY = Math.cos(this.rotY);
      const sinY = Math.sin(this.rotY);
      const cosX = Math.cos(this.rotX);
      const sinX = Math.sin(this.rotX);
      const persp = this.opts.perspective;
      const [r0, g0, b0] = this.opts.color;
      const [r1, g1, b1] = this.opts.accent;
      const [r2, g2, b2] = this.opts.deep;

      // два прохода: сначала мягкое гало (сложение), потом сами точки
      ctx.globalCompositeOperation = "lighter";
      for (let pass = 0; pass < 2; pass += 1) {
        const halo = pass === 0;
        for (let i = 0; i < this.points.length; i += 1) {
          const p = this.points[i];
          // «дыхание» точек по радиусу
          const jitter = 1
            + 0.022 * Math.sin(p.phase + t * 2.0)
            + 0.075 * level * Math.sin(p.phase * 2.7 + t * 5.4);
          const x0 = p.x * jitter;
          const y0 = p.y * jitter;
          const z0 = p.z * jitter;

          // поворот вокруг Y, затем вокруг X
          const x1 = x0 * cosY + z0 * sinY;
          const z1 = -x0 * sinY + z0 * cosY;
          const y1 = y0 * cosX - z1 * sinX;
          const z2 = y0 * sinX + z1 * cosX;

          const scale = persp / (persp + z2);          // 1..~1.5
          const sx = cx + x1 * radius * scale;
          const sy = cy + y1 * radius * scale;
          if (sx < -40 || sy < -40 || sx > w + 40 || sy > h + 40) continue;

          const depth = (z2 + 1) / 2;                  // 0 — дальняя сторона, 1 — ближняя
          const fade = Math.pow(depth, 1.35);
          if (halo) {
            if (depth < 0.45) continue;
            const a = fade * (0.10 + 0.12 * level);
            const r = (p.size * 3.2 + 1.6) * scale * (1 + 0.25 * level);
            ctx.fillStyle = `rgba(${r0}, ${g0}, ${b0}, ${a.toFixed(3)})`;
            ctx.beginPath();
            ctx.arc(sx, sy, r, 0, TAU);
            ctx.fill();
          } else {
            const a = 0.12 + 0.72 * fade;
            const r = (0.75 + 0.95 * fade) * p.size * scale * (1 + 0.18 * level);
            let cr = r2, cg = g2, cb = b2;
            if (p.warm) { cr = r1; cg = g1; cb = b1; }
            else if (fade > 0.6) { cr = r0; cg = g0; cb = b0; }
            ctx.fillStyle = `rgba(${cr}, ${cg}, ${cb}, ${a.toFixed(3)})`;
            ctx.beginPath();
            ctx.arc(sx, sy, Math.max(0.35, r), 0, TAU);
            ctx.fill();
          }
        }
      }
      ctx.globalCompositeOperation = "source-over";

      // тонкое кольцо-орбита: подчёркивает объём
      ctx.strokeStyle = `rgba(120, 190, 255, ${(0.06 + 0.10 * level).toFixed(3)})`;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.ellipse(cx, cy, radius * 1.06, radius * (0.30 + 0.04 * Math.sin(t * 0.9)), this.rotX * 0.9, 0, TAU);
      ctx.stroke();
    }
  }

  window.DotSphere3D = DotSphere3D;
})();
