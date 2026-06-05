'use strict';

const POLL_MS = 1000;
const $ = (id) => document.getElementById(id);

let selectedArm = null;   // sticky user selection
let latest = null;

// ── formatting ──────────────────────────────────────────────────────────
function money(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  return (n >= 0 ? '+' : '−') + '₹' + Math.abs(Math.round(n)).toLocaleString('en-IN');
}
function pct(x) { return (x === null || x === undefined) ? '—' : (x * 100).toFixed(0) + '%'; }
function sc(n) { return n > 0 ? 'pos' : (n < 0 ? 'neg' : ''); }
function setVal(el, n) { el.textContent = money(n); el.classList.remove('pos', 'neg'); if (n) el.classList.add(n > 0 ? 'pos' : 'neg'); }
function istTime(iso) { try { return new Date(iso).toLocaleTimeString('en-GB', { timeZone: 'Asia/Kolkata' }); } catch { return '—'; } }

// total P&L for an arm = realized + (unrealized if live online)
function armTotals(arm, online) {
  const realized = (arm.realized && arm.realized.net_pnl) || 0;
  const unreal = (online && arm.live && arm.live.totals) ? arm.live.totals.unrealized_pnl : 0;
  return { realized, unreal, total: realized + unreal };
}

// ── poll ──────────────────────────────────────────────────────────────────
async function poll() {
  try {
    const r = await fetch('/api/monitor', { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    latest = await r.json();
    render(latest);
    $('err-line').textContent = '';
  } catch (e) {
    setStatus('error', 'ERROR');
    $('err-line').textContent = 'monitor unreachable: ' + e.message;
  }
}
function setStatus(state, label) { const el = $('conn-status'); el.dataset.state = state; el.textContent = label; }

function render(data) {
  const arms = data.arms || {};
  const names = Object.keys(arms);
  const online = !!data.live_online;

  const anyHalted = names.some((n) => arms[n].live && arms[n].live.risk && arms[n].live.risk.halted);
  if (anyHalted) setStatus('halted', 'HALTED');
  else if (online) setStatus('live', 'LIVE');
  else setStatus('offline', 'OFFLINE');

  $('session-date').textContent = data.date || '—';
  $('server-time').textContent = istTime(data.server_time) + ' IST';
  // feed age = min across arms' live snapshots
  let feed = null;
  names.forEach((n) => { const a = arms[n].live; if (online && a && a.feed && a.feed.last_packet_age_sec != null) feed = feed == null ? a.feed.last_packet_age_sec : Math.min(feed, a.feed.last_packet_age_sec); });
  $('feed-age').textContent = feed != null ? feed.toFixed(1) + 's' : '—';
  $('arm-count').textContent = names.length + ' arms';

  if (selectedArm === null || !arms[selectedArm]) selectedArm = arms['control'] ? 'control' : names[0];

  renderLeaderboard(arms, online);
  if (selectedArm) renderDetail(arms[selectedArm], selectedArm, online);
}

// ── leaderboard ─────────────────────────────────────────────────────────
function renderLeaderboard(arms, online) {
  const names = Object.keys(arms).sort((a, b) => armTotals(arms[b], online).total - armTotals(arms[a], online).total);
  const body = $('arm-rows');
  if (names.length === 0) { body.innerHTML = '<div class="empty">awaiting arms…</div>'; return; }
  body.innerHTML = names.map((n) => {
    const arm = arms[n], r = arm.realized || {}, t = armTotals(arm, online);
    const risk = (arm.live && arm.live.risk) || {};
    const halted = risk.halted;
    const sel = n === selectedArm ? ' selected' : '';
    return `<div class="row arm-row clickable${sel}" data-arm="${n}">
      <div>${n}${halted ? ' <span class="chip">HALT</span>' : ''}</div>
      <div class="num ${sc(t.total)}">${money(t.total)}</div>
      <div class="num ${sc(t.realized)}">${money(t.realized)}</div>
      <div class="num ${sc(t.unreal)}">${money(t.unreal)}</div>
      <div class="num">${r.n_trades || 0}</div>
      <div class="num">${pct(r.win_rate)}</div>
      <div class="num ${sc(risk.day_net_pnl)}">${risk.day_net_pnl != null ? money(risk.day_net_pnl) : '—'}</div>
      <div class="dim">${arm.note || ''}</div>
    </div>`;
  }).join('');
  body.querySelectorAll('[data-arm]').forEach((el) => {
    el.addEventListener('click', () => { selectedArm = el.dataset.arm; if (latest) render(latest); });
  });
}

// ── selected-arm detail ───────────────────────────────────────────────────
function renderDetail(arm, name, online) {
  const r = arm.realized || {};
  const t = armTotals(arm, online);
  $('detail-title').textContent = name;
  $('detail-note').textContent = arm.note ? '— ' + arm.note + (arm.universe ? ' · ' + arm.universe.join(' ') : '') : '';

  setVal($('total-pnl'), t.total);
  setVal($('realized-pnl'), t.realized);
  setVal($('unrealized-pnl'), t.unreal);
  $('total-sub').textContent = 'realized ' + money(t.realized) + ' · unreal ' + money(t.unreal);
  $('realized-sub').textContent = (r.n_trades || 0) + ' closed · fees ' + money(-(r.fees || 0));
  const openN = (online && arm.live && arm.live.totals) ? arm.live.totals.open_positions : 0;
  $('unrealized-sub').textContent = openN + ' open position' + (openN === 1 ? '' : 's');

  $('win-rate').textContent = pct(r.win_rate);
  $('winrate-sub').textContent = 'payoff ' + (r.payoff ? r.payoff.toFixed(2) : '—') + ' · W ' + money(r.avg_win) + ' / L ' + money(r.avg_loss);

  const posts = (online && arm.live && arm.live.totals) ? arm.live.totals.n_posts : null;
  const fills = (online && arm.live && arm.live.totals) ? arm.live.totals.n_fills : (r.n_trades || 0);
  $('trades-stat').textContent = (r.n_trades || 0) + (posts != null ? ' / ' + fills : '');
  $('fill-sub').textContent = posts ? ('fill rate ' + pct(fills / posts) + ' · ' + posts + ' posts') : 'fill rate —';

  renderRisk(arm.live, online, t.realized);
  renderEquity(r.equity_curve || [], online ? t.unreal : 0);
  renderPositions(online ? arm.live : null);
  renderInstruments(r.per_instrument || {});
  renderExits(r.exit_breakdown || {});
}

function renderRisk(live, online, realizedFallback) {
  const risk = (live && live.risk) || {};
  const dayPnl = (online && risk.day_net_pnl != null) ? risk.day_net_pnl : realizedFallback;
  const limit = risk.loss_limit != null ? risk.loss_limit : -20000;
  setVal($('day-risk'), dayPnl);
  const frac = dayPnl < 0 ? Math.min(1, dayPnl / limit) : 0;
  const fill = $('risk-fill');
  fill.style.width = (frac * 100).toFixed(0) + '%';
  fill.style.background = frac > 0.66 ? 'var(--red)' : (frac > 0.33 ? 'var(--amber)' : 'var(--green)');
  $('risk-sub').textContent = 'limit ' + money(limit) + ' · ' + (frac * 100).toFixed(0) + '% used';
}

function renderPositions(live) {
  const body = $('positions-body');
  if (!live || !live.brokers) { body.innerHTML = '<div class="empty">trader offline</div>'; $('open-count').textContent = '— live'; return; }
  const open = Object.values(live.brokers).filter((b) => b.position_side !== 0);
  $('open-count').textContent = open.length + ' live';
  if (open.length === 0) { body.innerHTML = '<div class="empty">flat — no open positions</div>'; return; }
  body.innerHTML = open.map((b) => {
    const side = b.position_side > 0 ? '<span class="side-long">LONG</span>' : '<span class="side-short">SHORT</span>';
    return `<div class="row pos-row"><div>${b.underlying}</div><div>${side}</div>
      <div>${b.entry_price != null ? b.entry_price.toFixed(2) : '—'}</div>
      <div>${b.last_mid ? b.last_mid.toFixed(2) : '—'}</div><div>${b.qty}</div>
      <div class="num ${sc(b.unrealized_pnl)}">${money(b.unrealized_pnl)}</div></div>`;
  }).join('');
}

function renderInstruments(per) {
  const body = $('instrument-body');
  const syms = Object.keys(per);
  if (syms.length === 0) { body.innerHTML = '<div class="empty">no trades yet</div>'; return; }
  body.innerHTML = syms.sort((a, b) => per[b].net - per[a].net).map((s) => {
    const d = per[s];
    return `<div class="row inst-row"><div>${s}</div><div class="num ${sc(d.net)}">${money(d.net)}</div>
      <div class="num">${d.n}</div><div class="num">${pct(d.win_rate)}</div></div>`;
  }).join('');
}

function renderExits(ex) {
  const body = $('exit-body');
  const ms = Object.keys(ex);
  if (ms.length === 0) { body.innerHTML = '<div class="empty">no trades yet</div>'; return; }
  const order = ['maker_exit', 'taker_max_hold', 'taker_stop', 'taker_reversal', 'taker_eod'];
  ms.sort((a, b) => (order.indexOf(a) + 1 || 99) - (order.indexOf(b) + 1 || 99));
  body.innerHTML = ms.map((m) => {
    const d = ex[m];
    return `<div class="row exit-row"><div>${m}</div><div class="num">${d.n}</div>
      <div class="num ${sc(d.net)}">${money(d.net)}</div><div class="num">${pct(d.win_rate)}</div></div>`;
  }).join('');
}

// ── canvas equity curve (unchanged math) ──────────────────────────────────
function renderEquity(curve, unrealized) {
  const canvas = $('equity-chart');
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || 1180, cssH = 280;
  if (canvas.width !== Math.round(cssW * dpr)) { canvas.width = Math.round(cssW * dpr); canvas.height = Math.round(cssH * dpr); }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);
  const pad = { l: 8, r: 64, t: 14, b: 18 };
  const w = cssW - pad.l - pad.r, h = cssH - pad.t - pad.b;
  const pts = curve.map((p, i) => ({ x: i, y: p.cum }));
  const liveY = (pts.length ? pts[pts.length - 1].y : 0) + (unrealized || 0);
  $('equity-tag').textContent = curve.length ? `${curve.length} trades · last ${money(pts[pts.length - 1].y)}` : 'awaiting trades';
  if (pts.length === 0) {
    ctx.fillStyle = '#47525b'; ctx.font = '12px JetBrains Mono, monospace';
    ctx.fillText('no closed trades yet today', pad.l + 8, pad.t + 20); drawZero(ctx, pad, w, h, Y0(pad, h, 0, 1, 0)); return;
  }
  const ys = pts.map((p) => p.y).concat([0, liveY]);
  let lo = Math.min(...ys), hi = Math.max(...ys);
  if (lo === hi) { lo -= 1; hi += 1; }
  const padY = (hi - lo) * 0.12; lo -= padY; hi += padY;
  const X = (i) => pad.l + (pts.length === 1 ? w / 2 : (i / (pts.length - 1)) * w);
  const Y = (v) => pad.t + h - ((v - lo) / (hi - lo)) * h;
  drawZero(ctx, pad, w, h, Y(0));
  const end = pts[pts.length - 1].y, col = end >= 0 ? '#34d399' : '#f87171';
  ctx.beginPath(); ctx.moveTo(X(0), Y(pts[0].y));
  pts.forEach((p, i) => ctx.lineTo(X(i), Y(p.y)));
  ctx.lineTo(X(pts.length - 1), Y(0)); ctx.lineTo(X(0), Y(0)); ctx.closePath();
  const g = ctx.createLinearGradient(0, pad.t, 0, pad.t + h);
  g.addColorStop(0, end >= 0 ? 'rgba(52,211,153,0.16)' : 'rgba(248,113,113,0.16)'); g.addColorStop(1, 'rgba(0,0,0,0)');
  ctx.fillStyle = g; ctx.fill();
  ctx.beginPath(); ctx.lineWidth = 1.6; ctx.strokeStyle = col;
  pts.forEach((p, i) => i ? ctx.lineTo(X(i), Y(p.y)) : ctx.moveTo(X(i), Y(p.y))); ctx.stroke();
  if (unrealized) {
    ctx.beginPath(); ctx.setLineDash([4, 3]); ctx.lineWidth = 1.2; ctx.strokeStyle = liveY >= 0 ? '#34d399' : '#f87171';
    ctx.moveTo(X(pts.length - 1), Y(pts[pts.length - 1].y)); ctx.lineTo(X(pts.length - 1) + 14, Y(liveY)); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = liveY >= 0 ? '#34d399' : '#f87171'; ctx.beginPath(); ctx.arc(X(pts.length - 1) + 14, Y(liveY), 2.5, 0, 7); ctx.fill();
  }
  ctx.fillStyle = col; ctx.font = '11px JetBrains Mono, monospace'; ctx.textAlign = 'left';
  ctx.fillText(money(end), pad.l + w + 6, Y(end) + 3);
}
function Y0(pad, h, lo, hi, v) { return pad.t + h - ((v - lo) / (hi - lo)) * h; }
function drawZero(ctx, pad, w, h, yZero) {
  ctx.strokeStyle = '#222c34'; ctx.lineWidth = 1; ctx.setLineDash([2, 3]);
  ctx.beginPath(); ctx.moveTo(pad.l, yZero); ctx.lineTo(pad.l + w, yZero); ctx.stroke(); ctx.setLineDash([]);
  ctx.fillStyle = '#47525b'; ctx.font = '10px JetBrains Mono, monospace'; ctx.textAlign = 'left';
  ctx.fillText('0', pad.l + w + 6, yZero + 3);
}

poll();
setInterval(poll, POLL_MS);
