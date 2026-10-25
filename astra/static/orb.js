/* ============================================================
   Орб — живой индикатор состояния и голоса.

   Устройство:
   • большой круг с вращающимся градиентом (CSS), дышит от громкости;
   • вокруг по орбитам кружат маленькие кружки (JS + requestAnimationFrame);
   • иногда кружок «вливается» в большой (сжимается, вспыхивает) и через
     мгновение вылетает обратно на орбиту — с разгоном и затуханием;
   • громкость (микрофон / речь ассистента) ускоряет вращение и увеличивает
     амплитуду, поэтому круг реагирует на голос.
   ============================================================ */
(function () {
  "use strict";

  const PARTICLE_COUNT = { idle: 9, listening: 12, speaking: 13, thinking: 10, transcribing: 11 };

  class Orb {
    constructor(el, options = {}) {
      this.el = el;
      this.mode = "idle";
      this.target = 0;          // целевая громкость 0..1
      this.level = 0;           // сглаженная громкость
      this.speedFactor = 1;
      this.showParticles = options.particles !== false;
      this.particles = [];
      this.running = true;
      this.lastTs = 0;
      this.time = 0;

      this._buildParticles();
      this._loop = this._loop.bind(this);
      this._raf = requestAnimationFrame(this._loop);
    }

    /* --------------------------- построение --------------------------- */
    _buildParticles() {
      if (!this.showParticles || !this.el) return;
      // убираем прошлые
      this.el.querySelectorAll(".orb-particle").forEach((n) => n.remove());
      const count = PARTICLE_COUNT[this.mode] || PARTICLE_COUNT.idle;
      for (let i = 0; i < count; i += 1) {
        const node = document.createElement("div");
        node.className = "orb-particle";
        const variant = i % 3;
        if (variant === 1) node.classList.add("v2");
        if (variant === 2) node.classList.add("v3");
        node.style.setProperty("--size", `${8 + Math.random() * 9}px`);
        this.el.appendChild(node);
        this.particles.push({
          node,
          angle: Math.random() * Math.PI * 2,
          radius: 0.62 + Math.random() * 0.34,     // доля от радиуса контейнера
          speed: 0.28 + Math.random() * 0.5,       // рад/сек
          direction: Math.random() > 0.35 ? 1 : -1,
          bobPhase: Math.random() * Math.PI * 2,
          bobSpeed: 0.6 + Math.random() * 1.4,
          state: "orbit",                          // orbit | inward | absorbed | outward
          t: 0,
          nextMerge: 1.4 + Math.random() * 5.5,    // через сколько секунд попробует влиться
          mergeProgress: 0,
          mergeFrom: 1,
          alpha: 0.55 + Math.random() * 0.45,
          scale: 0.7 + Math.random() * 0.6,
        });
      }
      this.el.classList.add("has-particles");
    }

    _clearParticles() {
      this.particles.forEach((p) => p.node.remove());
      this.particles = [];
    }

    /* --------------------------- управление --------------------------- */
    setMode(mode) {
      if (this.mode === mode) return;
      this.mode = mode || "idle";
      const small = this.el.classList.contains("small");
      this.el.className = "orb" + (small ? " small" : "") +
        (this.mode && this.mode !== "idle" ? " mode-" + this.mode : "");
      if (this.showParticles) {
        this.el.classList.add("has-particles");
      }
      if (this.mode === "idle") this.target = 0;
      // на смену режима — небольшая вспышка активности
      if (this.mode === "listening" || this.mode === "speaking") this.pulse(0.35);
      this._rebuildForMode();
    }

    _rebuildForMode() {
      if (!this.showParticles) return;
      const wanted = PARTICLE_COUNT[this.mode] || PARTICLE_COUNT.idle;
      if (this.particles.length === wanted) return;
      // аккуратно доводим количество частиц, не сбрасывая существующие
      while (this.particles.length > wanted) {
        const p = this.particles.pop();
        if (p && p.node.parentNode) p.node.remove();
      }
      while (this.particles.length < wanted) {
        const node = document.createElement("div");
        node.className = "orb-particle" + (this.particles.length % 3 === 1 ? " v2" : this.particles.length % 3 === 2 ? " v3" : "");
        node.style.setProperty("--size", `${8 + Math.random() * 9}px`);
        this.el.appendChild(node);
        this.particles.push({
          node,
          angle: Math.random() * Math.PI * 2,
          radius: 0.6 + Math.random() * 0.36,
          speed: 0.28 + Math.random() * 0.5,
          direction: Math.random() > 0.35 ? 1 : -1,
          bobPhase: Math.random() * Math.PI * 2,
          bobSpeed: 0.6 + Math.random() * 1.4,
          state: "orbit",
          t: 0,
          nextMerge: 1 + Math.random() * 5,
          mergeProgress: 0,
          mergeFrom: 1,
          alpha: 0.55 + Math.random() * 0.45,
          scale: 0.7 + Math.random() * 0.6,
        });
      }
    }

    setLevel(value) {
      this.target = Math.max(0, Math.min(1, Number(value) || 0));
    }

    pulse(amount = 0.25) {
      this.target = Math.max(this.target, amount);
      this.pulses = (this.pulses || 0) + 1;
    }

    /* ------------------------------ цикл ------------------------------ */
    _loop(ts) {
      if (!this.running) return;
      const dtRaw = this.lastTs ? (ts - this.lastTs) / 1000 : 0.016;
      this.lastTs = ts;
      const dt = Math.min(0.05, Math.max(0.001, dtRaw));
      this.time += dt;

      // быстрая атака, плавный спад — как настоящий индикатор уровня
      const k = this.target > this.level ? 0.5 : 0.11;
      this.level += (this.target - this.level) * k;
      if (this.level < 0.002) this.level = 0;

      const idleBreath = this.mode === "idle" ? 0.05 + Math.sin(this.time * 0.8) * 0.035 : 0;
      const displayLevel = Math.max(this.level, idleBreath);
      this.el.style.setProperty("--level", displayLevel.toFixed(3));
      this.el.style.setProperty("--spin", `${(this.time * 26) % 360}deg`);

      const energy = 1 + this.level * 2.2;
      this.speedFactor = energy;

      this._animateParticles(dt, displayLevel);
      this._raf = requestAnimationFrame(this._loop);
    }

    _animateParticles(dt, level) {
      if (!this.particles.length) return;
      const w = this.el.clientWidth || 168;
      const h = this.el.clientHeight || 168;
      const baseRadius = Math.min(w, h) / 2;

      for (const p of this.particles) {
        p.angle += p.speed * p.direction * this.speedFactor * dt;

        if (p.state === "orbit") {
          p.nextMerge -= dt * (0.6 + level * 2.4);   // громче голос — чаще «вливаются»
          if (p.nextMerge <= 0) {
            p.state = "inward";
            p.t = 0;
            p.mergeFrom = p.radius;
            p.mergeProgress = 0;
          }
        } else if (p.state === "inward") {
          p.t += dt * 1.7;
          const ease = Math.min(1, p.t);
          // сжимаемся к центру с ускорением
          p.mergeProgress = ease * ease;
          if (p.t >= 1) {
            p.state = "absorbed";
            p.t = 0;
          }
        } else if (p.state === "absorbed") {
          p.t += dt;
          if (p.t > 0.22 + Math.random() * 0.35) {
            p.state = "outward";
            p.t = 0;
            p.angle = Math.random() * Math.PI * 2;
            p.radius = 0.62 + Math.random() * 0.34;
            p.scale = 0.7 + Math.random() * 0.6;
          }
        } else if (p.state === "outward") {
          p.t += dt * 2.1;
          if (p.t >= 1) {
            p.state = "orbit";
            p.t = 0;
            p.nextMerge = 2.4 + Math.random() * 6.5;
          }
        }

        // радиус с учётом слияния/вылета
        let radiusFactor = p.radius;
        let scale = p.scale;
        let alpha = p.alpha;
        let extraGlow = 0;

        if (p.state === "inward") {
          radiusFactor = p.mergeFrom * (1 - p.mergeProgress);
          scale = p.scale * (1 - p.mergeProgress * 0.85);
          alpha = p.alpha * (1 - p.mergeProgress * 0.75);
          extraGlow = p.mergeProgress;
        } else if (p.state === "absorbed") {
          radiusFactor = 0;
          scale = 0.12;
          alpha = 0.85 * Math.max(0, 1 - p.t * 2.4);
          extraGlow = 1;
        } else if (p.state === "outward") {
          const ease = 1 - Math.pow(1 - p.t, 3);
          radiusFactor = p.radius * ease * (1.08 - 0.08 * ease);
          scale = 0.35 + 0.65 * ease;
          alpha = p.alpha * (0.35 + 0.65 * ease);
          extraGlow = (1 - ease) * 0.8;
        }

        const bob = Math.sin(this.time * p.bobSpeed + p.bobPhase) * (baseRadius * 0.045);
        const ringRadius = baseRadius * radiusFactor * (1 + level * 0.12);
        const x = Math.cos(p.angle) * ringRadius;
        const y = Math.sin(p.angle) * ringRadius * 0.92 + bob;

        const depth = (Math.sin(p.angle) + 1) / 2;            // 0 — за большим кругом, 1 — перед ним
        const depthScale = 0.62 + depth * 0.55;
        const finalScale = scale * depthScale * (1 + level * 0.28);

        p.node.style.transform = `translate3d(${x.toFixed(2)}px, ${y.toFixed(2)}px, 0) scale(${finalScale.toFixed(3)})`;
        p.node.style.opacity = alpha.toFixed(3);
        p.node.style.zIndex = depth > 0.5 ? "3" : "1";
        p.node.style.setProperty("--glow", extraGlow.toFixed(3));
      }
    }

    destroy() {
      this.running = false;
      if (this._raf) cancelAnimationFrame(this._raf);
      this._clearParticles();
    }
  }

  window.Orb = Orb;
})();
