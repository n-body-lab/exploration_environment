// HiveMarket observer: live view and replay of one run (see hivemarket/ui/server.py).
(function () {
  "use strict";
  const $ = id => document.getElementById(id);
  const SVG = "http://www.w3.org/2000/svg";
  const MAX_ROWS = 600;
  const MOVE_SECONDS = 1.6, MSG_SECONDS = 2.5, BACK_RECENT = 6;

  // Show page errors instead of failing silently.
  function showError(msg) { const box = $("error"); box.textContent = msg; box.hidden = false; }
  window.addEventListener("error", e => showError(`${e.message} (${(e.filename || "").split("/").pop()}:${e.lineno})`));
  window.addEventListener("unhandledrejection", e => showError(String(e.reason && e.reason.stack || e.reason)));

  const S = {
    runId: null, meta: null, es: null,
    events: [], ids: new Set(), market: [], alerts: [], sources: new Set(), hiddenSources: new Set(),
    live: true, viewT: Infinity, playing: false, speed: 5, expanded: new Set(), dirty: true, userScrolledLog: false,
  };

  // ------------------------------------------------------------------ helpers
  const fmt = ts => {
    if (!isFinite(ts)) return "--:--:--";
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString([], { hour12: false }) + "." + String(d.getMilliseconds()).padStart(3, "0");
  };
  function el(tag, attrs = {}, ...kids) {
    const n = tag.startsWith("svg:") ? document.createElementNS(SVG, tag.slice(4)) : document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === "text") n.textContent = v; else n.setAttribute(k, v === true ? "" : v);
    }
    for (const k of kids) if (k != null) n.append(k);
    return n;
  }
  // The slider starts when the market does: Docker builds and start-up can take most of a short run.
  const tMin = () => (S.market.length ? S.market[0].ts : S.events.length ? S.events[0].ts : 0);
  const tMax = () => (S.events.length ? S.events[S.events.length - 1].ts : 0);
  const viewTime = () => (S.live ? Infinity : S.viewT);
  const nowTime = () => (S.live ? Math.max(Date.now() / 1000, tMax()) : S.viewT); // for animations

  function insertSorted(arr, e) {
    if (!arr.length || arr[arr.length - 1].ts <= e.ts) { arr.push(e); return; }
    let lo = 0, hi = arr.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (arr[mid].ts <= e.ts) lo = mid + 1; else hi = mid; }
    arr.splice(lo, 0, e);
  }

  // --------------------------------------------------------------- runs/stream
  // Refresh the run list; open `prefer` (or, if nothing is open yet, the newest live run, else the newest).
  async function loadRuns(prefer) {
    const runs = await (await fetch("/api/runs")).json();
    const sel = $("run");
    sel.replaceChildren(...runs.map(r => el("option", { value: r.run_id, text:
      `${r.run_id}  ·  ${r.status}${r.mode ? " · " + r.mode : ""}` })));
    if (!runs.length) { $("status").textContent = "no runs found"; return; }
    const known = id => id && runs.some(r => r.run_id === id);
    const pick = known(prefer) ? prefer : known(S.runId) ? S.runId
               : (runs.find(r => r.status === "live") || runs[0]).run_id;
    sel.value = pick;
    const mine = runs.find(r => r.run_id === pick);
    if (S.meta && mine && pick === S.runId && mine.status !== S.meta.status) { // e.g. live -> finished
      S.meta.status = mine.status;
      $("status").textContent = mine.status; $("status").className = "pill " + mine.status;
      if (mine.status !== "live") loadReconstruction();
    }
    if (pick !== S.runId) await openRun(pick);
  }

  async function openRun(id) {
    if (S.es) S.es.close();
    Object.assign(S, { runId: id, events: [], ids: new Set(), market: [], alerts: [], sources: new Set(),
                       live: true, viewT: Infinity, playing: false, expanded: new Set(), dirty: true,
                       userScrolledLog: false });
    history.replaceState(null, "", "#" + encodeURIComponent(id));
    S.meta = await (await fetch(`/api/runs/${encodeURIComponent(id)}`)).json();
    const status = $("status");
    status.textContent = S.meta.status; status.className = "pill " + S.meta.status;
    $("agent-filter").replaceChildren(el("option", { value: "", text: "all agents" }),
      ...S.meta.agents.map(a => el("option", { value: a.id, text: a.id })));
    loadReconstruction();
    S.es = new EventSource(`/api/runs/${encodeURIComponent(id)}/stream`);
    S.es.addEventListener("batch", m => ingest(JSON.parse(m.data)));
    S.es.onerror = () => { status.textContent = "reconnecting…"; };
    S.es.onopen = () => { status.textContent = S.meta.status; };
    setLive(true);
  }

  async function loadReconstruction() {
    const r = await (await fetch(`/api/runs/${encodeURIComponent(S.runId)}/backchannel`)).json();
    const box = $("recon");
    if (!r.available) { box.textContent = "Post-run reconstruction: not available yet (runs after the run ends)."; return; }
    const delivered = r.records.filter(x => x.status === "delivered").length;
    box.textContent = `Post-run reconstruction (analysis/backchannel.jsonl): ${r.records.length} backchannel ` +
                      `messages, ${delivered} delivered.`;
  }

  function ingest(batch) {
    let newSources = false;
    for (const e of batch) {
      if (!e.event_id || S.ids.has(e.event_id)) continue; // a reconnect resends the backlog
      S.ids.add(e.event_id);
      insertSorted(S.events, e);
      if (e.source === "market") insertSorted(S.market, e);
      if (!S.sources.has(e.source)) { S.sources.add(e.source); newSources = true; }
    }
    S.alerts = HM.detectBackchannel(S.events, S.meta.agents);
    if (newSources) renderSources();
    S.dirty = true;
  }

  // ------------------------------------------------------------------- timeline
  function setLive(on) {
    S.live = on; S.playing = on ? false : S.playing;
    $("live").classList.toggle("on", on);
    $("play").textContent = S.playing ? "❚❚" : "▶";
    S.dirty = true;
  }
  function syncScrub() {
    const lo = tMin(), hi = tMax();
    const t = S.live ? hi : S.viewT;
    $("scrub").value = hi > lo ? Math.round(((t - lo) / (hi - lo)) * 1000) : 1000;
    $("clock").textContent = fmt(S.live ? hi : S.viewT);
  }
  $("scrub").addEventListener("input", e => {
    const lo = tMin(), hi = tMax();
    S.viewT = lo + (hi - lo) * (e.target.value / 1000);
    setLive(false);
  });
  $("live").addEventListener("click", () => {
    if (S.live) { S.viewT = tMax(); setLive(false); } else setLive(true);
  });
  const GAP_SECONDS = 2; // with "skip quiet gaps", a replay jumps over any stretch longer than this with no events
  function savePref(k, v) { try { localStorage.setItem(k, v); } catch (_) { /* storage unavailable */ } }
  function loadPref(k) { try { return localStorage.getItem(k); } catch (_) { return null; } }
  const savedSpeed = loadPref("hm-speed"), savedSkip = loadPref("hm-skip-gaps");
  if (savedSpeed && [...$("speed").options].some(o => o.value === savedSpeed)) $("speed").value = savedSpeed;
  if (savedSkip !== null) $("skip-gaps").checked = savedSkip === "1";
  S.speed = +$("speed").value;
  $("speed").addEventListener("change", e => { S.speed = +e.target.value; savePref("hm-speed", e.target.value); });
  $("skip-gaps").addEventListener("change", e => savePref("hm-skip-gaps", e.target.checked ? "1" : "0"));
  // The first event after time t (binary search), for skipping quiet gaps.
  function nextEventTs(t) {
    let lo = 0, hi = S.events.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (S.events[mid].ts <= t) lo = mid + 1; else hi = mid; }
    return lo < S.events.length ? S.events[lo].ts : Infinity;
  }
  function togglePlay() {
    if (S.live) { S.viewT = tMin(); setLive(false); }
    else if (!S.playing && S.viewT >= tMax()) S.viewT = tMin(); // at the end: start over
    S.playing = !S.playing;
    $("play").textContent = S.playing ? "❚❚" : "▶";
  }
  document.addEventListener("keydown", e => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName);
    if (e.code === "Space" && !typing && !$("timeline").hidden) { e.preventDefault(); togglePlay(); }
  });
  $("play").addEventListener("click", togglePlay);

  // ---------------------------------------------------------------------- graph
  function layout(ids) {
    const n = ids.length, pos = {};
    ids.forEach((id, i) => {
      const a = -Math.PI / 2 + (2 * Math.PI * i) / Math.max(n, 1);
      pos[id] = n === 1 ? { x: 500, y: 320 } : { x: 500 + 360 * Math.cos(a), y: 330 + 225 * Math.sin(a) };
    });
    return pos;
  }
  const NODE_W = 178, NODE_H = 104;
  function clip(c, toward) { // point where the line c -> toward leaves c's node box
    const dx = toward.x - c.x, dy = toward.y - c.y;
    const sx = (NODE_W / 2 + 6) / Math.abs(dx || 1e-9), sy = (NODE_H / 2 + 6) / Math.abs(dy || 1e-9);
    const s = Math.min(sx, sy, 1);
    return { x: c.x + dx * s, y: c.y + dy * s };
  }
  function curve(p, q, bend) { // quadratic from box edge to box edge, bent to one side
    const mx = (p.x + q.x) / 2, my = (p.y + q.y) / 2, dx = q.x - p.x, dy = q.y - p.y;
    const len = Math.hypot(dx, dy) || 1;
    const c = { x: mx - (dy / len) * bend * len, y: my + (dx / len) * bend * len };
    const a = clip(p, c), b = clip(q, c);
    return { a, b, c, d: `M${a.x},${a.y} Q${c.x},${c.y} ${b.x},${b.y}`,
             at: t => ({ x: (1 - t) ** 2 * a.x + 2 * (1 - t) * t * c.x + t * t * b.x,
                         y: (1 - t) ** 2 * a.y + 2 * (1 - t) * t * c.y + t * t * b.y }) };
  }
  function markers() {
    const defs = el("svg:defs");
    for (const [name, color] of [["edge", "--edge"], ["order", "--order"], ["move", "--move"], ["msg", "--msg"], ["back", "--back"]]) {
      const m = el("svg:marker", { id: "arrow-" + name, viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7,
                                   markerHeight: 7, orient: "auto-start-reverse" });
      m.append(el("svg:path", { d: "M0,0 L10,5 L0,10 z", style: `fill: var(${color})` }));
      defs.append(m);
    }
    return defs;
  }
  function chip(x, y, label, cls, textCls) {
    const w = Math.max(26, label.length * 7 + 8);
    return [el("svg:rect", { x, y, width: w, height: 16, rx: 3, class: cls }),
            el("svg:text", { x: x + w / 2, y: y + 12, "text-anchor": "middle", class: textCls, text: label }), w];
  }

  function renderGraph() {
    const svg = $("graph");
    const world = S.meta.world || {};
    const ids = S.meta.agents.map(a => a.id).filter(id => !world.nodes || id in world.nodes);
    for (const id of Object.keys(world.nodes || {})) if (!ids.includes(id)) ids.push(id);
    const pos = layout(ids);
    const t = viewTime(), now = nowTime();
    const st = HM.stateAt(world, S.market, t);
    const g = el("svg:g");
    svg.replaceChildren(markers(), g);

    // trade edges (cost on the edge; to pays from)
    for (const e of world.edges || []) {
      if (!pos[e.from] || !pos[e.to]) continue;
      const c = curve(pos[e.from], pos[e.to], 0.12);
      g.append(el("svg:path", { d: c.d, class: "g-edge", "marker-end": "url(#arrow-edge)" }));
      const m = c.at(0.5);
      g.append(el("svg:text", { x: m.x, y: m.y - 4, "text-anchor": "middle", class: "g-edge-label", text: e.cost }));
    }
    // open orders: seller -> buyer, along the trade edge
    for (const o of Object.values(st.orders)) {
      if (!pos[o.seller] || !pos[o.buyer]) continue;
      const c = curve(pos[o.seller], pos[o.buyer], 0.12);
      g.append(el("svg:path", { d: c.d, class: "g-order", "marker-end": "url(#arrow-order)" }));
      const m = c.at(0.35);
      g.append(el("svg:text", { x: m.x, y: m.y + 14, "text-anchor": "middle", class: "g-edge-label",
                                text: `${o.token} ${o.status}` }));
    }
    // backchannel: aggregated per pair, on the other side of the edge
    const pairs = {};
    for (const a of S.alerts) {
      if (a.ts > t || a.level !== "contact" || !pos[a.from] || !pos[a.to]) continue;
      const k = a.from + ">" + a.to;
      const p = pairs[k] || (pairs[k] = { from: a.from, to: a.to, n: 0, last: 0, ports: new Set() });
      p.n += 1; p.last = Math.max(p.last, a.ts); if (a.port) p.ports.add(a.port);
    }
    const alarmed = new Set();
    for (const p of Object.values(pairs)) {
      const recent = now - p.last < BACK_RECENT;
      if (recent) { alarmed.add(p.from); alarmed.add(p.to); }
      const c = curve(pos[p.from], pos[p.to], -0.28);
      g.append(el("svg:path", { d: c.d, class: "g-back" + (recent ? " recent" : ""), "marker-end": "url(#arrow-back)" }));
      const m = c.at(0.5);
      g.append(el("svg:text", { x: m.x, y: m.y, "text-anchor": "middle", class: "g-back-label",
                                text: `×${p.n}${p.ports.size ? " :" + [...p.ports].join(",") : ""}` }));
    }
    // recent messages and token moves (animated while live or replaying)
    for (const e of S.market) {
      if (e.ts > t) break;
      const age = now - e.ts, d = e.data || {};
      if (e.kind === "message" && age < MSG_SECONDS) {
        for (const r of d.recipients || [d.to]) {
          if (!pos[d.sender] || !pos[r]) continue;
          g.append(el("svg:path", { d: curve(pos[d.sender], pos[r], 0.22).d, class: "g-msg", "marker-end": "url(#arrow-msg)",
                                    opacity: 1 - age / MSG_SECONDS }));
        }
      }
      if (e.kind === "exchange" && age < MOVE_SECONDS + 0.6 && pos[d.seller] && pos[d.buyer]) {
        const c = curve(pos[d.seller], pos[d.buyer], 0.12);
        g.append(el("svg:path", { d: c.d, class: "g-move", "marker-end": "url(#arrow-move)" }));
        const p = c.at(Math.min(1, Math.max(0, age / MOVE_SECONDS)));
        g.append(el("svg:circle", { cx: p.x, cy: p.y, r: 13, class: "g-flying" }));
        g.append(el("svg:text", { x: p.x, y: p.y + 4, "text-anchor": "middle", class: "g-chip-text", text: d.token }));
      }
    }
    // nodes
    const agentsById = Object.fromEntries(S.meta.agents.map(a => [a.id, a]));
    for (const id of ids) {
      const { x, y } = pos[id], spec = (world.nodes || {})[id] || {}, a = agentsById[id] || {};
      const done = st.completed.includes(id);
      const box = el("svg:g", { transform: `translate(${x - NODE_W / 2},${y - NODE_H / 2})` });
      box.append(el("svg:rect", { width: NODE_W, height: NODE_H, rx: 8,
                                  class: "g-node" + (done ? " done" : "") + (alarmed.has(id) ? " alarm" : "") }));
      box.append(el("svg:text", { x: 10, y: 19, class: "g-name", text: id + (done ? " ✓" : "") }));
      box.append(el("svg:text", { x: NODE_W - 10, y: 19, "text-anchor": "end", class: "g-sub",
                                  text: (a.model || "").slice(0, 18) }));
      const credits = st.credits[id], avail = st.available ? st.available[id] : null;
      const escrow = Object.values((st.escrow || {})[id] || {}).reduce((s, v) => s + v, 0);
      box.append(el("svg:text", { x: 10, y: 38, class: "g-credits",
                                  text: `${credits ?? "?"} cr` + (escrow ? `  (${escrow} in escrow)` : "") +
                                        (avail != null && !escrow && avail !== credits ? `  (${avail} free)` : "") }));
      let cx = 10;
      box.append(el("svg:text", { x: 10, y: 58, class: "g-sub", text: "holds" }));
      cx = 48;
      for (const tok of st.holds[id] || []) {
        const [r, tx, w] = chip(cx, 47, tok, "g-chip-held", "g-chip-text");
        box.append(r, tx); cx += w + 4; if (cx > NODE_W - 30) break;
      }
      if (spec.needs && spec.needs.length) {
        box.append(el("svg:text", { x: 10, y: 82, class: "g-sub", text: "needs" }));
        cx = 48;
        for (const tok of spec.needs) {
          const known = (st.known[id] || []).includes(tok);
          const [r, tx, w] = chip(cx, 71, tok, "g-chip-need" + (known ? " known" : ""), "g-chip-text need" + (known ? " known" : ""));
          box.append(r, tx); cx += w + 4; if (cx > NODE_W - 30) break;
        }
      }
      g.append(box);
    }
    // round header
    const rounds = (world.rounds || {}).count;
    const ideal = (world.ideal || {}).ideal_credits;
    let head = st.round ? `Round ${st.round}${rounds ? " of " + rounds : ""} · ${st.phase}` : "waiting for the first round";
    if (st.outcome && st.phase !== "active") head += ` · ${String(st.outcome).toUpperCase()}`;
    g.append(el("svg:text", { x: 16, y: 24, class: "g-round", text: head }));
    g.append(el("svg:text", { x: 16, y: 42, class: "g-round",
                              text: `spent this round: ${st.spent}` + (ideal != null ? ` · ideal ${ideal}` : "") }));
    $("round").textContent = head;
    $("graph-meta").textContent = `${Object.keys(st.orders).length} open orders`;
  }

  // ------------------------------------------------------------------ alerts
  function roundAt(ts) {
    let r = 0;
    for (const e of S.market) { if (e.ts > ts) break; if (e.kind === "round_start") r = e.data.round; }
    return r;
  }
  function renderAlerts() {
    const t = viewTime(), showProbes = $("show-probes").checked;
    const list = S.alerts.filter(a => showProbes || a.level !== "probe");
    const first = S.alerts.find(a => a.level === "contact" || a.level === "evidence");
    const box = $("back-first"), banner = $("banner");
    if (first && first.ts <= t) {
      const txt = `Backchannel since ${fmt(first.ts)} (round ${roundAt(first.ts)}): ${first.from || "?"} → ${first.to || "?"}`;
      box.textContent = txt; box.hidden = false; banner.textContent = "BACKCHANNEL"; banner.hidden = false;
    } else { box.hidden = true; banner.hidden = true; }
    const ol = $("alerts");
    const anchor = scrollAnchor(ol); { ol.replaceChildren(el("li", { class: "empty", text: "No off-channel activity seen." })); return; }
    ol.replaceChildren(...list.slice(-300).map(a => {
      const li = el("li", { "data-id": a.event_id, class: a.ts > t ? "future" : null, title: "Jump to this moment" },
        el("div", {}, el("span", { class: "lvl " + a.level, text: a.level }),
           el("span", { class: "mono", text: fmt(a.ts) + "  " }),
           el("span", { class: "who", text: `${a.from || "?"} → ${a.to || "?"}${a.port ? " :" + a.port : ""}` }),
           a.established === false ? el("span", { class: "obs", text: "  refused" }) : null),
        a.detail ? el("div", { class: "detail", text: a.detail }) : null,
        a.observers && a.observers.length ? el("div", { class: "obs", text: "seen by " + a.observers.join(", ") }) : null);
      li.addEventListener("click", () => { S.viewT = a.ts + 0.001; S.userScrolledLog = false; setLive(false); });
      return li;
    }));
    restoreScroll(ol, anchor);
  }

  // --------------------------------------------------------------------- log
  function renderSources() {
    $("sources").replaceChildren(...[...S.sources].sort().map(src => {
      const cb = el("input", { type: "checkbox", checked: !S.hiddenSources.has(src) });
      cb.addEventListener("change", () => { cb.checked ? S.hiddenSources.delete(src) : S.hiddenSources.add(src); S.dirty = true; });
      return el("label", {}, cb, el("span", { class: "src " + src, text: src }));
    }));
  }
  // Where the reader is in a scrolling box: at the bottom, or which row is at the top and how far down.
  function scrollAnchor(box) {
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 24;
    let id = null, delta = 0;
    for (const row of box.querySelectorAll("[data-id]")) {
      if (row.offsetTop + row.offsetHeight > box.scrollTop) { id = row.dataset.id; delta = row.offsetTop - box.scrollTop; break; }
    }
    return { atBottom, id, delta, top: box.scrollTop };
  }
  // Put the box back where the reader was after its contents were rebuilt. Only this box scrolls:
  // never scrollIntoView, which would also move the page.
  function restoreScroll(box, anchor) {
    const row = anchor.id && box.querySelector(`[data-id="${CSS.escape(anchor.id)}"]`);
    box.scrollTop = row ? row.offsetTop - anchor.delta : anchor.top;
  }

  function renderLog() {
    const agent = $("agent-filter").value, q = $("search").value.trim().toLowerCase();
    const noise = $("hide-noise").checked, t = viewTime();
    const alertIds = new Set(S.alerts.map(a => a.event_id));
    const wrap = $("log-wrap");
    const anchor = scrollAnchor(wrap);
    const rows = [];
    let matched = 0;
    for (let i = S.events.length - 1; i >= 0 && rows.length < MAX_ROWS; i--) {
      const e = S.events[i];
      if (S.hiddenSources.has(e.source) || (agent && e.agent_id !== agent) || (noise && HM.isNoise(e))) continue;
      const sum = HM.summarize(e);
      if (q && !(e.kind.toLowerCase().includes(q) || sum.toLowerCase().includes(q))) continue;
      matched++;
      rows.push([e, sum]);
    }
    rows.reverse();
    const body = el("tbody");
    for (const [e, sum] of rows) {
      const tr = el("tr", { "data-id": e.event_id,
                            class: [e.ts > t ? "future" : "", alertIds.has(e.event_id) ? "alert" : ""].join(" ").trim() || null },
        el("td", { class: "t", text: fmt(e.ts) }), el("td", {}, el("span", { class: "src " + e.source, text: e.source })),
        el("td", { text: e.agent_id || "" }), el("td", { text: e.kind }), el("td", { class: "sum", text: sum }));
      tr.addEventListener("click", () => { S.expanded.has(e.event_id) ? S.expanded.delete(e.event_id) : S.expanded.add(e.event_id); S.dirty = true; });
      body.append(tr);
      if (S.expanded.has(e.event_id)) {
        body.append(el("tr", {}, el("td", { colspan: 5 }, el("pre", { text: JSON.stringify(e, null, 1) }))));
      }
    }
    $("log").replaceChildren(body);
    $("log-count").textContent = `${matched}${matched >= MAX_ROWS ? "+" : ""} shown of ${S.events.length}`;
    const follow = $("follow").checked;
    if (follow && S.live && anchor.atBottom) {
      wrap.scrollTop = wrap.scrollHeight; // following, and the reader is at the bottom: stay there
    } else if (follow && !S.live && !S.userScrolledLog) {
      // replay: keep the current moment (the last row that has happened) near the middle of the box
      const now = [...body.querySelectorAll("tr[data-id]:not(.future)")].pop();
      if (now) wrap.scrollTop = Math.max(0, now.offsetTop - wrap.clientHeight / 2);
    } else {
      restoreScroll(wrap, anchor); // the reader scrolled away: keep what they're reading in place
    }
  }

  // ------------------------------------------------------------------- loop
  // The lists, counters and slider redraw on a timer (4x a second, only when something changed);
  // animation frames only drive the graph while something moves. Browsers pause animation
  // frames in background tabs, so nothing depends on them but the animation.
  function redraw() {
    if (!S.meta || !S.dirty) return;
    S.dirty = false;
    try {
      renderGraph(); renderAlerts(); renderLog(); syncScrub();
    } catch (err) { showError(err.stack || String(err)); }
    $("counts").textContent = `${S.events.length} events · ${S.alerts.filter(a => a.level !== "probe").length} backchannel alerts`;
  }
  let lastFrame = performance.now();
  function frame(ts) {
    const dt = Math.min(0.5, (ts - lastFrame) / 1000); lastFrame = ts;
    if (S.meta) {
      if (S.playing) {
        S.viewT += dt * S.speed;
        if ($("skip-gaps").checked) {
          const next = nextEventTs(S.viewT);
          if (next - S.viewT > GAP_SECONDS && next !== Infinity) S.viewT = next - 0.2; // land just before it
        }
        if (S.viewT >= tMax()) { S.viewT = tMax(); S.playing = false; $("play").textContent = "▶"; }
        S.dirty = true;
      }
      const animating = S.live && Date.now() / 1000 - tMax() < MSG_SECONDS + 1;
      if (animating || S.playing) renderGraph();
    }
    requestAnimationFrame(frame);
  }
  setInterval(redraw, 250);

  $("log-wrap").addEventListener("wheel", () => { S.userScrolledLog = true; }, { passive: true });
  $("log-wrap").addEventListener("touchmove", () => { S.userScrolledLog = true; }, { passive: true });
  $("scrub").addEventListener("input", () => { S.userScrolledLog = false; });
  $("follow").addEventListener("change", () => { S.userScrolledLog = false; if ($("follow").checked) $("log-wrap").scrollTop = $("log-wrap").scrollHeight; });

  for (const id of ["agent-filter", "hide-noise", "follow", "show-probes"]) $(id).addEventListener("change", () => { S.dirty = true; });
  $("search").addEventListener("input", () => { S.dirty = true; });
  $("run").addEventListener("change", e => openRun(e.target.value));
  loadRuns(decodeURIComponent(location.hash.slice(1)));
  setInterval(() => loadRuns(), 10000); // new runs appear in the list; status changes show up
  requestAnimationFrame(frame);
})();
