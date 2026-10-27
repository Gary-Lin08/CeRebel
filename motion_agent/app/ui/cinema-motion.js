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

  // Landing "The fix": indexed findings (same job as playground Swing analysis).
  (function initLpFix() {
    const root = document.querySelector("[data-lp-fix]");
    if (!root) return;
    const indexEl = document.getElementById("lpFixIndex");
    const othersEl = document.getElementById("lpFixOthers");
    const othersBlock = document.getElementById("lpFixOthersBlock");
    const els = {
      kicker: document.getElementById("lpFixKicker"),
      metric: document.getElementById("lpFixMetric"),
      caption: document.getElementById("lpFixCaption"),
      headline: document.getElementById("lpFixHeadline"),
      plain: document.getElementById("lpFixPlain"),
      whyBlock: document.getElementById("lpFixWhyBlock"),
      why: document.getElementById("lpFixWhy"),
      cueBlock: document.getElementById("lpFixCueBlock"),
      cue: document.getElementById("lpFixCue"),
      drillBlock: document.getElementById("lpFixDrillBlock"),
      drill: document.getElementById("lpFixDrill"),
      checkRow: document.getElementById("lpFixCheckRow"),
      check: document.getElementById("lpFixCheck"),
    };
    let findings = [];
    let fi = 0;

    function paint() {
      const f = findings[fi];
      if (!f) return;
      if (els.kicker) {
        els.kicker.textContent = fi === 0 ? "The one thing to fix" : "Also in this swing";
      }
      const metric = (f.metric && f.metric.value) || "—";
      if (els.metric) els.metric.innerHTML = "<span></span>";
      if (els.metric) els.metric.querySelector("span").textContent = metric;
      if (els.caption) els.caption.textContent = (f.metric && f.metric.caption) || "";
      if (els.headline) els.headline.textContent = f.headline || f.title || "";
      if (els.plain) els.plain.textContent = f.plain || f.detail || "";
      const show = (block, node, text) => {
        const on = !!(text && String(text).trim());
        if (block) block.hidden = !on;
        if (node) node.textContent = text || "";
      };
      show(els.whyBlock, els.why, f.why);
      show(els.cueBlock, els.cue, f.cue);
      show(els.drillBlock, els.drill, f.drill);
      if (els.checkRow) {
        const on = !!(f.check && String(f.check).trim());
        els.checkRow.hidden = !on;
        if (els.check) els.check.textContent = f.check || "";
      }
      if (indexEl) {
        [...indexEl.querySelectorAll('[role="tab"]')].forEach((btn, i) => {
          const on = i === fi;
          btn.setAttribute("aria-selected", String(on));
          btn.tabIndex = on ? 0 : -1;
        });
      }
      if (othersEl && othersBlock) {
        othersEl.textContent = "";
        findings.forEach((o, i) => {
          if (i === fi) return;
          const b = document.createElement("button");
          b.type = "button";
          b.className = "lp-analysis__other";
          const title = document.createElement("strong");
          title.textContent = o.headline || o.title || "Finding";
          const line = document.createElement("span");
          line.textContent = o.metric && o.metric.value
            ? o.metric.value + " · " + (o.metric.caption || "")
            : (o.title || "");
          const chev = document.createElement("i");
          chev.setAttribute("aria-hidden", "true");
          chev.textContent = "›";
          b.append(title, line, chev);
          b.addEventListener("click", () => select(i, true));
          othersEl.appendChild(b);
        });
        othersBlock.hidden = findings.length < 2;
      }
    }

    function select(i, focusIndex) {
      if (!findings[i]) return;
      fi = i;
      paint();
      if (focusIndex && indexEl) {
        const btn = indexEl.querySelectorAll('[role="tab"]')[i];
        if (btn) btn.focus();
      }
    }

    function buildIndex() {
      if (!indexEl) return;
      indexEl.textContent = "";
      findings.forEach((f, i) => {
        const b = document.createElement("button");
        b.type = "button";
        b.setAttribute("role", "tab");
        b.id = "lpFixTab-" + (f.id || i);
        b.setAttribute("aria-controls", "lpPanFix");
        b.setAttribute("aria-selected", i === 0 ? "true" : "false");
        b.tabIndex = i === 0 ? 0 : -1;
        const label = (f.metric && f.metric.value) ? f.metric.value + " · " + (f.title || f.headline || "Fix") : (f.title || f.headline || "Fix");
        b.textContent = label;
        b.addEventListener("click", () => select(i));
        b.addEventListener("keydown", (e) => {
          const k = e.key === "ArrowRight" || e.key === "ArrowDown" ? 1
            : e.key === "ArrowLeft" || e.key === "ArrowUp" ? -1 : 0;
          if (!k) return;
          e.preventDefault();
          select((i + k + findings.length) % findings.length, true);
        });
        indexEl.appendChild(b);
      });
    }

    function boot(list) {
      findings = Array.isArray(list) ? list.slice() : [];
      findings.sort((a, b) => Number(b.severity || 0) - Number(a.severity || 0));
      if (!findings.length) return;
      fi = 0;
      buildIndex();
      paint();
    }

    fetch("/static/hero/swing-sample.json")
      .then((r) => r.json())
      .then((data) => boot(data.findings || []))
      .catch(() => {});
  })();

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
