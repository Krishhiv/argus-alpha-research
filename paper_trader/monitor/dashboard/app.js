'use strict';

const POLL_MS = 1000;
const $ = (id) => document.getElementById(id);

// ── formatting ──────────────────────────────────────────────────────────
function money(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  const sign = n >= 0 ? '+' : '−';
  return sign + '₹' + Math.abs(Math.round(n)).toLocaleString('en-IN');
}
function pct(x) { return (x === null || x === undefined) ? '—' : (x * 100).toFixed(0) + '%'; }
function signClass(n) { return n > 0 ? 'pos' : (n < 0 ? 'neg' : ''); }
function setVal(el, n) { el.textContent = money(n); el.classList.remove('pos', 'neg'); if (n) el.classList.add(n > 0 ? 'pos' : 'neg'); }
function istTime(iso) {
  try { return new Date(iso).toLocaleTimeString('en-GB', { timeZone: 'Asia/Kolkata' }); }
  catch { return '—'; }
}

// ── poll loop ───────────────────────────────────────────────────────────
async function poll() {
  try {
    const r = await fetch('/api/monitor', { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();
    render(data);
    $('err-line').textContent = '';
  } catch (e) {
    setStatus('error', 'ERROR');
    $('err-line').textContent = 'monitor unreachable: ' + e.message;
  }
}

function setStatus(state, label) {
  const el = $('conn-status');
  el.dataset.state = state;
  el.textContent = label;
}

function render(data) {
  const realized = data.realized || {};
  const live = data.live;
  const online = !!data.live_online;

  // status pill
  if (live && live.risk && live.risk.halted) setStatus('halted', 'HALTED');
  else if (online) setStatus('live', 'LIVE');
  else setStatus('offline', 'OFFLINE');

  $('session-date').textContent = data.date || '—';
  $('server-time').textContent = istTime(data.server_time) + ' IST';
  const feedAge = (online && live && live.feed && live.feed.last_packet_age_sec != null)
    ? live.feed.last_packet_age_sec.toFixed(1) + 's' : '—';
  $('feed-age').textContent = feedAge;

  // PnL cards
  const realizedPnl = realized.net_pnl || 0;
  const unrealized = (online && live && live.totals) ? live.totals.unrealized_pnl : 0;
  const total = realizedPnl + unrealized;
  setVal($('total-pnl'), total);
  setVal($('realized-pnl'), realizedPnl);
  setVal($('unrealized-pnl'), unrealized);
  $('total-sub').textContent = 'realized ' + money(realizedPnl) + ' · unreal ' + money(unrealized);
  $('realized-sub').textContent = (realized.n_trades || 0) + ' closed · fees ' + money(-(realized.fees || 0));
  const openN = (online && live && live.totals) ? live.totals.open_positions : 0;
  $('unrealized-sub').textContent = openN + ' open position' + (openN === 1 ? '' : 's');

  // win rate / payoff
  $('win-rate').textContent = pct(realized.win_rate);
  $('winrate-sub').textContent = 'payoff ' + (realized.payoff ? realized.payoff.toFixed(2) : '—')
    + ' · W ' + money(realized.avg_win) + ' / L ' + money(realized.avg_loss);

  // trades / fills
  const posts = (online && live && live.totals) ? live.totals.n_posts : null;
  const fills = (online && live && live.totals) ? live.totals.n_fills : (realized.n_trades || 0);
  $('trades-stat').textContent = (realized.n_trades || 0) + (posts != null ? ' / ' + fills : '');
  $('fill-sub').textContent = posts ? ('fill rate ' + pct(fills / posts) + ' · ' + posts + ' posts') : 'fill rate —';

  // day-risk gauge
  renderRisk(live, online, realizedPnl);

  // chart + tables
  renderEquity(realized.equity_curve || [], online ? unrealized : 0);
  renderPositions(online ? live : null);
  renderInstruments(realized.per_instrument || {});
  renderExits(realized.exit_breakdown || {});
}

function renderRisk(live, online, realizedFallback) {
  const dayPnl = (online && live && live.risk && live.risk.day_net_pnl != null)
    ? live.risk.day_net_pnl : realizedFallback;
  const limit = (live && live.risk && live.risk.loss_limit != null) ? live.risk.loss_limit : -20000;
  setVal($('day-risk'), dayPnl);
  const frac = dayPnl < 0 ? Math.min(1, dayPnl / limit) : 0;   // limit is negative
  const fill = $('risk-fill');
  fill.style.width = (frac * 100).toFixed(0) + '%';
  fill.style.background = frac > 0.66 ? 'var(--red)' : (frac > 0.33 ? 'var(--amber)' : 'var(--green)');
  $('risk-sub').textContent = 'limit ' + money(limit) + ' · ' + (frac * 100).toFixed(0) + '% used';
}

// ── tables ──────────────────────────────────────────────────────────────
function renderPositions(live) {
  const body = $('positions-body');
  if (!live || !live.brokers) { body.innerHTML = '<div class="empty">trader offline</div>'; $('open-count').textContent = '— live'; return; }
  const open = Object.values(live.brokers).filter((b) => b.position_side !== 0);
  $('open-count').textContent = open.length + ' live';
  if (open.length === 0) { body.innerHTML = '<div class="empty">flat — no open positions</div>'; return; }
  body.innerHTML = open.map((b) => {
    const side = b.position_side > 0 ? '<span class="side-long">LONG</span>' : '<span class="side-short">SHORT</span>';
    return `<div class="row pos-row">
      <div>${b.underlying}</div><div>${side}</div>
      <div>${b.entry_price != null ? b.entry_price.toFixed(2) : '—'}</div>
      <div>${b.last_mid ? b.last_mid.toFixed(2) : '—'}</div>
      <div>${b.qty}</div>
      <div class="num ${signClass(b.unrealized_pnl)}">${money(b.unrealized_pnl)}</div>
    </div>`;
  }).join('');
}

function renderInstruments(per) {
  const body = $('instrument-body');
  const syms = Object.keys(per);
  if (syms.length === 0) { body.innerHTML = '<div class="empty">no trades yet</div>'; return; }
  body.innerHTML = syms.sort((a, b) => per[b].net - per[a].net).map((s) => {
    const d = per[s];
    return `<div class="row inst-row">
      <div>${s}</div>
      <div class="num ${signClass(d.net)}">${money(d.net)}</div>
      <div class="num">${d.n}</div>
      <div class="num">${pct(d.win_rate)}</div>
    </div>`;
  }).join('');
}

function renderExits(ex) {
  const body = $('exit-body');
  const methods = Object.keys(ex);
  if (methods.length === 0) { body.innerHTML = '<div class="empty">no trades yet</div>'; return; }
  const order = ['maker_exit', 'taker_max_hold', 'taker_stop', 'taker_eod'];
  methods.sort((a, b) => (order.indexOf(a) + 1 || 99) - (order.indexOf(b) + 1 || 99));
  body.innerHTML = methods.map((m) => {
    const d = ex[m];
    return `<div class="row exit-row">
      <div>${m}</div>
      <div class="num">${d.n}</div>
      <div class="num ${signClass(d.net)}">${money(d.net)}</div>
      <div class="num">${pct(d.win_rate)}</div>
    </div>`;
  }).join('');
}

// ── canvas equity curve ───────────────────────────────────────────────────
function renderEquity(curve, unrealized) {
  const canvas = $('equity-chart');
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || 1180;
  const cssH = 300;
  if (canvas.width !== Math.round(cssW * dpr)) { canvas.width = Math.round(cssW * dpr); canvas.height = Math.round(cssH * dpr); }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);

  const pad = { l: 8, r: 64, t: 14, b: 18 };
  const w = cssW - pad.l - pad.r;
  const h = cssH - pad.t - pad.b;

  const pts = curve.map((p, i) => ({ x: i, y: p.cum }));
  // include a live point (last realized + unrealized) for the dashed live segment
  const liveY = (pts.length ? pts[pts.length - 1].y : 0) + (unrealized || 0);

  $('equity-tag').textContent = curve.length
    ? `${curve.length} trades · last ${money(pts[pts.length - 1].y)}` : 'awaiting trades';

  if (pts.length === 0) {
    ctx.fillStyle = '#47525b'; ctx.font = '12px JetBrains Mono, monospace';
    ctx.fillText('no closed trades yet today', pad.l + 8, pad.t + 20);
    drawZero(ctx, pad, w, h, 0, 1, -1);
    return;
  }

  const ys = pts.map((p) => p.y).concat([0, liveY]);
  let lo = Math.min(...ys), hi = Math.max(...ys);
  if (lo === hi) { lo -= 1; hi += 1; }
  const padY = (hi - lo) * 0.12; lo -= padY; hi += padY;
  const X = (i) => pad.l + (pts.length === 1 ? w / 2 : (i / (pts.length - 1)) * w);
  const Y = (v) => pad.t + h - ((v - lo) / (hi - lo)) * h;

  drawZero(ctx, pad, w, h, lo, hi, Y(0));

  // area + line
  const end = pts[pts.length - 1].y;
  const col = end >= 0 ? '#34d399' : '#f87171';
  ctx.beginPath(); ctx.moveTo(X(0), Y(pts[0].y));
  pts.forEach((p, i) => ctx.lineTo(X(i), Y(p.y)));
  ctx.lineTo(X(pts.length - 1), Y(0)); ctx.lineTo(X(0), Y(0)); ctx.closePath();
  const grad = ctx.createLinearGradient(0, pad.t, 0, pad.t + h);
  grad.addColorStop(0, (end >= 0 ? 'rgba(52,211,153,0.16)' : 'rgba(248,113,113,0.16)'));
  grad.addColorStop(1, 'rgba(0,0,0,0)');
  ctx.fillStyle = grad; ctx.fill();

  ctx.beginPath(); ctx.lineWidth = 1.6; ctx.strokeStyle = col;
  pts.forEach((p, i) => i ? ctx.lineTo(X(i), Y(p.y)) : ctx.moveTo(X(i), Y(p.y)));
  ctx.stroke();

  // dashed live segment to total (realized+unrealized)
  if (unrealized) {
    ctx.beginPath(); ctx.setLineDash([4, 3]); ctx.lineWidth = 1.2;
    ctx.strokeStyle = liveY >= 0 ? '#34d399' : '#f87171';
    ctx.moveTo(X(pts.length - 1), Y(pts[pts.length - 1].y));
    ctx.lineTo(X(pts.length - 1) + 14, Y(liveY)); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = liveY >= 0 ? '#34d399' : '#f87171';
    ctx.beginPath(); ctx.arc(X(pts.length - 1) + 14, Y(liveY), 2.5, 0, 7); ctx.fill();
  }

  // right-edge label
  ctx.fillStyle = col; ctx.font = '11px JetBrains Mono, monospace'; ctx.textAlign = 'left';
  ctx.fillText(money(end), pad.l + w + 6, Y(end) + 3);
}

function drawZero(ctx, pad, w, h, lo, hi, yZero) {
  ctx.strokeStyle = '#222c34'; ctx.lineWidth = 1; ctx.setLineDash([2, 3]);
  ctx.beginPath(); ctx.moveTo(pad.l, yZero); ctx.lineTo(pad.l + w, yZero); ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = '#47525b'; ctx.font = '10px JetBrains Mono, monospace'; ctx.textAlign = 'left';
  ctx.fillText('0', pad.l + w + 6, yZero + 3);
}

poll();
setInterval(poll, POLL_MS);
