/* EastMoney global market monitor - dashboard + single stock terminal */
(() => {
  "use strict";

  const state = {
    overview: null,
    market: null,
    session: null,
    rows: [],
    total: 0,
    page: 1,
    size: 50,
    sort: "pct",
    order: "desc",
    search: "",
    moversTab: "gainers",
    sparks: {},
    loading: false,
    collapsed: new Set(),
    watch: [],
    signalSource: "limit_up",
    intelSection: "headline",
    intelRows: [],
    intelLoading: false,
    signalRows: [],
    signalLoading: false,
    stock: null,
    stockSecid: null,
    chartMode: "trend",
    flowMode: "intraday",
    drawerTimer: null,
  };

  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const svgEl = (tag, attrs) => {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.entries(attrs || {}).forEach(([k, v]) => node.setAttribute(k, String(v)));
    return node;
  };

  /* ---------------- formatting ---------------- */
  const num = (v) => (v === null || v === undefined || Number.isNaN(v) ? null : Number(v));

  function fmtPrice(v) {
    const n = num(v);
    if (n === null) return "—";
    const abs = Math.abs(n);
    if (abs >= 10000) return n.toLocaleString("en-US", { maximumFractionDigits: 1 });
    if (abs >= 100) return n.toFixed(2);
    if (abs >= 1) return n.toFixed(3);
    if (abs >= 0.01) return n.toFixed(4);
    return n.toFixed(5);
  }

  function fmtPct(v) {
    const n = num(v);
    if (n === null) return "—";
    return `${n > 0 ? "+" : ""}${n.toFixed(2)}%`;
  }

  function fmtSigned(v) {
    const n = num(v);
    if (n === null) return "—";
    return `${n > 0 ? "+" : ""}${n.toFixed(2)}`;
  }

  function fmtBig(v) {
    const n = num(v);
    if (n === null || n === 0) return "—";
    const abs = Math.abs(n);
    if (abs >= 1e12) return `${(n / 1e12).toFixed(2)}万亿`;
    if (abs >= 1e8) return `${(n / 1e8).toFixed(2)}亿`;
    if (abs >= 1e4) return `${(n / 1e4).toFixed(2)}万`;
    return n.toFixed(0);
  }

  function fmtShares(v) {
    const n = num(v);
    if (n === null) return "—";
    if (Math.abs(n) >= 1e8) return `${(n / 1e8).toFixed(2)}亿股`;
    if (Math.abs(n) >= 1e4) return `${(n / 1e4).toFixed(2)}万股`;
    return `${n.toFixed(0)}股`;
  }

  const dirClass = (v) => {
    const n = num(v);
    if (n === null || n === 0) return "flat";
    return n > 0 ? "up" : "down";
  };

  const clockTime = (ts) => {
    if (!ts) return "—";
    return new Date(ts * 1000).toLocaleTimeString("zh-CN", { hour12: false });
  };

  function ago(ts) {
    if (!ts) return "从未";
    const s = Math.max(0, Math.floor(Date.now() / 1000) - ts);
    if (s < 60) return `${s}秒前`;
    if (s < 3600) return `${Math.floor(s / 60)}分钟前`;
    return `${Math.floor(s / 3600)}小时前`;
  }

  function fmtDelta(seconds) {
    const s = Math.max(0, Math.floor(seconds));
    if (s < 60) return `${s}秒`;
    if (s < 3600) return `${Math.floor(s / 60)}分钟`;
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    if (h < 24) return m ? `${h}小时${m}分` : `${h}小时`;
    return `${Math.floor(h / 24)}天${h % 24}小时`;
  }

  const kindLabel = (kind) => ({
    index: "指数", stock: "股票", future: "期货", forex: "外汇",
    bond: "债券", fund: "基金", sector: "板块", commodity: "商品",
  }[kind] || kind || "品种");

  /* ---------------- networking ---------------- */
  async function api(path, options) {
    const res = await fetch(path, Object.assign({ headers: { Accept: "application/json" } }, options));
    if (!res.ok) {
      let msg = `${res.status} ${res.statusText}`;
      try {
        const body = await res.json();
        if (body && body.error) msg = body.error;
      } catch (_) { /* keep default */ }
      throw new Error(msg);
    }
    return res.json();
  }

  let toastTimer = null;
  function toast(message) {
    const node = $("toast");
    node.textContent = message;
    node.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => node.classList.remove("show"), 2600);
  }

  function setLive(mode, label) {
    const pulse = $("livePulse");
    pulse.classList.toggle("off", mode === "off");
    pulse.classList.toggle("warn", mode === "warn");
    $("liveLabel").textContent = label;
  }

  const debounce = (fn, ms) => {
    let t = null;
    return (...args) => {
      clearTimeout(t);
      t = setTimeout(() => fn(...args), ms);
    };
  };

  /* ---------------- charts ---------------- */
  function boxWidth(node, fallback) {
    const w = node.clientWidth - 8;
    return w > 80 ? w : fallback;
  }

  function sparkline(values, width, height, color) {
    const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: "none",
                               width: "100%", height: "100%" });
    if (!values || values.length < 2) return svg;
    const min = Math.min(...values), max = Math.max(...values), span = max - min || 1;
    const step = width / (values.length - 1);
    let d = "";
    values.forEach((v, i) => {
      d += `${i === 0 ? "M" : "L"}${(i * step).toFixed(1)},${(height - ((v - min) / span) * (height - 2) - 1).toFixed(1)}`;
    });
    svg.appendChild(svgEl("path", { d, fill: "none", stroke: color, "stroke-width": 1.3,
                                    "vector-effect": "non-scaling-stroke" }));
    svg.appendChild(svgEl("path", { d: `${d}L${width},${height}L0,${height}Z`, fill: color, opacity: 0.08 }));
    return svg;
  }

  function emptyChart(node, message) {
    node.replaceChildren();
    const w = boxWidth(node, 600), h = 200;
    const svg = svgEl("svg", { viewBox: `0 0 ${w} ${h}`, width: "100%", height: `${h}px` });
    const t = svgEl("text", { x: w / 2, y: h / 2, fill: "#7f92ad", "font-size": 11, "text-anchor": "middle" });
    t.textContent = message;
    svg.appendChild(t);
    node.appendChild(svg);
  }

  const UP = "#f6465d", DOWN = "#16c784", GRID = "#16223a", AXIS = "#7f92ad";

  /* Crosshair + tooltip shared by every chart. Coordinates are viewBox units,
     so the mapping stays correct when the SVG is scaled to the container. */
  function attachHover(node, svg, opts) {
    const { w, h, xs, yFor, html, plot } = opts;
    if (!xs || xs.length < 2) return;
    const tip = el("div", "chart-tip");
    node.appendChild(tip);
    const g = svgEl("g", { class: "xhair" });
    const vline = svgEl("line", { y1: plot.top, y2: plot.bottom, stroke: "#6b8ab5",
                                  "stroke-width": 1, "stroke-dasharray": "3 3" });
    const hline = svgEl("line", { x1: plot.left, x2: plot.right, stroke: "#41536f",
                                  "stroke-width": 1, "stroke-dasharray": "3 3" });
    const dot = svgEl("circle", { r: 2.8, fill: "#e8f0fb", stroke: "#08101c", "stroke-width": 1.2 });
    const stampBg = svgEl("rect", { y: plot.bottom + 2, height: 13, rx: 2,
                                    fill: "#0b1424", stroke: "#22334f", "stroke-width": 1 });
    const stamp = svgEl("text", { y: plot.bottom + 12, fill: "#c3d2e8", "font-size": 9,
                                  "text-anchor": "middle" });
    g.append(vline, hline, dot, stampBg, stamp);
    svg.appendChild(g);

    let active = -1;

    function nearest(clientX) {
      const rect = svg.getBoundingClientRect();
      if (!rect.width) return -1;
      const vx = (clientX - rect.left) * (w / rect.width);
      let lo = 0, hi = xs.length - 1;
      while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (xs[mid] < vx) lo = mid + 1; else hi = mid;
      }
      if (lo > 0 && Math.abs(xs[lo - 1] - vx) < Math.abs(xs[lo] - vx)) lo -= 1;
      return Math.abs(xs[lo] - vx) <= Math.max(6, (xs[1] - xs[0]) * 0.75) ? lo : -1;
    }

    function show(index) {
      const i = index;
      if (i < 0 || i >= xs.length) { hide(); return; }
      active = i;
      const x = xs[i];
      const y = yFor ? yFor(i) : null;
      vline.setAttribute("x1", x); vline.setAttribute("x2", x);
      if (y === null || Number.isNaN(y)) {
        hline.style.display = "none"; dot.style.display = "none";
      } else {
        hline.style.display = ""; dot.style.display = "";
        hline.setAttribute("y1", y); hline.setAttribute("y2", y);
        dot.setAttribute("cx", x); dot.setAttribute("cy", y);
      }
      g.classList.add("on");
      if (opts.label) {
        const text = opts.label(i) || "";
        stamp.textContent = text;
        const width = Math.max(26, text.length * 5.4 + 8);
        stampBg.setAttribute("x", x - width / 2);
        stampBg.setAttribute("width", width);
        stamp.setAttribute("x", x);
      } else {
        stamp.textContent = "";
        stampBg.setAttribute("width", 0);
      }
      tip.innerHTML = html(i);
      tip.classList.add("on");
      const leftPct = Math.min(88, Math.max(12, (x / w) * 100));
      tip.style.left = `${leftPct}%`;
      if (y !== null && !Number.isNaN(y) && y < 64) {
        tip.style.top = `${((y + 12) / h) * 100}%`;
        tip.style.transform = "translate(-50%, 0)";
      } else {
        tip.style.top = `${(((y ?? plot.top) - 8) / h) * 100}%`;
        tip.style.transform = "translate(-50%, -100%)";
      }
    }

    function hide() {
      active = -1;
      g.classList.remove("on");
      tip.classList.remove("on");
    }

    svg.addEventListener("pointermove", (e) => show(nearest(e.clientX)));
    svg.addEventListener("pointerdown", (e) => show(nearest(e.clientX)));
    svg.addEventListener("pointerleave", hide);
    svg.addEventListener("pointercancel", hide);
    svg.addEventListener("pointerup", (e) => {
      if (e.pointerType !== "mouse") setTimeout(hide, 1800);
    });
    return { hide };
  }

  function tipRow(label, value, cls) {
    return `<div class="tip-row"><span class="tip-k">${label}</span>` +
           `<span class="tip-v${cls ? ` ${cls}` : ""}">${value}</span></div>`;
  }

  function tipDate(text) {
    return `<div class="tip-date">${text}</div>`;
  }


  function drawTrend(node, points, preClose) {
    const w = boxWidth(node, 700), h = Math.max(240, node.clientHeight - 8);
    const padL = 52, padR = 46, padT = 10, padB = 20;
    const plotW = w - padL - padR, plotH = h - padT - padB;
    const values = points.map((p) => p.price).filter((v) => v !== null);
    if (values.length < 2) { emptyChart(node, "该品种暂无分时数据"); return; }
    const base = num(preClose) || values[0];
    const min = Math.min(...values, base), max = Math.max(...values, base);
    const span = max - min || Math.abs(max) * 0.001 || 1;
    const x = (i) => padL + (i / (points.length - 1)) * plotW;
    const y = (v) => padT + plotH - ((v - min) / span) * plotH;

    const svg = svgEl("svg", { viewBox: `0 0 ${w} ${h}`, width: "100%", height: `${h}px` });
    for (let i = 0; i <= 4; i += 1) {
      const gy = padT + (plotH / 4) * i;
      svg.appendChild(svgEl("line", { x1: padL, x2: w - padR, y1: gy, y2: gy, stroke: GRID, "stroke-width": 1 }));
      const price = max - (span / 4) * i;
      const label = svgEl("text", { x: padL - 6, y: gy + 3, fill: AXIS, "font-size": 9, "text-anchor": "end" });
      label.textContent = fmtPrice(price);
      svg.appendChild(label);
      const pct = ((price - base) / base) * 100;
      const right = svgEl("text", { x: w - padR + 6, y: gy + 3, fill: pct >= 0 ? UP : DOWN,
                                    "font-size": 9 });
      right.textContent = `${pct > 0 ? "+" : ""}${pct.toFixed(2)}%`;
      svg.appendChild(right);
    }
    svg.appendChild(svgEl("line", { x1: padL, x2: w - padR, y1: y(base), y2: y(base),
                                    stroke: "#4a5b78", "stroke-width": 1, "stroke-dasharray": "4 4" }));
    let d = "";
    points.forEach((p, i) => { if (p.price !== null) d += `${d ? "L" : "M"}${x(i).toFixed(1)},${y(p.price).toFixed(1)}`; });
    const last = values[values.length - 1];
    const color = last >= base ? UP : DOWN;
    const grad = svgEl("linearGradient", { id: "trendFill", x1: 0, y1: 0, x2: 0, y2: 1 });
    grad.appendChild(svgEl("stop", { offset: "0%", "stop-color": color, "stop-opacity": 0.28 }));
    grad.appendChild(svgEl("stop", { offset: "100%", "stop-color": color, "stop-opacity": 0 }));
    const defs = svgEl("defs", {});
    defs.appendChild(grad);
    svg.appendChild(defs);
    svg.appendChild(svgEl("path", { d: `${d}L${x(points.length - 1)},${padT + plotH}L${padL},${padT + plotH}Z`,
                                    fill: "url(#trendFill)" }));
    svg.appendChild(svgEl("path", { d, fill: "none", stroke: color, "stroke-width": 1.4 }));
    node.replaceChildren(svg);
    attachHover(node, svg, {
      w, h,
      xs: points.map((_, i) => x(i)),
      yFor: (i) => (points[i].price === null || points[i].price === undefined
        ? NaN : y(points[i].price)),
      plot: { left: padL, right: w - padR, top: padT, bottom: padT + plotH },
      label: (i) => String(points[i].t || "").slice(-5),
      html: (i) => {
        const p = points[i];
        const pct = p.price === null || p.price === undefined
          ? null : ((p.price - base) / base) * 100;
        return tipDate(p.t || p.time || "") +
          tipRow("价格", fmtPrice(p.price)) +
          tipRow("涨跌", pct === null ? "—"
            : `${pct > 0 ? "+" : ""}${pct.toFixed(2)}%`, pct === null ? "" : pct >= 0 ? "up" : "down") +
          tipRow("昨收", fmtPrice(base));
      },
    });
  }

  function drawCandles(node, bars) {
    const w = boxWidth(node, 700), h = Math.max(240, node.clientHeight - 8);
    if (!bars || bars.length < 2) { emptyChart(node, "暂无K线数据"); return; }
    const rows = bars.slice(-70);
    const padL = 52, padR = 46, padT = 10, padB = 18;
    const plotW = w - padL - padR;
    const volH = Math.min(56, (h - padT - padB) * 0.24);
    const priceH = h - padT - padB - volH - 8;
    const highs = rows.map((b) => b.high), lows = rows.map((b) => b.low);
    const max = Math.max(...highs), min = Math.min(...lows);
    const span = max - min || Math.abs(max) * 0.001 || 1;
    const maxVol = Math.max(...rows.map((b) => b.volume || 0), 1);
    const step = plotW / rows.length;
    const bodyW = Math.max(1.4, step * 0.58);
    const y = (v) => padT + priceH - ((v - min) / span) * priceH;
    const volY = (v) => h - padB - (v / maxVol) * volH;

    const svg = svgEl("svg", { viewBox: `0 0 ${w} ${h}`, width: "100%", height: `${h}px` });
    for (let i = 0; i <= 4; i += 1) {
      const gy = padT + (priceH / 4) * i;
      svg.appendChild(svgEl("line", { x1: padL, x2: w - padR, y1: gy, y2: gy, stroke: GRID, "stroke-width": 1 }));
      const label = svgEl("text", { x: padL - 6, y: gy + 3, fill: AXIS, "font-size": 9, "text-anchor": "end" });
      label.textContent = fmtPrice(max - (span / 4) * i);
      svg.appendChild(label);
    }
    rows.forEach((b, i) => {
      const cx = padL + i * step + step / 2;
      const up = b.close >= b.open;
      const color = up ? UP : DOWN;
      svg.appendChild(svgEl("line", { x1: cx, x2: cx, y1: y(b.high), y2: y(b.low), stroke: color,
                                      "stroke-width": 1 }));
      const top = y(Math.max(b.open, b.close));
      const height = Math.max(1, Math.abs(y(b.open) - y(b.close)));
      svg.appendChild(svgEl("rect", { x: cx - bodyW / 2, y: top, width: bodyW, height,
                                      fill: up ? color : color, opacity: up ? 0.9 : 0.9 }));
      svg.appendChild(svgEl("rect", { x: cx - bodyW / 2, y: volY(b.volume || 0), width: bodyW,
                                      height: Math.max(0.6, h - padB - volY(b.volume || 0)),
                                      fill: color, opacity: 0.45 }));
    });
    const first = rows[0].d, lastBar = rows[rows.length - 1].d;
    [first, lastBar].forEach((label, idx) => {
      const t = svgEl("text", { x: idx === 0 ? padL : w - padR, y: h - 5, fill: AXIS, "font-size": 9,
                                "text-anchor": idx === 0 ? "start" : "end" });
      t.textContent = label;
      svg.appendChild(t);
    });
    node.replaceChildren(svg);
    attachHover(node, svg, {
      w, h,
      xs: rows.map((_, i) => padL + i * step + step / 2),
      yFor: (i) => y((rows[i].open + rows[i].close) / 2),
      plot: { left: padL, right: w - padR, top: padT, bottom: h - padB },
      label: (i) => rows[i].d,
      html: (i) => {
        const b = rows[i];
        const prev = i > 0 ? rows[i - 1].close : b.open;
        const pct = prev ? ((b.close - prev) / prev) * 100 : (b.pct ?? 0);
        const amp = prev ? ((b.high - b.low) / prev) * 100 : 0;
        const cls = pct >= 0 ? "up" : "down";
        const chg = ((b.close - (prev || b.open)));
        return tipDate(b.d) +
          tipRow("开", fmtPrice(b.open)) +
          tipRow("高", fmtPrice(b.high), "up") +
          tipRow("低", fmtPrice(b.low), "down") +
          tipRow("收", fmtPrice(b.close), cls) +
          tipRow("涨跌", `${pct > 0 ? "+" : ""}${pct.toFixed(2)}% (${chg > 0 ? "+" : ""}${chg.toFixed(2)})`, cls) +
          tipRow("振幅", `${amp.toFixed(2)}%`) +
          tipRow("成交量", fmtBig(b.volume)) +
          tipRow("成交额", fmtBig(b.amount));
      },
    });
  }

  function drawFlow(node, rows, mode) {
    const w = boxWidth(node, 700), h = Math.max(110, node.clientHeight - 6);
    if (!rows || !rows.length) { emptyChart(node, "暂无资金流数据"); return; }
    const padL = 50, padR = 46, padT = 8, padB = 22;
    const plotW = w - padL - padR, plotH = h - padT - padB;
    const svg = svgEl("svg", { viewBox: `0 0 ${w} ${h}`, width: "100%", height: `${h}px` });

    if (mode === "daily") {
      const shown = rows.slice(-20);
      const values = shown.map((r) => r.main || 0);
      const max = Math.max(...values, 0), min = Math.min(...values, 0);
      const span = max - min || 1;
      const zeroY = padT + plotH - ((0 - min) / span) * plotH;
      svg.appendChild(svgEl("line", { x1: padL, x2: w - padR, y1: zeroY, y2: zeroY,
                                      stroke: "#31415c", "stroke-width": 1 }));
      const step = plotW / Math.max(values.length, 12);
      const barW = Math.min(22, Math.max(2, step * 0.7));
      values.forEach((v, i) => {
        const yv = padT + plotH - ((v - min) / span) * plotH;
        const top = Math.min(yv, zeroY);
        svg.appendChild(svgEl("rect", {
          x: padL + i * step + (step - barW) / 2, y: top,
          width: barW, height: Math.max(0.8, Math.abs(yv - zeroY)),
          fill: v >= 0 ? UP : DOWN, opacity: 0.8,
        }));
      });
      [max, min].forEach((v) => {
        const gy = padT + plotH - ((v - min) / span) * plotH;
        const t = svgEl("text", { x: padL - 6, y: gy + 3, fill: AXIS, "font-size": 9, "text-anchor": "end" });
        t.textContent = v === 0 ? "0" : fmtBig(v);
        svg.appendChild(t);
      });
      node.replaceChildren(svg);
      attachHover(node, svg, {
        w, h,
        xs: shown.map((_, i) => padL + i * step + step / 2),
        yFor: (i) => padT + plotH - ((values[i] - min) / span) * plotH,
        plot: { left: padL, right: w - padR, top: padT, bottom: h - padB },
        label: (i) => String(shown[i].t || shown[i].date || "").slice(0, 10),
        html: (i) => {
          const r = shown[i];
          const main = r.main || 0;
          const cls = main >= 0 ? "up" : "down";
          return tipDate(r.t || r.date || "") +
            tipRow("主力净额", fmtBig(main), cls) +
            `<div class="tip-sep"></div>` +
            tipRow("超大单", fmtBig(r.xlarge), (r.xlarge || 0) >= 0 ? "up" : "down") +
            tipRow("大单", fmtBig(r.big), (r.big || 0) >= 0 ? "up" : "down") +
            tipRow("中单", fmtBig(r.mid), (r.mid || 0) >= 0 ? "up" : "down") +
            tipRow("小单", fmtBig(r.small), (r.small || 0) >= 0 ? "up" : "down");
        },
      });
      return;
    }

    // intraday: EastMoney already returns cumulative net inflow per minute
    const values = rows.map((r) => r.main || 0);
    const max = Math.max(...values, 0), min = Math.min(...values, 0);
    const span = max - min || 1;
    const zeroY = padT + plotH - ((0 - min) / span) * plotH;
    const x = (i) => padL + (i / Math.max(1, values.length - 1)) * plotW;
    const y = (v) => padT + plotH - ((v - min) / span) * plotH;
    svg.appendChild(svgEl("line", { x1: padL, x2: w - padR, y1: zeroY, y2: zeroY,
                                    stroke: "#31415c", "stroke-width": 1 }));
    let d = "";
    values.forEach((v, i) => { d += `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)}`; });
    const color = values[values.length - 1] >= 0 ? UP : DOWN;
    const grad = svgEl("linearGradient", { id: "flowFill", x1: 0, y1: 0, x2: 0, y2: 1 });
    grad.appendChild(svgEl("stop", { offset: "0%", "stop-color": color, "stop-opacity": 0.25 }));
    grad.appendChild(svgEl("stop", { offset: "100%", "stop-color": color, "stop-opacity": 0 }));
    const defs = svgEl("defs", {});
    defs.appendChild(grad);
    svg.appendChild(defs);
    svg.appendChild(svgEl("path", { d: `${d}L${x(values.length - 1)},${zeroY}L${padL},${zeroY}Z`,
                                    fill: "url(#flowFill)" }));
    svg.appendChild(svgEl("path", { d, fill: "none", stroke: color, "stroke-width": 1.3 }));
    [max, min].forEach((v) => {
      const t = svgEl("text", { x: padL - 6, y: y(v) + 3, fill: AXIS, "font-size": 9, "text-anchor": "end" });
      t.textContent = v === 0 ? "0" : fmtBig(v);
      svg.appendChild(t);
    });
    const lastLabel = svgEl("text", { x: w - padR + 4, y: y(values[values.length - 1]) + 3,
                                      fill: color, "font-size": 9 });
    lastLabel.textContent = fmtBig(values[values.length - 1]) === "—" ? "0" : fmtBig(values[values.length - 1]);
    svg.appendChild(lastLabel);
    node.replaceChildren(svg);
    attachHover(node, svg, {
      w, h,
      xs: values.map((_, i) => x(i)),
      yFor: (i) => y(values[i]),
      plot: { left: padL, right: w - padR, top: padT, bottom: h - padB },
      label: (i) => String((rows[i] || {}).t || "").slice(-5),
      html: (i) => {
        const r = rows[i] || {};
        const main = values[i];
        return tipDate(r.t || "") +
          tipRow("主力净额", fmtBig(main), main >= 0 ? "up" : "down") +
          `<div class="tip-sep"></div>` +
          tipRow("超大单", fmtBig(r.xlarge), (r.xlarge || 0) >= 0 ? "up" : "down") +
          tipRow("大单", fmtBig(r.big), (r.big || 0) >= 0 ? "up" : "down") +
          tipRow("中单", fmtBig(r.mid), (r.mid || 0) >= 0 ? "up" : "down") +
          tipRow("小单", fmtBig(r.small), (r.small || 0) >= 0 ? "up" : "down");
      },
    });
  }

  /* ---------------- navigation ---------------- */
  function marketStats(m) {
    return m && m.stats && Object.keys(m.stats).length ? m.stats : null;
  }

  function navItem(id, name, value, active, onClick, closed) {
    const item = el("div", "nav-item");
    if (active) item.classList.add("active");
    if (closed) item.classList.add("closed");
    item.appendChild(el("span", "nname", name));
    const val = el("span", "nval", value.text);
    if (value.cls) val.classList.add(value.cls);
    item.appendChild(val);
    item.addEventListener("click", onClick);
    return item;
  }

  function renderNav() {
    const nav = $("marketNav");
    nav.replaceChildren();

    // watchlist first
    const watchGroup = el("div", "nav-group");
    const watchHead = el("div", "nav-group-head");
    watchHead.append(el("span", "caret", "▾"), el("span", "glabel", "自选股"));
    const wcount = el("span", "gavg", String(state.watch.length));
    wcount.classList.add("flat");
    watchHead.appendChild(wcount);
    watchHead.addEventListener("click", () => {
      watchGroup.classList.toggle("collapsed");
    });
    const watchItems = el("div", "nav-items");
    if (!state.watch.length) {
      const hint = el("div", "nav-item");
      hint.style.cursor = "default";
      hint.appendChild(el("span", "nname", "点击任意品种加星"));
      watchItems.appendChild(hint);
    }
    state.watch.forEach((w) => {
      watchItems.appendChild(navItem(
        w.secid, w.name || w.secid,
        { text: w.pct === null || w.pct === undefined ? "—" : fmtPct(w.pct), cls: dirClass(w.pct) },
        state.stockSecid === w.secid,
        () => openStock(w.secid)));
    });
    watchGroup.append(watchHead, watchItems);
    nav.appendChild(watchGroup);

    const groups = state.overview ? state.overview.groups : [];
    groups.forEach((group) => {
      const wrap = el("div", "nav-group");
      if (state.collapsed.has(group.id)) wrap.classList.add("collapsed");
      const head = el("div", "nav-group-head");
      const live = group.markets.filter((m) => marketStats(m));
      const mean = live.length
        ? live.reduce((sum, m) => sum + (num(marketStats(m).avg_pct) || 0), 0) / live.length
        : null;
      head.append(el("span", "caret", "▾"), el("span", "glabel", group.name));
      const avg = el("span", "gavg", mean === null ? "—" : fmtPct(mean));
      avg.classList.add(dirClass(mean));
      head.appendChild(avg);
      head.title = group.desc;
      head.addEventListener("click", () => {
        if (state.collapsed.has(group.id)) state.collapsed.delete(group.id);
        else state.collapsed.add(group.id);
        wrap.classList.toggle("collapsed");
      });
      const items = el("div", "nav-items");
      group.markets.forEach((m) => {
        const st = marketStats(m);
        const closed = m.session && !m.session.open;
        const item = navItem(
          m.id, m.name,
          st ? { text: fmtPct(st.avg_pct), cls: dirClass(st.avg_pct) }
             : { text: m.stale ? "·" : "…", cls: "flat" },
          state.market && state.market.id === m.id,
          () => selectMarket(m.id), closed);
        if (m.session) {
          item.title = `${m.name} · ${m.session.text}`
            + (m.session.open_in ? ` · ${fmtDelta(m.session.open_in)}后开盘` : "")
            + (m.session.close_in ? ` · ${fmtDelta(m.session.close_in)}后收盘` : "");
        }
        items.appendChild(item);
      });
      wrap.append(head, items);
      nav.appendChild(wrap);
    });
  }

  /* ---------------- dashboard panels ---------------- */
  function renderTape() {
    const tape = $("tape");
    tape.replaceChildren();
    (state.overview ? state.overview.tape : []).forEach((r) => {
      const cell = el("div", "tape-cell");
      cell.appendChild(el("div", "tc-name", r.name));
      const row = el("div", "tc-row");
      const price = el("span", "tc-price", fmtPrice(r.price));
      const pct = el("span", "tc-pct", fmtPct(r.pct));
      pct.classList.add(dirClass(r.pct));
      price.classList.add(dirClass(r.pct));
      row.append(price, pct);
      cell.appendChild(row);
      const sparkHost = el("div", "tc-spark");
      const values = (state.sparks[r.secid] || []).map((p) => p[1]);
      if (values.length > 2) {
        sparkHost.appendChild(sparkline(values, 130, 18, num(r.pct) >= 0 ? UP : DOWN));
        sparkHost.style.opacity = "0.85";
      }
      cell.appendChild(sparkHost);
      cell.addEventListener("click", () => openStock(r.secid));
      cell.addEventListener("mouseenter", () => prefetchStock(r.secid), { once: true });
      tape.appendChild(cell);
    });
    const stamps = (state.overview ? state.overview.tape : []).map((r) => r.updated_at).filter(Boolean);
    $("tapeSub").textContent = stamps.length
      ? `${stamps.length} 个指数 · 最新 ${clockTime(Math.max(...stamps))}`
      : "等待首个采集周期";
  }

  function renderHeat() {
    const host = $("heatGrid");
    host.replaceChildren();
    const cells = [];
    (state.overview ? state.overview.groups : []).forEach((g) =>
      g.markets.forEach((m) => { if (m.primary) cells.push(m); }));
    cells.sort((a, b) => (num((marketStats(b) || {}).avg_pct) || -99) - (num((marketStats(a) || {}).avg_pct) || -99));
    cells.forEach((m) => {
      const st = marketStats(m);
      const cell = el("div", "heat-cell");
      if (state.market && state.market.id === m.id) cell.style.background = "#12233c";
      const top = el("div", "hc-top");
      top.append(el("span", "hc-name", m.name));
      const pct = el("span", "hc-pct", st ? fmtPct(st.avg_pct) : "—");
      pct.classList.add(st ? dirClass(st.avg_pct) : "flat");
      top.appendChild(pct);
      cell.appendChild(top);
      const bar = el("div", "heat-bar");
      const up = st ? st.up || 0 : 0;
      const down = st ? st.down || 0 : 0;
      const all = Math.max(1, up + down + (st ? st.flat || 0 : 0));
      const upBar = el("i"); upBar.style.width = `${(up / all) * 100}%`; upBar.style.background = UP;
      const downBar = el("i"); downBar.style.width = `${(down / all) * 100}%`; downBar.style.background = DOWN;
      bar.append(upBar, downBar);
      cell.appendChild(bar);
      const sub = el("div", "hc-sub");
      sub.append(el("span", null, `${up}涨 / ${down}跌`), el("span", null, st ? fmtBig(st.amount) : "—"));
      cell.appendChild(sub);
      const pick = st && st.top_gain ? st.top_gain : null;
      cell.appendChild(el("div", "hc-top-pick", pick ? `领涨 ${pick.name} ${fmtPct(pick.pct)}` : "尚无数据"));
      cell.addEventListener("click", () => selectMarket(m.id));
      host.appendChild(cell);
    });
  }

  let lastMovers = null;
  function renderMovers() {
    const list = $("movers");
    list.replaceChildren();
    const movers = lastMovers || (state.overview && state.overview.movers) || {};
    const rows = movers[state.moversTab] || [];
    if (!rows.length) { list.appendChild(el("li", "empty", "等待数据…")); return; }
    rows.forEach((r, i) => {
      const li = el("li");
      li.appendChild(el("span", "mv-rank", String(i + 1).padStart(2, "0")));
      const wrap = el("div");
      wrap.append(el("div", "mv-name", r.name), el("div", "mv-code", `${r.code} · ${kindLabel(r.kind)}`));
      li.appendChild(wrap);
      li.appendChild(el("span", "mv-price", fmtPrice(r.price)));
      const pct = el("span", "mv-pct", fmtPct(r.pct));
      pct.classList.add(dirClass(r.pct));
      li.appendChild(pct);
      li.addEventListener("click", () => openStock(r.secid));
      list.appendChild(li);
    });
  }

  function renderStatRow() {
    const host = $("statRow");
    host.replaceChildren();
    const st = state.stats || {};
    if (!st || st.total === undefined) {
      host.appendChild(el("span", null, "等待该市场首个采集周期"));
      return;
    }
    const add = (label, value, cls) => {
      const span = el("span", null, `${label} `);
      span.appendChild(el("b", cls || null, value));
      host.appendChild(span);
    };
    add("品种", String(st.total || 0));
    add("上涨", String(st.up || 0), "up");
    add("下跌", String(st.down || 0), "down");
    add("平盘", String(st.flat || 0), "flat");
    if (st.limit_up || st.limit_down) add("涨跌停", `${st.limit_up || 0}/${st.limit_down || 0}`);
    add("均幅", fmtPct(st.avg_pct), dirClass(st.avg_pct));
    add("成交额", fmtBig(st.amount));
  }

  function renderTable() {
    const body = $("quoteBody");
    body.replaceChildren();
    if (!state.rows.length) {
      const tr = el("tr");
      const td = el("td", "empty", state.loading ? "加载中…" : "没有匹配的数据");
      td.colSpan = 10;
      tr.appendChild(td);
      body.appendChild(tr);
      return;
    }
    state.rows.forEach((r, idx) => {
      const tr = el("tr");
      tr.dataset.secid = r.secid;
      if (state.stockSecid === r.secid) tr.classList.add("selected");
      tr.appendChild(el("td", "col-idx", String((state.page - 1) * state.size + idx + 1)));
      tr.appendChild(el("td", "cell-code", r.code));
      tr.appendChild(el("td", "cell-name", r.name));
      const price = el("td", "num", fmtPrice(r.price));
      price.classList.add(dirClass(r.pct));
      tr.appendChild(price);
      const pct = el("td", "num", fmtPct(r.pct));
      pct.classList.add(dirClass(r.pct));
      tr.appendChild(pct);
      const chg = el("td", "num", fmtSigned(r.chg));
      chg.classList.add(dirClass(r.chg));
      tr.appendChild(chg);
      tr.appendChild(el("td", "num", fmtBig(r.amount)));
      tr.appendChild(el("td", "num", num(r.turnover) === null ? "—" : `${num(r.turnover).toFixed(2)}%`));
      tr.appendChild(el("td", "num", fmtBig(r.mcap)));
      const barTd = el("td", "num spark-cell");
      const bar = el("div");
      bar.style.cssText = "height:4px;border-radius:2px;background:#16223a";
      const fill = el("i");
      fill.style.cssText = `display:block;height:100%;width:${Math.max(3, Math.min(100, Math.abs(num(r.amplitude) || 0) * 12))}%;background:${num(r.pct) >= 0 ? UP : DOWN}`;
      bar.appendChild(fill);
      barTd.appendChild(bar);
      tr.appendChild(barTd);
      tr.addEventListener("click", () => openStock(r.secid));
      tr.addEventListener("mouseenter", () => prefetchStock(r.secid), { once: true });
      body.appendChild(tr);
    });
  }

  function renderPager() {
    const pages = Math.max(1, Math.ceil(state.total / state.size));
    $("pageLabel").textContent = `${state.page} / ${pages}`;
    $("pagerInfo").textContent =
      `共 ${state.total.toLocaleString()} 条 · 每页 ${state.size} · 更新于 ${ago(state.marketUpdated)}`;
    $("prevPage").disabled = state.page <= 1;
    $("nextPage").disabled = state.page >= pages;
  }

  function renderMarketHeader() {
    const m = state.market;
    if (!m) return;
    $("marketTitle").textContent = m.name;
    const session = state.session;
    const chip = $("marketChip");
    if (session) {
      chip.textContent = `${m.kind.toUpperCase()} · ${session.text}`;
      chip.classList.toggle("chip-closed", !session.open);
      chip.title = session.open
        ? `距收盘 ${fmtDelta(session.close_in)}`
        : (session.open_in ? `距开盘 ${fmtDelta(session.open_in)}` : session.text);
    } else {
      chip.textContent = `${m.kind.toUpperCase()} · ${m.interval}s`;
    }
    const st = state.stats || {};
    const pieces = [];
    if (st.total) pieces.push(`${st.total} 个品种`);
    pieces.push(`采集 ${ago(state.marketUpdated)}`);
    if (session && !session.open) {
      pieces.push(session.open_in ? `${fmtDelta(session.open_in)}后开盘` : session.text);
    }
    $("marketMeta").textContent = pieces.join(" · ");
  }

  /* ---------------- stock drawer ---------------- */
  function renderDepth(data) {
    const host = $("ladder");
    host.replaceChildren();
    const asks = (data.depth && data.depth.asks) || [];
    const bids = (data.depth && data.depth.bids) || [];
    if (!asks.length && !bids.length) {
      host.appendChild(el("div", "ladder-empty",
        data.partial ? "盘口加载中…" : "无可交易盘口（已收盘或该市场不提供）"));
      $("depthSub").textContent = "—";
      return;
    }
    const maxVol = Math.max(...asks.map((a) => a.volume || 0), ...bids.map((b) => b.volume || 0), 1);
    const row = (tag, price, volume, side) => {
      const r = el("div", "ladder-row");
      r.appendChild(el("span", "lr-tag", tag));
      const p = el("span", "lr-price", fmtPrice(price));
      p.classList.add(side);
      r.appendChild(p);
      r.appendChild(el("span", "lr-vol", volume === null ? "—" : `${volume}`));
      const bar = el("i", "lr-bar");
      bar.style.width = `${Math.max(2, ((volume || 0) / maxVol) * 100)}%`;
      bar.style.background = side === "up" ? UP : DOWN;
      r.appendChild(bar);
      return r;
    };
    asks.slice().reverse().forEach((a, i) => host.appendChild(row(`卖${5 - i}`, a.price, a.volume, "up")));
    host.appendChild(el("div", "ladder-sep"));
    bids.forEach((b, i) => host.appendChild(row(`买${i + 1}`, b.price, b.volume, "down")));
    const ratio = num(data.bid_ratio);
    $("depthSub").textContent = ratio === null ? "—" : `委比 ${ratio.toFixed(2)}%`;
  }

  function renderMetrics(data) {
    const host = $("metrics");
    host.replaceChildren();
    const cn = data.market === 0 || data.market === 1;
    const rows = [
      ["今开", fmtPrice(data.open)], ["昨收", fmtPrice(data.prev_close)],
      ["最高", fmtPrice(data.high)], ["最低", fmtPrice(data.low)],
      ["成交量", data.volume === null ? "—" : (cn ? `${fmtBig(data.volume)}手` : fmtBig(data.volume))],
      ["成交额", fmtBig(data.amount)],
      ["换手率", num(data.turnover) === null ? "—" : `${num(data.turnover).toFixed(2)}%`],
      ["量比", num(data.volume_ratio) === null ? "—" : num(data.volume_ratio).toFixed(2)],
      ["振幅", num(data.amplitude) === null ? "—" : `${num(data.amplitude).toFixed(2)}%`],
      ["均价", fmtPrice(data.avg_price)],
      ["市盈率", num(data.pe) === null ? "—" : num(data.pe).toFixed(2)],
      ["市净率", num(data.pb) === null ? "—" : num(data.pb).toFixed(2)],
      ["总市值", fmtBig(data.mcap)], ["流通市值", fmtBig(data.float_mcap)],
      ["总股本", fmtShares(data.shares)], ["流通股", fmtShares(data.float_shares)],
    ];
    if (cn) {
      rows.push(["涨停", fmtPrice(data.limit_up)], ["跌停", fmtPrice(data.limit_down)]);
    } else {
      rows.push(["每股净资产", num(data.bvps) === null ? "—" : fmtPrice(data.bvps)],
                ["市盈TTM", num(data.pe_ttm) === null ? "—" : num(data.pe_ttm).toFixed(2)]);
    }
    rows.push(["内盘", data.inner === null ? "—" : fmtBig(data.inner)],
              ["外盘", data.outer === null ? "—" : fmtBig(data.outer)]);
    rows.forEach(([k, v]) => {
      const cell = el("div");
      cell.appendChild(el("dt", null, k));
      cell.appendChild(el("dd", null, v));
      host.appendChild(cell);
    });
  }

  function renderTrades(data) {
    const host = $("trades");
    host.replaceChildren();
    const rows = (data.trades || []).slice().reverse();
    if (!rows.length) {
      host.appendChild(el("li", "empty", "该市场当前不提供逐笔成交"));
      $("tradesSub").textContent = "—";
      return;
    }
    rows.forEach((t) => {
      const li = el("li");
      li.appendChild(el("span", "t-time", t.time));
      const p = el("span", null, fmtPrice(t.price));
      p.classList.add(t.side === 1 ? "up" : t.side === 2 ? "down" : "flat");
      li.appendChild(p);
      li.appendChild(el("span", "t-vol", t.volume === null ? "—" : String(t.volume)));
      host.appendChild(li);
    });
    $("tradesSub").textContent = rows.length ? `${rows.length} 笔` : "—";
  }

  function renderChart() {
    const box = $("chartBox");
    const data = state.stock;
    if (!data) return;
    if (state.chartMode === "trend") {
      drawTrend(box, data.trends || [], data.pre_close);
      $("chartSub").textContent = data.trends && data.trends.length
        ? `分时 · ${data.trends.length} 点`
        : (data.partial ? "分时加载中…" : "分时");
    } else {
      const klt = Number(state.chartMode);
      const bars = (state.stockBars && state.stockBars[klt]) || [];
      drawCandles(box, bars);
      $("chartSub").textContent = `${klt === 101 ? "日K" : klt === 102 ? "周K" : "月K"} · ${bars.length} 根`;
    }
  }

  function renderFlow() {
    const box = $("flowBox");
    const data = state.stock;
    if (!data) return;
    if (!data.flow || !data.flow.length) {
      emptyChart(box, "该市场暂不提供资金流数据");
      return;
    }
    if (data.flow_reliable === false) {
      emptyChart(box, "该市场资金流口径不同，暂不展示");
      return;
    }
    const rows = state.flowMode === "daily"
      ? (state.stockDailyFlow || [])
      : (data.flow || []);
    drawFlow(box, rows, state.flowMode);
  }

  function renderDrawer() {
    const data = state.stock;
    if (!data) return;
    $("drawerName").textContent = data.name || data.secid;
    $("drawerCode").textContent = `${data.code || ""} · ${data.secid}`;
    $("drawerBadge").textContent = data.industry || kindLabel(state.stockKind || "stock");
    const last = $("dpLast");
    last.textContent = fmtPrice(data.price);
    last.className = `dp-last ${dirClass(data.pct)}`;
    $("dpChg").textContent = fmtSigned(data.change);
    $("dpChg").className = `dp-chg ${dirClass(data.change)}`;
    $("dpPct").textContent = fmtPct(data.pct);
    $("dpPct").className = `dp-pct ${dirClass(data.pct)}`;
    $("dpSub").textContent =
      `开 ${fmtPrice(data.open)} · 高 ${fmtPrice(data.high)} · 低 ${fmtPrice(data.low)} · `
      + `额 ${fmtBig(data.amount)} · ${data.ts ? new Date(data.ts * 1000).toLocaleTimeString("zh-CN", { hour12: false }) : "—"}`;
    const watching = !!data.watched;
    $("watchBtn").classList.toggle("on", watching);
    $("watchLabel").textContent = watching ? "已自选" : "自选";
    renderChart();
    renderDepth(data);
    renderMetrics(data);
    renderTrades(data);
    renderFlow();
    loadStockSignals(data.secid || state.stockSecid);
  }

  const prefetched = new Map();
  function prefetchStock(secid) {
    if (!secid) return;
    const last = prefetched.get(secid) || 0;
    if (Date.now() - last < 45000) return;
    prefetched.set(secid, Date.now());
    fetch(`/api/stock/${encodeURIComponent(secid)}/prefetch`, { method: "GET" }).catch(() => {});
  }

  async function openStock(secid) {
    if (!secid) return;
    state.stockSecid = secid;
    $("drawer").hidden = false;
    $("drawerScrim").hidden = false;
    document.body.style.overflow = "hidden";
    $("drawerName").textContent = "加载中…";
    $("dpLast").textContent = "—";
    $("dpChg").textContent = "";
    $("dpPct").textContent = "";
    $("dpSub").textContent = "正在拉取盘口 / 分时 / 资金流…";
    $("ladder").textContent = "加载中…";
    $("metrics").replaceChildren();
    $("trades").replaceChildren(el("li", "empty", "加载中…"));
    emptyChart($("chartBox"), "加载中…");
    emptyChart($("flowBox"), "加载中…");
    renderNav();
    renderTable();
    await loadStock(secid);
    if (state.drawerTimer) clearInterval(state.drawerTimer);
    state.drawerTimer = setInterval(() => {
      if (!$("drawer").hidden && !document.hidden) loadStock(secid, true);
    }, 5000);
  }

  async function loadStock(secid, quiet) {
    try {
      const data = await api(`/api/stock/${encodeURIComponent(secid)}`);
      state.stock = data;
      state.stockKind = (state.overview && state.overview.tape.find((t) => t.secid === secid))
        ? "index" : "stock";
      renderDrawer();
      if (state.chartMode !== "trend") await loadBars(Number(state.chartMode));
      // the budget returned a partial payload - top it up shortly, not in 5s
      if (data.partial) {
        const attempt = (state.partialRetries || 0) + 1;
        state.partialRetries = attempt;
        if (attempt <= 3 && secid === state.stockSecid) {
          clearTimeout(state.partialTimer);
          state.partialTimer = setTimeout(() => loadStock(secid, true), 700);
        }
      } else {
        state.partialRetries = 0;
      }
    } catch (err) {
      if (!quiet) {
        $("drawerName").textContent = "加载失败";
        $("dpSub").textContent = err.message;
      }
    }
  }

  async function loadBars(klt) {
    if (!state.stockSecid) return;
    state.stockBars = state.stockBars || {};
    if (state.stockBars[klt] && !state.chartMode) return;
    try {
      const res = await api(`/api/stock/${encodeURIComponent(state.stockSecid)}/kline?klt=${klt}&limit=180`);
      state.stockBars[klt] = res.bars || [];
      if (Number(state.chartMode) === klt) renderChart();
    } catch (_) { /* keep previous render */ }
  }

  async function loadDailyFlow() {
    if (!state.stockSecid) return;
    try {
      const res = await api(`/api/stock/${encodeURIComponent(state.stockSecid)}/flow?limit=20`);
      state.stockDailyFlow = res.rows || [];
      if (state.flowMode === "daily") renderFlow();
    } catch (_) { /* ignore */ }
  }

  function closeDrawer() {
    $("drawer").hidden = true;
    $("drawerScrim").hidden = true;
    document.body.style.overflow = "";
    state.stockSecid = null;
    if (state.drawerTimer) { clearInterval(state.drawerTimer); state.drawerTimer = null; }
    renderNav();
    renderTable();
  }

  async function toggleWatch() {
    const data = state.stock;
    if (!data) return;
    try {
      if (data.watched) {
        await fetch(`/api/watchlist/${encodeURIComponent(data.secid)}`, { method: "DELETE" });
      } else {
        await api("/api/watchlist", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ secid: data.secid, name: data.name }),
        });
      }
      await loadWatchlist();
      data.watched = !data.watched;
      $("watchBtn").classList.toggle("on", data.watched);
      $("watchLabel").textContent = data.watched ? "已自选" : "自选";
      toast(data.watched ? "已加入自选" : "已移出自选");
    } catch (err) {
      toast(`自选操作失败: ${err.message}`);
    }
  }

  async function loadWatchlist() {
    try {
      const data = await api("/api/watchlist");
      state.watch = data.items || [];
      renderNav();
    } catch (_) { /* ignore */ }
  }

  /* ---------------- global search ---------------- */
  let searchActive = 0;
  let searchItems = [];

  function renderSearchResults(results) {
    const box = $("gsearchResults");
    box.replaceChildren();
    searchItems = results;
    searchActive = 0;
    if (!results.length) {
      box.appendChild(el("div", "gsr-empty", "没有匹配的品种"));
      box.hidden = false;
      return;
    }
    results.forEach((r, i) => {
      const item = el("div", "gsr-item");
      if (i === 0) item.classList.add("active");
      item.appendChild(el("span", "gsr-code", r.code));
      item.appendChild(el("span", "gsr-name", r.name));
      item.appendChild(el("span", "gsr-price", fmtPrice(r.price)));
      const pct = el("span", "gsr-pct", fmtPct(r.pct));
      pct.classList.add(dirClass(r.pct));
      item.appendChild(pct);
      item.addEventListener("mousedown", (e) => { e.preventDefault(); pickSearch(i); });
      box.appendChild(item);
    });
    box.hidden = false;
  }

  function pickSearch(index) {
    const row = searchItems[index];
    if (!row) return;
    $("gsearchResults").hidden = true;
    $("gsearchInput").value = "";
    $("gsearchInput").blur();
    openStock(row.secid);
  }

  function moveSearch(delta) {
    if (!searchItems.length) return;
    searchActive = (searchActive + delta + searchItems.length) % searchItems.length;
    [...$("gsearchResults").children].forEach((node, i) => node.classList.toggle("active", i === searchActive));
  }

  function bindSearch() {
    const input = $("gsearchInput");
    const box = $("gsearchResults");
    const run = debounce(async () => {
      const q = input.value.trim();
      if (q.length < 1) { box.hidden = true; return; }
      try {
        const res = await api(`/api/search?q=${encodeURIComponent(q)}`);
        renderSearchResults(res.results || []);
      } catch (_) { box.hidden = true; }
    }, 180);
    input.addEventListener("input", run);
    input.addEventListener("focus", () => { if (searchItems.length) box.hidden = false; });
    input.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown") { e.preventDefault(); moveSearch(1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); moveSearch(-1); }
      else if (e.key === "Enter") { e.preventDefault(); pickSearch(searchActive); }
      else if (e.key === "Escape") { box.hidden = true; input.blur(); }
    });
    document.addEventListener("click", (e) => {
      if (!$("gsearchWrap").contains(e.target)) box.hidden = true;
    });
  }

  /* ---------------- dashboard loading ---------------- */
  function flattenMarkets() {
    if (!state.overview) return [];
    return state.overview.groups.flatMap((g) => g.markets);
  }

  async function loadOverview() {
    try {
      const data = await api("/api/overview");
      state.overview = data;
      $("instrumentCount").textContent = (data.counts.instruments || 0).toLocaleString();
      const proxy = data.proxy || {};
      $("proxyCount").textContent = proxy.enabled ? `${proxy.live || 0} 在线` : "直连";
      $("lastUpdate").textContent = clockTime(data.generated_at);
      $("railFoot").textContent =
        `${data.counts.markets} 个市场 · ${(data.counts.history_points || 0).toLocaleString()} 个历史点`;
      if (!proxy.enabled) setLive("warn", "直连模式");
      else if ((proxy.live || 0) > 0) setLive("on", "数据流正常");
      else if ((proxy.counters || {}).extract_calls) setLive("warn", "代理重连中");
      else setLive("warn", "代理待采集");
      if (!state.market) {
        const all = flattenMarkets();
        const preferred = all.find((m) => m.id === "cn.a") || all.find((m) => !m.stale) || all[0];
        if (preferred) selectMarket(preferred.id, true);
      }
      renderNav();
      renderTape();
      renderHeat();
      lastMovers = data.movers;
      renderMovers();
      loadSparks();
    } catch (err) {
      setLive("off", "接口异常");
      console.warn("overview failed", err);
    }
  }

  async function loadSparks() {
    const secids = (state.overview ? state.overview.tape : []).map((r) => r.secid);
    if (!secids.length) return;
    try {
      const data = await api(`/api/spark?secids=${encodeURIComponent(secids.join(","))}&points=40`);
      state.sparks = data.series || {};
      renderTape();
    } catch (_) { /* sparkline is cosmetic */ }
  }

  async function loadMarket(id, resetPage) {
    if (resetPage) state.page = 1;
    state.loading = true;
    const params = new URLSearchParams({
      page: String(state.page), size: String(state.size),
      sort: state.sort, order: state.order,
    });
    if (state.search) params.set("q", state.search);
    try {
      const data = await api(`/api/market/${encodeURIComponent(id)}?${params}`);
      state.market = data.market;
      state.session = data.session || null;
      state.rows = data.rows;
      state.total = data.total;
      state.stats = data.stats && Object.keys(data.stats).length ? data.stats : null;
      state.marketUpdated = data.updated_at;
      renderMarketHeader();
      renderStatRow();
      renderTable();
      renderPager();
      renderNav();
      renderHeat();
    } catch (err) {
      toast(`加载 ${id} 失败: ${err.message}`);
    } finally {
      state.loading = false;
    }
  }

  function selectMarket(id, silent) {
    if (!id) return;
    state.market = { id, name: "" };
    state.page = 1;
    if (!silent) state.stockSecid = null;
    loadMarket(id, true);
  }

  async function loadStatus() {
    try {
      const data = await api("/api/status");
      const sys = $("sysPanel");
      sys.replaceChildren();
      const proxy = data.proxy || {};
      const counters = proxy.counters || {};
      const row = (label, value) => {
        const r = el("div", "sys-row");
        r.append(el("span", null, label), el("b", null, value));
        sys.appendChild(r);
      };
      row("运行市场", `${data.markets_live}/${data.markets_total}`);
      row("开市中", String(data.markets_open || 0));
      if (data.clock) {
        const clock = data.clock;
        row("UTC 时钟", `${clock.server_utc.slice(11, 19)} · ${clock.source}`);
        row("时钟偏移", `${clock.offset_seconds > 0 ? "+" : ""}${clock.offset_seconds}s`);
      }
      if (data.last_run && data.last_run.finished) {
        row("最近采集", `${data.last_run.markets} 市场 · ${Number(data.last_run.duration).toFixed(1)}s`);
      }
      row("品种总数", (data.counts.instruments || 0).toLocaleString());
      row("历史点", (data.counts.history_points || 0).toLocaleString());
      row("代理在线", proxy.enabled ? `${proxy.live} / ${proxy.tracked}` : "未启用");
      row("请求/错误", `${(data.client.requests || 0).toLocaleString()} / ${data.client.errors || 0}`);
      row("代理命中", `${counters.success || 0} 成功 · ${counters.failures || 0} 失败`);
      const bar = el("div", "sys-bar");
      const fill = el("i");
      const ok = counters.success || 0, bad = counters.failures || 0;
      fill.style.width = `${ok + bad ? (ok / (ok + bad)) * 100 : 0}%`;
      bar.appendChild(fill);
      sys.appendChild(bar);
      if (proxy.last_error) {
        const r = el("div", "sys-row");
        r.append(el("span", "lvl-warn", "代理错误"), el("b", null, proxy.last_error.slice(0, 32)));
        sys.appendChild(r);
      }

      const feed = $("feed");
      feed.replaceChildren();
      (data.events || []).forEach((ev) => {
        const li = el("li");
        li.appendChild(el("time", null, clockTime(ev.ts)));
        li.appendChild(el("span", `lvl-${ev.level}`, `${ev.scope ? ev.scope + " · " : ""}${ev.message}`));
        feed.appendChild(li);
      });
      if (!(data.events || []).length) feed.appendChild(el("li", "empty", "暂无事件"));
      $("lastUpdate").textContent = clockTime(data.time);
    } catch (_) { /* keep last values */ }
  }

  /* ---------------- intel: full-text news ---------------- */
  const INTEL_SECTIONS = [
    { id: "headline", name: "财经导读" },
    { id: "kuaixun", name: "7x24快讯" },
    { id: "viewpoint", name: "东方视点" },
    { id: "global", name: "全球" },
    { id: "topic", name: "焦点专题" },
    { id: "community", name: "社区" },
  ];

  function renderIntelTabs() {
    const host = $("intelTabs");
    host.replaceChildren();
    INTEL_SECTIONS.forEach((sec) => {
      const btn = el("button", "tab", sec.name);
      if (state.intelSection === sec.id) btn.classList.add("active");
      btn.addEventListener("click", () => {
        state.intelSection = sec.id;
        renderIntelTabs();
        loadIntel();
      });
      host.appendChild(btn);
    });
  }

  function renderIntel() {
    const host = $("intelList");
    host.replaceChildren();
    if (!state.intelRows.length) {
      host.appendChild(el("div", "empty", state.intelLoading ? "加载中…" : "暂无内容"));
      return;
    }
    state.intelRows.forEach((item) => {
      const row = el("div", "intel-item");
      const secName = (INTEL_SECTIONS.find((s) => s.id === item.section) || {}).name || item.section;
      row.appendChild(el("span", "ii-sec", secName));
      row.appendChild(el("span", "ii-title", item.title));
      const meta = el("span", "ii-meta");
      const when = item.published ? new Date(item.published * 1000) : null;
      meta.textContent = `${when ? when.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—"}${item.words ? ` · ${item.words}字` : ""}`;
      if (item.words) meta.classList.add("ii-has-body");
      row.appendChild(meta);
      row.addEventListener("click", () => openReader(item.art_id));
      host.appendChild(row);
    });
  }

  async function loadIntel() {
    state.intelLoading = true;
    renderIntel();
    try {
      const res = await api(`/api/intel/articles?section=${encodeURIComponent(state.intelSection)}&days=7&limit=40`);
      state.intelRows = res.items || [];
    } catch (_) {
      state.intelRows = [];
    } finally {
      state.intelLoading = false;
      renderIntel();
      const total = (state.intelRows || []).filter((r) => r.words).length;
      $("intelMeta").textContent = `${state.intelRows.length} 条 · ${total} 条含正文 · 保留 7 天`;
    }
  }

  async function openReader(artId) {
    $("reader").hidden = false;
    $("readerScrim").hidden = false;
    document.body.style.overflow = "hidden";
    $("readerTitle").textContent = "加载中…";
    $("readerBody").textContent = "";
    $("readerStats").textContent = "";
    try {
      const item = await api(`/api/intel/article/${encodeURIComponent(artId)}`);
      const sec = INTEL_SECTIONS.find((s) => s.id === item.section);
      $("readerSection").textContent = sec ? sec.name : item.section;
      $("readerTitle").textContent = item.title || "(无标题)";
      const when = item.published ? new Date(item.published * 1000) : null;
      $("readerMeta").textContent =
        `${item.media || ""} ${when ? when.toLocaleString("zh-CN") : ""}`.trim();
      const body = (item.body || item.summary || "").trim();
      $("readerBody").textContent = body || "该条目只有标题与摘要，正文尚未抓取。";
      $("readerStats").textContent = item.words ? `正文 ${item.words} 字` : "仅摘要";
      $("readerSource").href = item.url || "#";
      $("readerSource").style.visibility = item.url ? "visible" : "hidden";
    } catch (err) {
      $("readerTitle").textContent = "加载失败";
      $("readerBody").textContent = err.message;
    }
  }

  function closeReader() {
    $("reader").hidden = true;
    $("readerScrim").hidden = true;
    document.body.style.overflow = "";
  }

  /* ---------------- T+1 signals ---------------- */
  const SIGNAL_SOURCES = [
    { id: "limit_up", name: "涨停池", cols: ["名称", "连板", "封单额", "行业", "首封"],
      row: (r) => {
        const p = r.payload || {};
        const t = String(p.first_seal || "").padStart(6, "0");
        return [r.name, `${p.boards || 1}板`, fmtBig(p.seal_amount), p.industry || "—",
                `${t.slice(0, 2)}:${t.slice(2, 4)}`];
      }, num: [1, 2] },
    { id: "limit_down", name: "跌停池", cols: ["名称", "跌幅", "封单额", "行业"],
      row: (r) => {
        const p = r.payload || {};
        return [r.name, fmtPct(p.pct), fmtBig(p.seal_amount), p.industry || "—"];
      }, num: [1, 2] },
    { id: "dragon_tiger", name: "龙虎榜", cols: ["名称", "净买入", "涨跌幅", "上榜原因"],
      row: (r) => {
        const p = r.payload || {};
        return [r.name, fmtBig(p.net_buy), fmtPct(p.pct), p.reason || p.seats || "—"];
      }, num: [1, 2] },
    { id: "stock_flow", name: "主力流入", cols: ["名称", "主力净额", "占比", "涨跌幅"],
      row: (r) => {
        const p = r.payload || {};
        return [r.name, fmtBig(p.main_net), p.main_ratio === null ? "—" : `${p.main_ratio}%`,
                fmtPct(p.pct)];
      }, num: [1, 2, 3] },
    { id: "sector_flow", name: "板块资金", cols: ["板块", "主力净额", "涨幅", "类型"],
      row: (r) => {
        const p = r.payload || {};
        return [r.name, fmtBig(p.main_net), fmtPct(p.pct), p.kind === "concept" ? "概念" : "行业"];
      }, num: [1, 2] },
    { id: "margin", name: "两融", cols: ["名称", "融资余额", "融资净买入", "5日净买入"],
      row: (r) => {
        const p = r.payload || {};
        return [r.name, fmtBig(p.balance), fmtBig(p.net_buy), fmtBig(p.net_buy_5d)];
      }, num: [1, 2, 3] },
    { id: "research", name: "研报", cols: ["股票", "标题", "机构", "日期"],
      row: (r) => {
        const p = r.payload || {};
        return [r.name, p.title || "—", p.org || "—", p.date || "—"];
      }, num: [] },
    { id: "announcement", name: "公告", cols: ["股票", "公告标题", "日期"],
      row: (r) => {
        const p = r.payload || {};
        return [r.name, p.title || "—", (p.display_time || p.date || "").slice(0, 16)];
      }, num: [] },
    { id: "popularity", name: "人气榜", cols: ["名称", "排名", "变化"],
      row: (r) => {
        const p = r.payload || {};
        const delta = p.prev_rank && p.rank ? p.prev_rank - p.rank : null;
        return [r.name, `#${p.rank || "—"}`,
                delta === null ? "—" : `${delta > 0 ? "↑" : delta < 0 ? "↓" : "·"}${Math.abs(delta)}`];
      }, num: [1, 2] },
    { id: "connect", name: "沪深港通", cols: ["通道", "成交额", "净买入", "额度余量"],
      row: (r) => {
        const p = r.payload || {};
        return [r.name, fmtBig(p.turnover), fmtBig(p.net_buy), fmtBig(p.remain)];
      }, num: [1, 2, 3] },
  ];

  function signalMeta(id) {
    return SIGNAL_SOURCES.find((s) => s.id === id) || SIGNAL_SOURCES[0];
  }

  function renderSignalTabs() {
    const host = $("signalTabs");
    host.replaceChildren();
    SIGNAL_SOURCES.forEach((src) => {
      const btn = el("button", "tab", src.name);
      btn.dataset.source = src.id;
      if (state.signalSource === src.id) btn.classList.add("active");
      btn.addEventListener("click", () => {
        state.signalSource = src.id;
        renderSignalTabs();
        loadSignals();
      });
      host.appendChild(btn);
    });
  }

  function renderSignals() {
    const meta = signalMeta(state.signalSource);
    const head = $("signalHead");
    const body = $("signalBody");
    head.replaceChildren();
    const tr = el("tr");
    meta.cols.forEach((c, i) => {
      const th = el("th", meta.num.includes(i) ? "num" : null, c);
      tr.appendChild(th);
    });
    head.appendChild(tr);
    body.replaceChildren();
    if (!state.signalRows.length) {
      const empty = el("tr");
      const td = el("td", "empty", state.signalLoading ? "加载中…" : "今日该来源暂无数据");
      td.colSpan = meta.cols.length;
      empty.appendChild(td);
      body.appendChild(empty);
      return;
    }
    state.signalRows.forEach((r) => {
      const row = el("tr");
      const cells = meta.row(r);
      cells.forEach((value, i) => {
        const td = el("td", meta.num.includes(i) ? "num" : null, value === null || value === undefined ? "—" : String(value));
        if (meta.num.includes(i) && typeof value === "string" && value.startsWith("+")) {
          td.classList.add("up");
        }
        row.appendChild(td);
      });
      if (r.stock) {
        row.addEventListener("click", () => openStock(r.stock));
        row.addEventListener("mouseenter", () => prefetchStock(r.stock), { once: true });
      }
      body.appendChild(row);
    });
  }

  async function loadSignals() {
    state.signalLoading = true;
    try {
      const res = await api(`/api/signals/${encodeURIComponent(state.signalSource)}?limit=80`);
      state.signalRows = res.rows || [];
    } catch (_) {
      state.signalRows = [];
    } finally {
      state.signalLoading = false;
      renderSignals();
    }
  }

  async function loadSignalSummary() {
    try {
      const data = await api("/api/signals/summary");
      const day = data.day || "";
      const total = Object.values(data.sources || {}).reduce((n, s) => n + (s.count || 0), 0);
      $("signalsDay").textContent = day ? `${day} · ${total} 条 · 保留 ${data.retention_days} 天` : "—";
    } catch (_) { /* ignore */ }
  }

  function signalText(r) {
    const p = r.payload || {};
    switch (r.source) {
      case "limit_up":
        return `涨停 ${p.boards || 1} 连板 · 封单 ${fmtBig(p.seal_amount)} · ${p.industry || ""}`;
      case "limit_down":
        return `跌停 · 封单 ${fmtBig(p.seal_amount)}`;
      case "dragon_tiger":
        return `龙虎榜净买入 ${fmtBig(p.net_buy)} · ${p.reason || ""}`;
      case "stock_flow":
        return `主力净额 ${fmtBig(p.main_net)} (${p.main_ratio ?? "—"}%)`;
      case "margin":
        return `融资余额 ${fmtBig(p.balance)} · 净买入 ${fmtBig(p.net_buy)}`;
      case "research":
        return `${p.org || ""}：${p.title || ""}`;
      case "announcement":
        return p.title || "";
      case "popularity":
        return `人气 #${p.rank ?? "—"}（前值 ${p.prev_rank ?? "—"}）`;
      case "sector_flow":
        return `板块资金 ${fmtBig(p.main_net)}`;
      case "connect":
        return `成交 ${fmtBig(p.turnover)} · 净买入 ${fmtBig(p.net_buy)}`;
      default:
        return r.name || "";
    }
  }

  async function loadStockSignals(secid) {
    const host = $("stockSignals");
    host.replaceChildren(el("li", "empty", "加载中…"));
    $("stockSignalSub").textContent = "—";
    try {
      const res = await api(`/api/signals/stock/${encodeURIComponent(secid)}`);
      const rows = res.signals || [];
      host.replaceChildren();
      $("stockSignalSub").textContent = `${rows.length} 条`;
      if (!rows.length) {
        host.appendChild(el("li", "empty", "该品种近 7 日无信号"));
        return;
      }
      rows.slice(0, 20).forEach((r) => {
        const li = el("li");
        const badge = el("span", "sl-src", (signalMeta(r.source).name || r.source));
        li.append(badge, el("span", "sl-text", `${r.day.slice(5)} ${signalText(r)}`));
        li.title = `${r.day} ${r.source} ${signalText(r)}`;
        host.appendChild(li);
      });
    } catch (_) {
      host.replaceChildren(el("li", "empty", "信号加载失败"));
    }
  }

  /* ---------------- controls ---------------- */
  function bindControls() {
    $("refreshBtn").addEventListener("click", async () => {
      const btn = $("refreshBtn");
      btn.classList.add("spin");
      btn.disabled = true;
      try {
        await api("/api/refresh", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(state.market ? { market: state.market.id } : {}),
        });
        toast("已触发采集");
        setTimeout(() => { loadMarket(state.market.id); loadOverview(); }, 1200);
      } catch (err) {
        toast(`刷新失败: ${err.message}`);
      } finally {
        btn.classList.remove("spin");
        btn.disabled = false;
      }
    });

    $("searchInput").addEventListener("input", (e) => {
      clearTimeout(state.searchTimer);
      state.search = e.target.value.trim();
      state.searchTimer = setTimeout(() => loadMarket(state.market.id, true), 260);
    });

    $("pageSize").addEventListener("change", (e) => {
      state.size = Number(e.target.value);
      loadMarket(state.market.id, true);
    });

    $("prevPage").addEventListener("click", () => {
      if (state.page > 1) { state.page -= 1; loadMarket(state.market.id); }
    });
    $("nextPage").addEventListener("click", () => {
      const pages = Math.max(1, Math.ceil(state.total / state.size));
      if (state.page < pages) { state.page += 1; loadMarket(state.market.id); }
    });

    $("exportBtn").addEventListener("click", (e) => {
      if (!state.market) { e.preventDefault(); return; }
      e.currentTarget.href = `/api/export?market=${encodeURIComponent(state.market.id)}`;
    });

    document.querySelectorAll("th.sortable").forEach((th) => {
      th.tabIndex = 0;
      th.setAttribute("role", "button");
      const activate = () => {
        const field = th.dataset.sort;
        if (state.sort === field) state.order = state.order === "desc" ? "asc" : "desc";
        else { state.sort = field; state.order = "desc"; }
        document.querySelectorAll("th.sortable").forEach((n) => n.classList.remove("active", "asc"));
        th.classList.add("active");
        if (state.order === "asc") th.classList.add("asc");
        loadMarket(state.market.id, true);
      };
      th.addEventListener("click", activate);
      th.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); activate(); }
      });
    });

    $("moverTabs").addEventListener("click", (e) => {
      const btn = e.target.closest(".tab");
      if (!btn) return;
      state.moversTab = btn.dataset.tab;
      document.querySelectorAll("#moverTabs .tab").forEach((t) => t.classList.toggle("active", t === btn));
      renderMovers();
    });

    $("chartTabs").addEventListener("click", async (e) => {
      const btn = e.target.closest(".tab");
      if (!btn) return;
      state.chartMode = btn.dataset.klt === "trend" ? "trend" : btn.dataset.klt;
      document.querySelectorAll("#chartTabs .tab").forEach((t) => t.classList.toggle("active", t === btn));
      if (state.chartMode !== "trend") await loadBars(Number(state.chartMode));
      renderChart();
    });

    $("flowTabs").addEventListener("click", async (e) => {
      const btn = e.target.closest(".tab");
      if (!btn) return;
      state.flowMode = btn.dataset.flow;
      document.querySelectorAll("#flowTabs .tab").forEach((t) => t.classList.toggle("active", t === btn));
      if (state.flowMode === "daily") await loadDailyFlow();
      renderFlow();
    });

    $("watchBtn").addEventListener("click", toggleWatch);
    $("drawerClose").addEventListener("click", closeDrawer);
    $("drawerScrim").addEventListener("click", closeDrawer);
    $("readerClose").addEventListener("click", closeReader);
    $("readerScrim").addEventListener("click", closeReader);

    $("expandAll").addEventListener("click", (e) => {
      const allCollapsed = state.collapsed.size > 0;
      state.collapsed.clear();
      if (!allCollapsed) (state.overview ? state.overview.groups : []).forEach((g) => state.collapsed.add(g.id));
      e.currentTarget.textContent = allCollapsed ? "全部收起" : "全部展开";
      renderNav();
    });

    $("themeBtn").addEventListener("click", () => {
      document.body.classList.toggle("high-contrast");
      toast(document.body.classList.contains("high-contrast") ? "高对比度已开启" : "标准对比度");
    });

    document.addEventListener("keydown", (e) => {
      const typing = ["INPUT", "TEXTAREA"].includes(document.activeElement.tagName);
      if (e.key === "/" && !typing) { e.preventDefault(); $("gsearchInput").focus(); }
      else if (e.key === "Escape") {
        if (!$("gsearchResults").hidden) $("gsearchResults").hidden = true;
        else if (!$("reader").hidden) closeReader();
        else closeDrawer();
      } else if (e.key.toLowerCase() === "w" && !typing && !e.metaKey && !e.ctrlKey
                 && !$("drawer").hidden) { toggleWatch(); }
      else if (e.key.toLowerCase() === "r" && !typing && !e.metaKey && !e.ctrlKey) { $("refreshBtn").click(); }
    });

    let resizeTimer = null;
    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        if (state.stock) { renderChart(); renderFlow(); }
      }, 150);
    });

    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) { loadOverview(); if (state.market) loadMarket(state.market.id); }
    });
  }

  function tickClock() {
    $("clock").textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  }

  async function boot() {
    bindControls();
    bindSearch();
    renderSignalTabs();
    renderIntelTabs();
    tickClock();
    setInterval(tickClock, 1000);
    await loadWatchlist();
    await loadOverview();
    if (state.market) await loadMarket(state.market.id, true);
    await loadStatus();
    await loadSignalSummary();
    await loadSignals();
    await loadIntel();
    setInterval(() => { if (!document.hidden) loadSignals(); }, 60000);
    setInterval(() => { if (!document.hidden) loadSignalSummary(); }, 120000);
    setInterval(() => { if (!document.hidden) loadOverview(); }, 8000);
    // only poll the table while the selected market is actually trading
    setInterval(() => {
      if (document.hidden || !state.market || state.loading) return;
      if (state.session && !state.session.open) {
        if (!state.closedTick || Date.now() - state.closedTick > 60000) {
          state.closedTick = Date.now();
          loadMarket(state.market.id);
        }
        return;
      }
      loadMarket(state.market.id);
    }, 12000);
    setInterval(() => { if (!document.hidden) loadStatus(); }, 20000);
    setInterval(() => { if (!document.hidden) loadWatchlist(); }, 20000);
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
