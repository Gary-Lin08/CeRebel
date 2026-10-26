/* CeRebel Playground motion — Lenis + GSAP + Vanta + electric border */
(function () {
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = src;
      script.onload = resolve;
      script.onerror = reject;
      document.head.appendChild(script);
    });
  }

  // Vanta paints only the operator UI (?dev=1); customer pages hide it, so they skip the download
  // and the second WebGL context.
  async function initVanta() {
    const el = document.getElementById("pgVanta");
    if (!el || reduce || !document.body.classList.contains("pg-dev")) return null;
    try {
      await loadScript("https://cdnjs.cloudflare.com/ajax/libs/three.js/r134/three.min.js");
      await loadScript("https://cdn.jsdelivr.net/npm/vanta@0.5.24/dist/vanta.fog.min.js");
      return window.VANTA.FOG({
        el,
        THREE: window.THREE,
        mouseControls: true,
        touchControls: true,
        gyroControls: false,
        minHeight: 200,
        minWidth: 200,
        highlightColor: 0xe1fe67,
        midtoneColor: 0x1a2210,
        lowlightColor: 0x050605,
        baseColor: 0x0a0b0b,
        blurFactor: 0.55,
        speed: 0.7,
        zoom: 0.85,
      });
    } catch (_) {
      return null;
    }
  }

  function shouldPreventLenis(node) {
    let el = node;
    while (el && el !== document.documentElement) {
      if (el.nodeType !== 1) {
        el = el.parentElement;
        continue;
      }
      if (
        el.id === "chat"
        || el.hasAttribute("data-lenis-prevent")
        || el.classList.contains("bubble")
        || el.classList.contains("thinking-body")
      ) {
        return true;
      }
      const style = window.getComputedStyle(el);
      const oy = style.overflowY;
      if (
        (oy === "auto" || oy === "scroll" || oy === "overlay")
        && el.scrollHeight > el.clientHeight + 1
      ) {
        return true;
      }
      el = el.parentElement;
    }
    return false;
  }

  function initLenisGsap() {
    if (reduce || !window.Lenis || !window.gsap) return null;
    const lenis = new window.Lenis({
      duration: 1.2,
      easing: (t) => Math.min(1, 1.001 - Math.pow(2, -10 * t)),
      smoothWheel: true,
      // Nested panels (coach chat, prompt editor) must keep native scroll.
      prevent: shouldPreventLenis,
    });
    function raf(time) {
      lenis.raf(time);
      requestAnimationFrame(raf);
    }
    requestAnimationFrame(raf);

    const gsap = window.gsap;
    if (window.ScrollTrigger) {
      gsap.registerPlugin(window.ScrollTrigger);
      lenis.on("scroll", window.ScrollTrigger.update);
    }

    const monument = document.querySelector(".pg-monument__word");
    if (monument) {
      gsap.fromTo(
        monument,
        { yPercent: 28, opacity: 0 },
        { yPercent: 0, opacity: 1, duration: 1.25, ease: "expo.out", delay: 0.08 },
      );
    }
    gsap.fromTo(
      "[data-enter]",
      { y: 28, opacity: 0 },
      { y: 0, opacity: 1, duration: 0.95, stagger: 0.08, ease: "expo.out", delay: 0.35 },
    );

    const corners = document.querySelectorAll(".pg-corner");
    if (corners.length && window.ScrollTrigger) {
      gsap.to(corners, {
        opacity: 0.35,
        scrollTrigger: {
          trigger: ".pg-stage",
          start: "top 70%",
          end: "top 20%",
          scrub: true,
        },
      });
    }
    window.__cerebelLenis = lenis;
    return lenis;
  }

  function initElectricBorder(root) {
    if (!root || reduce) return;
    const canvas = root.querySelector("canvas");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const color = root.dataset.color || "#e1fe67";
    let raf = 0;
    let t = 0;

    function resize() {
      const rect = root.getBoundingClientRect();
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.max(1, Math.floor(rect.width * dpr));
      canvas.height = Math.max(1, Math.floor(rect.height * dpr));
      canvas.style.width = `${rect.width}px`;
      canvas.style.height = `${rect.height}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    function noise(x, y) {
      return (Math.sin(x * 12.9898 + y * 78.233) * 43758.5453) % 1;
    }

    function draw() {
      const w = root.clientWidth;
      const h = root.clientHeight;
      if (w < 2 || h < 2) {
        raf = requestAnimationFrame(draw);
        return;
      }
      ctx.clearRect(0, 0, w, h);
      const pad = 3;
      const r = 10;
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.25;
      ctx.shadowColor = color;
      ctx.shadowBlur = 8;
      ctx.beginPath();
      const steps = 140;
      for (let i = 0; i <= steps; i += 1) {
        const p = i / steps;
        let x;
        let y;
        const perim = 2 * (w + h - 4 * pad - 4 * r) + 2 * Math.PI * r;
        let d = p * perim;
        const straightW = w - 2 * pad - 2 * r;
        const straightH = h - 2 * pad - 2 * r;
        if (d < straightW) {
          x = pad + r + d;
          y = pad;
        } else {
          d -= straightW;
          if (d < (Math.PI / 2) * r) {
            const a = -Math.PI / 2 + d / r;
            x = pad + r + straightW + r * Math.cos(a);
            y = pad + r + r * Math.sin(a);
          } else {
            d -= (Math.PI / 2) * r;
            if (d < straightH) {
              x = w - pad;
              y = pad + r + d;
            } else {
              d -= straightH;
              if (d < (Math.PI / 2) * r) {
                const a = d / r;
                x = pad + r + straightW + r * Math.cos(a);
                y = pad + r + straightH + r * Math.sin(a);
              } else {
                d -= (Math.PI / 2) * r;
                if (d < straightW) {
                  x = w - pad - r - d;
                  y = h - pad;
                } else {
                  d -= straightW;
                  if (d < (Math.PI / 2) * r) {
                    const a = Math.PI / 2 + d / r;
                    x = pad + r + r * Math.cos(a);
                    y = pad + r + straightH + r * Math.sin(a);
                  } else {
                    d -= (Math.PI / 2) * r;
                    if (d < straightH) {
                      x = pad;
                      y = h - pad - r - d;
                    } else {
                      d -= straightH;
                      const a = Math.PI + d / r;
                      x = pad + r + r * Math.cos(a);
                      y = pad + r + r * Math.sin(a);
                    }
                  }
                }
              }
            }
          }
        }
        const n = noise(p * 18 + t, t * 0.7) * 2.2;
        const nx = x + n * Math.cos(p * Math.PI * 2);
        const ny = y + n * Math.sin(p * Math.PI * 2);
        if (i === 0) ctx.moveTo(nx, ny);
        else ctx.lineTo(nx, ny);
      }
      ctx.closePath();
      ctx.stroke();
      t += 0.016;
      raf = requestAnimationFrame(draw);
    }

    resize();
    draw();
    window.addEventListener("resize", resize);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", resize);
    };
  }

  function protectNestedScroll() {
    document.querySelectorAll("#chat, [data-lenis-prevent]").forEach((el) => {
      el.addEventListener(
        "wheel",
        (event) => {
          // Keep wheel local when the panel can still scroll.
          const atTop = el.scrollTop <= 0;
          const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 1;
          if ((event.deltaY < 0 && atTop) || (event.deltaY > 0 && atBottom)) return;
          event.stopPropagation();
        },
        { passive: true },
      );
    });
  }

  function boot() {
    initVanta();
    initLenisGsap();
    protectNestedScroll();
    document.querySelectorAll("[data-electric]").forEach((el) => initElectricBorder(el));
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => setTimeout(boot, 30));
  } else {
    setTimeout(boot, 30);
  }
})();
