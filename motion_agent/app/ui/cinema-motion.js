/* CeRebel interface motion: headline word reveals, magnetic buttons, the floating nav,
   tabs with a sliding indicator, and drag-over feedback on uploads. Springs live in CSS
   (--spring); this file only sets classes and custom properties. Reduced motion skips it. */
(function () {
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const fine = matchMedia("(hover: hover) and (pointer: fine)").matches;

  // Headlines rise word by word from behind a mask once they come into view.
  document.querySelectorAll("[data-split]").forEach((el) => {
    const words = el.textContent.trim().split(/\s+/);
    el.setAttribute("aria-label", el.textContent.trim());
    el.textContent = "";
    words.forEach((w, i) => {
      const outer = document.createElement("span");
      outer.className = "w";
      outer.setAttribute("aria-hidden", "true");
      const inner = document.createElement("span");
      inner.textContent = w;
      inner.style.setProperty("--i", i);
      outer.appendChild(inner);
      el.append(outer, i < words.length - 1 ? " " : "");
    });
  });
  const reveal = new IntersectionObserver((entries) => {
    entries.forEach((e) => {
      if (!e.isIntersecting) return;
      e.target.classList.add("is-in");
      reveal.unobserve(e.target);
    });
  }, { threshold: 0.2, rootMargin: "0px 0px -8% 0px" });
  function watch() {
    document.querySelectorAll("[data-split], [data-reveal]").forEach((el) => {
      if (reduce) el.classList.add("is-in");
      else if (!el.classList.contains("is-in") && !el.closest("[hidden]")) reveal.observe(el);
    });
  }
  // The landing starts hidden until the page knows you're signed out.
  addEventListener("cerebel:landing", () => requestAnimationFrame(watch));
  const ready = document.fonts && document.fonts.ready ? document.fonts.ready : Promise.resolve();
  ready.then(() => { document.documentElement.classList.add("cx-ready"); watch(); });

  // Buttons lean toward the pointer, at most a few pixels, and a sheen follows it.
  if (fine && !reduce) {
    document.addEventListener("pointermove", (e) => {
      const b = e.target.closest && e.target.closest("[data-magnetic]");
      if (!b) return;
      const r = b.getBoundingClientRect();
      const dx = e.clientX - (r.left + r.width / 2), dy = e.clientY - (r.top + r.height / 2);
      b.style.setProperty("--mx", ((e.clientX - r.left) / r.width) * 100 + "%");
      b.style.setProperty("--tx", Math.max(-6, Math.min(6, dx * 0.18)) + "px");
      b.style.setProperty("--ty", Math.max(-4, Math.min(4, dy * 0.25)) + "px");
    }, { passive: true });
    document.addEventListener("pointerout", (e) => {
      const b = e.target.closest && e.target.closest("[data-magnetic]");
      if (!b || b.contains(e.relatedTarget)) return;
      b.style.setProperty("--tx", "0px");
      b.style.setProperty("--ty", "0px");
    });
  }

  // The landing nav turns into a floating glass bar once you leave the top.
  const nav = document.querySelector("[data-nav]");
  if (nav) {
    const onScroll = () => nav.classList.toggle("is-floating", scrollY > 40);
    addEventListener("scroll", onScroll, { passive: true });
    onScroll();
  }

  // Tabs: arrow keys move between them, the indicator slides under the current one.
  document.querySelectorAll("[data-tabs]").forEach((box) => {
    const tabs = [...box.querySelectorAll('[role="tab"]')];
    const ink = box.querySelector(".tabs__ink");
    function select(tab, focus) {
      tabs.forEach((t) => {
        const on = t === tab;
        t.setAttribute("aria-selected", String(on));
        t.tabIndex = on ? 0 : -1;
        const panel = document.getElementById(t.getAttribute("aria-controls"));
        if (panel) panel.hidden = !on;
      });
      if (focus) tab.focus();
      if (ink) {
        ink.style.width = tab.offsetWidth + "px";
        ink.style.transform = "translateX(" + tab.offsetLeft + "px)";
      }
    }
    tabs.forEach((t, i) => {
      t.addEventListener("click", () => select(t));
      t.addEventListener("keydown", (e) => {
        const k = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
        if (k) { e.preventDefault(); select(tabs[(i + k + tabs.length) % tabs.length], true); }
      });
    });
    const place = () => select(tabs.find((t) => t.getAttribute("aria-selected") === "true") || tabs[0]);
    addEventListener("resize", place);
    addEventListener("cerebel:landing", () => requestAnimationFrame(place));
    ready.then(place);
  });

  // A file dragged over the page wakes the background and lights the upload targets.
  let depth = 0;
  addEventListener("dragenter", (e) => {
    if (!e.dataTransfer || ![...e.dataTransfer.types].includes("Files")) return;
    depth++;
    document.body.classList.add("cx-dragging");
    if (window.cxFluid) window.cxFluid.pulse(1);
  });
  addEventListener("dragleave", () => { depth = Math.max(0, depth - 1); if (!depth) document.body.classList.remove("cx-dragging"); });
  addEventListener("drop", () => { depth = 0; document.body.classList.remove("cx-dragging"); });
})();
