/* MT5 Position Studio — the whole client, in one file, no build step.
 *
 * Deliberately plain: this ships to one machine and has to keep working after
 * years of not being touched. A framework would mean a toolchain, a lockfile
 * and a build that rots. Vanilla ES modules-free JS against a small JSON API
 * does not rot.
 *
 * Layout of the file:
 *   1. helpers      — formatting, DOM building, toasts
 *   2. transport    — the token-carrying fetch wrapper
 *   3. state        — everything the views read from
 *   4. jobs         — the progress strip and its poller
 *   5. views        — overview, trades, insights, terminals, settings
 *   6. charts       — hand-drawn SVG, because two charts is not worth a library
 *   7. boot         — wiring
 */
'use strict';

/* ── 1. helpers ──────────────────────────────────────────────────── */
const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'html') node.innerHTML = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

/** A number with fixed decimals and thousands separators, or an em dash. */
function num(value, digits = 2) {
  if (!isNum(value)) return '—';
  return value.toLocaleString(undefined, {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  });
}

function money(value, digits = 2) {
  if (!isNum(value)) return '—';
  const body = Math.abs(value).toLocaleString(undefined, {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  });
  // Explicit sign on money: "-12.40" and "12.40" differ by one glyph, and that
  // glyph is the entire point of the number.
  return (value < 0 ? '-' : value > 0 ? '+' : '') + body + (state.currency ? ' ' + state.currency : '');
}

/** Money without the currency, for the oversized figures on the overview.
 *  The unit is rendered beside them at a smaller size instead - appended to a
 *  24px number it wraps onto its own line and the card doubles in height. */
function moneyBare(value, digits = 2) {
  if (!isNum(value)) return '—';
  const body = Math.abs(value).toLocaleString(undefined, {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  });
  return (value < 0 ? '-' : value > 0 ? '+' : '') + body;
}

const pct = (value, digits = 1) => (isNum(value) ? num(value, digits) + '%' : '—');
const rmult = (value) => (isNum(value) ? (value >= 0 ? '+' : '') + num(value, 2) + 'R' : '—');
const sign = (value) => (!isNum(value) || value === 0 ? '' : value > 0 ? 'is-up' : 'is-down');

/* A price at the instrument's own precision. MT5's digits per symbol are not
 * stored, so render five decimals and drop the padding zeros: an index at
 * 26,258.03 should not read "26,258.03000" next to a pair at 1.09342. */
function price(value) {
  if (!isNum(value)) return '—';
  let text = num(value, 5);
  // Drop padding zeros, but never below two decimals - a price is not "1.1".
  while (/\.\d{3,}$/.test(text) && text.endsWith('0')) text = text.slice(0, -1);
  return text;
}

/* Duration: seconds under 61, then m ss, then h mm, then d h mm. This mirrors
 * model.format_duration deliberately - the table and workbook print the Python
 * label and these KPIs print this one, and "average 18m" next to rows reading
 * "18m 30s" would look like two different measurements. */
function duration(seconds) {
  if (!isNum(seconds) || seconds < 0) return '—';
  if (seconds < 61) return (seconds >= 10 ? Math.round(seconds) : num(seconds, 1)) + 's';
  const total = Math.round(seconds);
  const pad = (n) => String(n).padStart(2, '0');
  if (total < 3600) return Math.floor(total / 60) + 'm ' + pad(total % 60) + 's';
  if (total < 86400) {
    return Math.floor(total / 3600) + 'h ' + pad(Math.floor((total % 3600) / 60)) + 'm';
  }
  return Math.floor(total / 86400) + 'd ' +
    Math.floor((total % 86400) / 3600) + 'h ' + pad(Math.floor((total % 3600) / 60)) + 'm';
}

function shortDate(text) {
  if (!text) return '—';
  return String(text).slice(0, 16).replace('T', ' ');
}

/* Epoch seconds, as a human reads a clock. Recent work gets relative wording -
 * "4 min ago" answers "did my run just finish?" faster than a wall-clock time
 * does - and anything older gets the date it happened on. */
function stamp(epoch) {
  if (!isNum(epoch) || epoch <= 0) return '—';
  const when = new Date(epoch * 1000);
  if (Number.isNaN(when.getTime())) return '—';
  const ageS = (Date.now() - when.getTime()) / 1000;
  if (ageS >= 0 && ageS < 45) return 'just now';
  if (ageS > 0 && ageS < 3600) return Math.round(ageS / 60) + ' min ago';
  const p = (n) => String(n).padStart(2, '0');
  const clock = p(when.getHours()) + ':' + p(when.getMinutes());
  const today = new Date();
  const sameDay = when.getFullYear() === today.getFullYear() &&
    when.getMonth() === today.getMonth() && when.getDate() === today.getDate();
  if (sameDay) return clock;
  return `${when.getFullYear()}-${p(when.getMonth() + 1)}-${p(when.getDate())} ${clock}`;
}

function toast(message, kind = '', title = '') {
  const node = el('div', { class: 'toast' + (kind ? ' is-' + kind : '') }, [
    title ? el('strong', { text: title }) : null,
    message,
  ]);
  $('#toasts').append(node);
  // Errors linger: the user may have been looking at the chart, not the corner.
  const life = kind === 'error' ? 9000 : 4200;
  setTimeout(() => {
    node.style.transition = 'opacity .3s, transform .3s';
    node.style.opacity = '0';
    node.style.transform = 'translateY(6px)';
    setTimeout(() => node.remove(), 320);
  }, life);
  return node;
}

/* ── 2. transport ────────────────────────────────────────────────── */
async function call(path, options = {}) {
  const init = {
    method: options.method || (options.body ? 'POST' : 'GET'),
    headers: { 'X-Studio-Token': window.STUDIO_TOKEN },
  };
  if (options.body) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(options.body);
  }
  let response;
  try {
    response = await fetch('/api/' + path, init);
  } catch (err) {
    // The server died or the window is closing. Saying so beats a silent no-op.
    throw new Error('Lost contact with the app service.');
  }
  const text = await response.text();
  let payload = {};
  try { payload = text ? JSON.parse(text) : {}; } catch { payload = { error: text }; }
  if (!response.ok) throw new Error(payload.error || ('HTTP ' + response.status));
  return payload;
}

const imageUrl = (rel) => '/charts/' + rel.split('/').map(encodeURIComponent).join('/') +
  '?token=' + encodeURIComponent(window.STUDIO_TOKEN);

/* ── 3. state ────────────────────────────────────────────────────── */
const state = {
  app: {},
  terminals: [],
  accounts: [],
  settings: {},
  runs: [],
  exports: [],
  accountId: null,
  currency: '',
  overview: null,
  positions: { rows: [], total: 0, offset: 0, limit: 100, symbols: [] },
  filters: { search: '', symbol: '', outcome: '' },
  insightGroup: 'by_symbol',
  job: null,
  // Highest job id already announced, so the same finished run is not toasted
  // on every poll. Seeded on first load: whatever finished before the page
  // opened is history, not news.
  announced: null,
  polling: false,
  view: 'overview',
  drawer: { detail: null, index: 0 },
};

const VIEW_TITLES = {
  overview:  ['Overview', 'Your record, measured rather than remembered.'],
  trades:    ['Trades', 'Every position, with what it offered and what you kept.'],
  insights:  ['Insights', 'Which groups earn and which ones only feel busy.'],
  terminals: ['Terminals', 'Which MetaTrader installs this app can read.'],
  settings:  ['Settings', 'Your thresholds, your naming, your numbers.'],
};

/* ── 3b. appearance ──────────────────────────────────────────────────
 * Theme and accent are attributes on <html>; app.css keys every colour off
 * them. Nothing here re-renders a view - the stylesheet does all of it, which
 * is why switching is instant.
 *
 * The interface is English-only. A Persian version means mirroring the layout
 * *and* translating every label, and a mirrored window still reading English is
 * worse than an honest left-to-right one - so there is no language switch until
 * the translation exists. Persian text the user types into a note still renders
 * correctly; that is the Arabic font and the bidi rule in app.css, and neither
 * needs a setting.
 */
function applyAppearance(ui) {
  const pref = {
    theme: ui?.theme === 'light' ? 'light' : 'dark',
    accent: ['cyan', 'violet', 'amber', 'rose'].includes(ui?.accent) ? ui.accent : 'cyan',
  };
  const root = document.documentElement;
  // Cyan and dark are what the stylesheet already is, so their attributes are
  // removed rather than set - no rule to match means no override to compute.
  if (pref.theme === 'light') root.dataset.theme = 'light';
  else delete root.dataset.theme;
  if (pref.accent !== 'cyan') root.dataset.accent = pref.accent;
  else delete root.dataset.accent;
  // Mirrored so the next launch paints correctly before the API answers. The
  // settings file stays the source of truth; this is only a head start.
  try {
    localStorage.setItem('pstudio.appearance', JSON.stringify(pref));
  } catch { /* Private mode, full disk - not worth failing a theme switch over. */ }
}

/* ── 4. jobs ─────────────────────────────────────────────────────── */
const strip = {
  root: null,
  init() {
    this.root = $('#jobstrip');
    $('#btn-cancel').addEventListener('click', async () => {
      $('#btn-cancel').disabled = true;
      try { await call('jobs/cancel', { body: {} }); }
      catch (err) { toast(err.message, 'error'); }
    });
  },
  render(job) {
    if (!job) { this.root.hidden = true; return; }
    this.root.hidden = false;
    $('#job-title').textContent = job.title;
    $('#job-message').textContent = job.cancelling ? 'Stopping…' : job.message;
    $('#job-counter').textContent = job.total ? `${job.done} / ${job.total}` : '';
    $('#job-elapsed').textContent = job.elapsed_s ? duration(job.elapsed_s) : '';

    const bar = $('#job-bar').parentElement;
    const known = isNum(job.fraction);
    bar.classList.toggle('is-indeterminate', !known && job.status === 'running');
    $('#job-bar').style.width = known ? (job.fraction * 100).toFixed(1) + '%' : '0';

    const pulse = $('#job-pulse');
    pulse.className = 'pulse' +
      (job.status === 'error' ? ' is-error' : job.status === 'done' ? ' is-done' : '');
    $('#btn-cancel').hidden = job.finished;
    $('#btn-cancel').disabled = job.cancelling;
  },
};

/** Poll while work is running. Stops the moment nothing is active, so an idle
 *  app makes no requests at all. */
async function pollJobs() {
  if (state.polling) return;
  state.polling = true;
  try {
    for (;;) {
      const { job } = await call('jobs');
      state.job = job;
      strip.render(job);
      if (!job) break;
      if (job.finished) {
        // The server reports the last finished job as well as a running one, so
        // a run that starts and ends between two polls is still seen. Announce
        // each id once - otherwise the same toast fires on every poll.
        if (state.announced !== job.id) {
          state.announced = job.id;
          await onJobFinished(job);
        }
        break;
      }
      $('#btn-run').disabled = true;
      await new Promise((r) => setTimeout(r, 550));
    }
  } catch (err) {
    toast(err.message, 'error');
  } finally {
    state.polling = false;
    $('#btn-run').disabled = false;
  }
}

async function onJobFinished(job) {
  if (job.status === 'done') {
    toast(job.message || 'Finished', 'good', job.title);
  } else if (job.status === 'cancelled') {
    toast('Stopped. Anything already finished was kept.', 'warn', job.title);
  } else {
    toast(job.error || 'Failed', 'error', job.title);
  }
  // Whatever ran, the picture changed. Reload rather than guess what to patch.
  await loadState();
  if (state.accountId) await loadAccount();
  // Leave the finished strip up for a beat so the summary can be read.
  setTimeout(() => { if (!state.job || state.job.finished) strip.render(null); }, 4000);
}

async function submit(path, body, label) {
  try {
    const job = await call(path, { body: body || {} });
    state.job = job;
    strip.render(job);
    pollJobs();
    return job;
  } catch (err) {
    toast(err.message, 'error', label || 'Could not start');
    return null;
  }
}

/* ── 5. views ────────────────────────────────────────────────────── */
function showView(name) {
  state.view = name;
  $$('.rail-btn[data-view]').forEach((b) =>
    b.classList.toggle('is-active', b.dataset.view === name));
  $$('.view').forEach((v) => v.classList.toggle('is-active', v.dataset.view === name));
  const [title, sub] = VIEW_TITLES[name] || [name, ''];
  $('#view-title').textContent = title;
  $('#view-sub').textContent = sub;
  if (name === 'trades' && !state.positions.rows.length) loadPositions();
  if (name === 'insights') renderInsights();
  if (name === 'settings') renderSettings();
}

/* -- overview ------------------------------------------------------ */
function kpiHero(label, value, note, tone, unit) {
  return el('div', { class: 'kpi-hero' + (tone ? ' is-' + tone : '') }, [
    el('div', { class: 'label', text: label }),
    el('div', {
      class: 'value ' + (tone === 'up' ? 'is-up' : tone === 'down' ? 'is-down' : ''),
    }, [value, unit ? el('span', { class: 'unit', text: unit }) : null]),
    note ? el('div', { class: 'note', text: note }) : null,
  ]);
}

function kpi(label, value, note, tone) {
  const missing = value === '—';
  return el('div', { class: 'kpi' }, [
    el('div', { class: 'label', text: label }),
    el('div', {
      class: 'value' + (missing ? ' is-none' : tone ? ' is-' + tone : ''),
      text: missing ? 'not enough data' : value,
    }),
    note ? el('div', { class: 'note', text: note }) : null,
  ]);
}

function renderOverview() {
  const has = Boolean(state.overview && state.overview.summary.trades);
  $('#ov-empty').hidden = has;
  $('#ov-body').hidden = !has;
  if (!has) {
    $('#ov-empty').hidden = false;
    if (state.accounts.length) {
      $('#ov-empty h2').textContent = 'Nothing analyzed yet';
      $('#ov-empty p').textContent =
        'The account is connected. Run a full analysis to read the history, ' +
        'render the charts and build the workbook.';
      $('#ov-empty button').textContent = 'Run full analysis';
      $('#ov-empty button').dataset.action = 'run';
    }
    return;
  }

  const o = state.overview;
  const s = o.summary;
  const c = o.counts;

  $('#ov-headline').replaceChildren(
    kpiHero('Net result', moneyBare(s.net_profit),
      s.return_pct !== null ? pct(s.return_pct, 2) + ' on starting balance' : '',
      s.net_profit > 0 ? 'up' : s.net_profit < 0 ? 'down' : null, state.currency),
    kpiHero('Balance now', num(s.end_balance),
      'from ' + num(s.start_balance), 'accent', state.currency),
    kpiHero('Trades', String(s.trades),
      `${s.wins}W · ${s.losses}L${s.scratches ? ' · ' + s.scratches + ' flat' : ''}`),
    kpiHero('Win rate', pct(s.win_rate, 2),
      'payoff ' + num(s.payoff_ratio, 2) + '×'),
    kpiHero('Expectancy', moneyBare(s.expectancy, 2), rmult(s.expectancy_r) + ' per trade',
      s.expectancy > 0 ? 'up' : 'down', state.currency),
    kpiHero('Max drawdown', moneyBare(-Math.abs(s.max_drawdown)),
      pct(s.max_drawdown_pct, 2) + ' of peak', 'down', state.currency),
  );

  $('#ov-curve-note').textContent =
    `${s.trades} closed trades · peak ${num(Math.max(...s.equity_curve))}`;
  drawEquity($('#ov-curve'), s.equity_curve, s.start_balance);
  drawDonut($('#ov-donut'), s);

  $('#ov-metrics').replaceChildren(
    kpi('Profit factor', num(s.profit_factor, 3), 'gross win / gross loss',
      s.profit_factor >= 1 ? 'up' : 'down'),
    kpi('Gross profit', money(s.gross_profit), null, 'up'),
    kpi('Gross loss', money(s.gross_loss), null, 'down'),
    kpi('Average win', money(s.avg_win), null, 'up'),
    kpi('Average loss', money(s.avg_loss), null, 'down'),
    kpi('Largest win', money(s.largest_win), null, 'up'),
    kpi('Largest loss', money(s.largest_loss), null, 'down'),
    kpi('Median trade', money(s.median_trade)),
    kpi('SQN', num(s.sqn, 2), s.sqn_grade || `needs ${state.app.min_sample} trades`,
      isNum(s.sqn) && s.sqn >= 2 ? 'up' : isNum(s.sqn) && s.sqn < 1 ? 'down' : null),
    kpi('Recovery factor', num(s.recovery_factor, 2), 'net / max drawdown'),
    kpi('Best run', money(s.best_run_money), s.max_win_streak + ' in a row', 'up'),
    kpi('Worst run', money(s.worst_run_money), s.max_loss_streak + ' in a row', 'down'),
  );

  $('#ov-risk').replaceChildren(
    kpi('Average risk', money(s.avg_risk_money), 'per trade, from the initial stop'),
    kpi('Risk as % of balance', pct(s.avg_risk_pct, 2),
      isNum(s.max_risk_pct) ? 'worst ' + pct(s.max_risk_pct, 2) : null,
      isNum(s.avg_risk_pct) && s.avg_risk_pct > 2 ? 'warn' : null),
    kpi('Risk consistency', num(s.risk_consistency, 2),
      'spread of risk / average risk — lower is steadier',
      isNum(s.risk_consistency) && s.risk_consistency > 0.5 ? 'warn' : null),
    kpi('Planned reward:risk', num(s.avg_planned_rr, 2) + ' : 1',
      'what the stop and target promised'),
    kpi('Drawdown length', s.longest_dd_trades + ' trades',
      'longest stretch below a new high'),
    kpi('Current streak',
      (s.current_streak > 0 ? s.current_streak + ' wins'
        : s.current_streak < 0 ? Math.abs(s.current_streak) + ' losses' : 'flat'),
      null, s.current_streak > 0 ? 'up' : s.current_streak < 0 ? 'down' : null),
  );

  $('#ov-manage').replaceChildren(
    kpi('Average MFE', money(s.avg_mfe), 'best unrealised gain offered', 'up'),
    kpi('Average MAE', money(s.avg_mae), 'worst unrealised loss endured', 'down'),
    kpi('Capture of winners', pct(isNum(s.capture_of_winners) ? s.capture_of_winners * 100 : null, 1),
      'money kept out of money offered',
      isNum(s.capture_of_winners) && s.capture_of_winners < 0.5 ? 'warn' : null),
    kpi('Heat on winners', money(s.avg_winner_mae),
      'how far a winner went against you first'),
    kpi('Left on the table', money(s.avg_loser_mfe),
      'average MFE of trades that ended red'),
    kpi('Median heat', pct(s.median_heat_pct, 1),
      isNum(s.p90_heat_pct) ? '90th percentile ' + pct(s.p90_heat_pct, 1) : null),
    kpi('Gave it back', s.reversed_winners + ' trades',
      `losers that first reached ${s.reversed_winners_threshold_r}R` +
      (isNum(s.reversed_winners_pct) ? ` · ${pct(s.reversed_winners_pct, 1)} of losers` : ''),
      s.reversed_winners > 0 ? 'warn' : null),
    kpi('Average duration', duration(s.avg_duration_s),
      'median ' + duration(s.median_duration_s)),
    kpi('Charts on disk', String(c.shots),
      `${c.positions} positions · ${c.pending} awaiting work`),
  );

  const mc = o.monte_carlo;
  $('#ov-mc-note').textContent = mc.available
    ? `${mc.runs.toLocaleString()} runs · ${mc.horizon} trades ahead · resampled from your own results`
    : mc.reason;
  $('#ov-monte').replaceChildren(
    ...(mc.available ? [
      kpi('Median outcome', num(mc.median_final), 'balance after ' + mc.horizon + ' trades'),
      kpi('Bad case (5%)', num(mc.p05_final), '1 run in 20 ends below this', 'down'),
      kpi('Good case (95%)', num(mc.p95_final), '1 run in 20 ends above this', 'up'),
      kpi('Likely drawdown', money(-Math.abs(mc.median_drawdown)), 'median worst dip'),
      kpi('Drawdown to plan for', money(-Math.abs(mc.p95_drawdown)),
        '95th percentile — not the worst case', 'warn'),
      kpi('Chance of profit', pct(mc.profitable_pct, 1), null,
        mc.profitable_pct >= 50 ? 'up' : 'down'),
      kpi('Risk of halving', pct(mc.risk_of_50pct_loss, 2),
        'runs that ever dropped 50%',
        isNum(mc.risk_of_50pct_loss) && mc.risk_of_50pct_loss > 1 ? 'warn' : null),
    ] : [kpi('Simulation', '—', mc.reason)]),
  );

  const runs = state.runs.length ? state.runs.map((run) =>
    el('div', { class: 'list-row' }, [
      el('span', { class: 'dot ' + (run.status === 'ok' ? 'ok' : run.status === 'error' ? 'bad' : 'warn') }),
      el('div', { class: 'grow' }, [
        el('div', { class: 'title', text: run.kind.charAt(0).toUpperCase() + run.kind.slice(1) }),
        el('div', { class: 'sub', text: (run.detail || '') + (run.error ? ' — ' + run.error : '') }),
      ]),
      el('span', {
        class: 'muted mono', text: stamp(run.started_at),
        title: isNum(run.finished_at) && isNum(run.started_at)
          ? 'took ' + duration(run.finished_at - run.started_at) : '',
      }),
    ])) : [el('div', { class: 'list-empty', text: 'No runs recorded yet.' })];
  $('#ov-runs').replaceChildren(...runs);

  const exports = state.exports.length ? state.exports.map((file) =>
    el('div', { class: 'list-row' }, [
      el('div', { class: 'grow' }, [
        el('div', { class: 'title', text: file.name }),
        el('div', { class: 'sub', text: (file.size / 1048576).toFixed(2) + ' MB · ' + stamp(file.modified) }),
      ]),
      el('button', {
        class: 'btn ghost small', text: 'Open',
        onclick: () => reveal(file.path),
      }),
    ])) : [el('div', { class: 'list-empty', text: 'No workbooks yet. Run a full analysis.' })];
  $('#ov-exports').replaceChildren(...exports);
}

/* -- trades -------------------------------------------------------- */
function renderTrades() {
  const p = state.positions;
  const body = $('#t-body');
  if (!p.rows.length) {
    body.replaceChildren(el('tr', {}, [
      el('td', { colspan: '13', class: 'list-empty', text: 'No positions match.' }),
    ]));
  } else {
    body.replaceChildren(...p.rows.map((row) => {
      const open = !row.close_time;
      return el('tr', {
        class: open ? 'is-open-pos' : '',
        onclick: () => openDrawer(row.id),
      }, [
        el('td', { class: 'mono', text: row.ticket }),
        el('td', {}, [el('strong', { text: row.symbol })]),
        el('td', {}, [el('span', { class: 'tag ' + row.side, text: row.side })]),
        el('td', { class: 'mono dim', text: shortDate(row.open_time) }),
        el('td', { class: 'num', text: row.duration_label || '—' }),
        el('td', { class: 'num ' + sign(row.net_profit), text: money(row.net_profit) }),
        el('td', { class: 'num ' + sign(row.r_multiple), text: rmult(row.r_multiple) }),
        el('td', { class: 'num is-up', text: isNum(row.mfe_money) ? num(row.mfe_money) : '—' }),
        el('td', { class: 'num is-down', text: isNum(row.mae_money) ? num(row.mae_money) : '—' }),
        el('td', { class: 'num dim', text: pct(row.heat_pct, 0) }),
        el('td', { class: 'dim', text: row.session || '—' }),
        el('td', { class: 'dim', text: open ? 'still open' : (row.exit_reason || '—') }),
        el('td', { class: 'num' }, [
          row.shots
            ? el('span', { class: 'tag info', text: String(row.shots) })
            : el('span', { class: 'dim', text: '0' }),
        ]),
      ]);
    }));
  }
  $('#t-count').textContent = p.total + (p.total === 1 ? ' position' : ' positions');
  const from = p.total ? p.offset + 1 : 0;
  const to = Math.min(p.offset + p.limit, p.total);
  $('#t-range').textContent = `${from}–${to} of ${p.total}`;
  $('#t-prev').disabled = p.offset <= 0;
  $('#t-next').disabled = to >= p.total;

  const select = $('#f-symbol');
  if (select.options.length - 1 !== p.symbols.length) {
    select.replaceChildren(
      el('option', { value: '', text: 'All' }),
      ...p.symbols.map((sym) => el('option', { value: sym, text: sym })),
    );
    select.value = state.filters.symbol;
  }
}

/* -- drawer -------------------------------------------------------- */
async function openDrawer(positionId) {
  try {
    const detail = await call('positions/' + positionId);
    state.drawer = { detail, index: 0 };
    renderDrawer();
    $('#drawer').hidden = false;
  } catch (err) {
    toast(err.message, 'error');
  }
}

function renderDrawer() {
  const { detail } = state.drawer;
  if (!detail) return;
  const row = detail.position;

  $('#d-title').textContent = `${row.symbol} ${row.side.toUpperCase()} · #${row.ticket}`;
  $('#d-sub').textContent =
    `${shortDate(row.open_time)} → ${row.close_time ? shortDate(row.close_time) : 'still open'}` +
    `   ·   ${row.duration_label}   ·   ${row.volume} lots`;

  $('#d-metrics').replaceChildren(
    kpi('Net', money(row.net_profit), null, sign(row.net_profit) === 'is-up' ? 'up' : 'down'),
    kpi('R multiple', rmult(row.r_multiple), 'planned ' + num(row.planned_rr, 2) + ':1'),
    kpi('MFE', isNum(row.mfe_money) ? money(row.mfe_money) : '—', 'best it offered', 'up'),
    kpi('MAE', isNum(row.mae_money) ? money(row.mae_money) : '—', 'worst it endured', 'down'),
    kpi('Heat', pct(row.heat_pct, 1), 'of planned risk used'),
    kpi('Capture', pct(isNum(row.capture_ratio) ? row.capture_ratio * 100 : null, 1),
      'of the move you kept'),
    kpi('Entry', price(row.open_price)),
    kpi('Exit', row.close_price ? price(row.close_price) : '—', row.exit_reason || null),
    kpi('Stop', row.sl_initial ? price(row.sl_initial) : 'none set',
      row.sl_initial ? null : 'no initial stop on this trade'),
    kpi('Target', row.tp_initial ? price(row.tp_initial) : 'none set'),
    kpi('Session', row.session || '—', row.weekday || null),
    kpi('Measured from', row.excursion_source || '—', 'source of MAE and MFE'),
  );

  const tabs = detail.shots.map((shot, index) =>
    el('button', {
      class: 'shot-tab' + (index === state.drawer.index ? ' is-active' : '') +
        (shot.exists ? '' : ' is-missing'),
      onclick: () => { state.drawer.index = index; renderDrawer(); },
    }, [
      el('span', { class: 'tf', text: shot.timeframe }),
      el('span', { text: shot.event }),
    ]));
  $('#d-tabs').replaceChildren(...(tabs.length ? tabs
    : [el('span', { class: 'muted', text: 'No charts rendered for this position yet.' })]));

  const shot = detail.shots[state.drawer.index];
  const image = $('#d-image');
  if (shot && shot.exists) {
    image.src = imageUrl(shot.rel_path);
    image.hidden = false;
    $('#d-caption').textContent =
      `${shot.timeframe} ${shot.event} · ${shot.bars} candles · ${shot.rel_path}`;
  } else {
    image.hidden = true;
    image.removeAttribute('src');
    $('#d-caption').textContent = shot
      ? 'The file for this shot is no longer on disk. Re-run capture to rebuild it.'
      : 'Run a capture to produce the chart images.';
  }

  $('#d-note').value = row.note || '';
  $('#d-note-status').textContent = '';
  $('#d-folder').disabled = !detail.folder;
}

/* -- insights ------------------------------------------------------ */
function renderInsights() {
  $('#ins-minsample').textContent = state.app.min_sample || 20;
  const rows = (state.overview && state.overview[state.insightGroup]) || [];
  const peak = Math.max(1, ...rows.map((r) => Math.abs(r.net)));
  const body = $('#ins-body');
  if (!rows.length) {
    body.replaceChildren(el('tr', {}, [
      el('td', { colspan: '11', class: 'list-empty', text: 'Nothing to group yet.' }),
    ]));
    return;
  }
  body.replaceChildren(...rows.map((r) => el('tr', {}, [
    el('td', {}, [
      el('strong', { text: r.key }),
      r.trades < (state.app.min_sample || 20)
        ? el('span', { class: 'tag warn', style: 'margin-left:8px', text: 'thin' })
        : null,
    ]),
    el('td', { class: 'num', text: r.trades }),
    el('td', { class: 'num', text: pct(r.win_rate, 1) }),
    el('td', { class: 'num ' + sign(r.net), text: money(r.net) }),
    el('td', { class: 'num', text: num(r.profit_factor, 2) }),
    el('td', { class: 'num ' + sign(r.expectancy), text: money(r.expectancy, 2) }),
    el('td', { class: 'num ' + sign(r.expectancy_r), text: rmult(r.expectancy_r) }),
    el('td', { class: 'num dim', text: pct(r.share_of_net, 1) }),
    el('td', { class: 'num is-up', text: num(r.best) }),
    el('td', { class: 'num is-down', text: num(r.worst) }),
    el('td', {}, [
      el('div', { class: 'weight' }, [
        el('i', {
          style: `width:${(Math.abs(r.net) / peak * 100).toFixed(1)}%;` +
            `background:${r.net >= 0 ? 'var(--up)' : 'var(--down)'}`,
        }),
      ]),
    ]),
  ])));
}

/* -- terminals ----------------------------------------------------- */
function renderTerminals() {
  const rows = state.terminals.map((t) => {
    const dot = t.last_error ? 'bad' : t.exists === false ? 'warn' : t.enabled ? 'ok' : 'off';
    // Broker, build and the logins seen there are how you tell three installs
    // apart when all three are called "MT5 - something".
    const facts = [
      t.broker || null,
      t.build ? 'build ' + t.build : null,
      (t.known_logins || []).length
        ? 'login ' + (t.known_logins || []).join(', ')
        : 'no account seen yet',
    ].filter(Boolean).join('  ·  ');
    return el('div', { class: 'list-row' }, [
      el('span', {
        class: 'dot ' + dot,
        title: t.last_error || (t.exists === false ? 'the file is gone'
          : t.enabled ? 'ready' : 'disabled'),
      }),
      el('div', { class: 'grow' }, [
        el('div', { class: 'title' }, [
          t.name || 'MetaTrader 5',
          t.is_manual ? el('span', { class: 'tag', style: 'margin-left:8px', text: 'added by hand' }) : null,
          t.is_portable ? el('span', { class: 'tag info', style: 'margin-left:6px', text: 'portable' }) : null,
          t.exists === false ? el('span', { class: 'tag warn', style: 'margin-left:6px', text: 'missing' }) : null,
        ]),
        el('div', { class: 'sub', text: facts }),
        el('div', { class: 'sub dim', text: t.exe_path }),
        t.last_error ? el('div', { class: 'sub', style: 'color:var(--down)', text: t.last_error }) : null,
      ]),
      el('button', {
        class: 'btn ghost small', text: 'Connect',
        onclick: () => submit(`terminals/${t.id}/probe`, {}, 'Connect'),
      }),
      el('button', {
        class: 'btn ghost small', text: 'Read history',
        onclick: () => submit(`terminals/${t.id}/sync`, {}, 'Read history'),
      }),
      el('button', {
        class: 'btn ghost small', text: t.enabled ? 'Disable' : 'Enable',
        onclick: async () => {
          try {
            const out = await call(`terminals/${t.id}/enabled`, { body: { enabled: !t.enabled } });
            state.terminals = out.terminals;
            renderTerminals();
          } catch (err) { toast(err.message, 'error'); }
        },
      }),
      el('button', {
        class: 'btn ghost small danger', text: 'Remove',
        onclick: async () => {
          try {
            const out = await call(`terminals/${t.id}`, { method: 'DELETE' });
            state.terminals = out.terminals;
            renderTerminals();
            toast('Removed. Nothing on disk was touched.', 'good');
          } catch (err) { toast(err.message, 'error'); }
        },
      }),
    ]);
  });
  $('#term-list').replaceChildren(...(rows.length ? rows
    : [el('div', { class: 'list-empty', text: 'No terminals yet. Press “Rescan this PC”.' })]));

  const accounts = state.accounts.map((a) => el('div', { class: 'list-row' }, [
    el('span', { class: 'dot ok' }),
    el('div', { class: 'grow' }, [
      el('div', { class: 'title' }, [
        `${a.login} — ${a.holder || 'unnamed'}`,
        a.is_demo ? el('span', { class: 'pill', text: 'demo' }) : null,
      ]),
      el('div', { class: 'sub', text: `${a.server || '?'} · ${a.currency || '?'} · ` +
        `1:${a.leverage || '?'} · ${a.company || ''}` }),
    ]),
    el('div', { class: 'stack-right' }, [
      // A balance is not a gain or a loss, so no explicit sign here.
      el('span', { class: 'mono', text: num(a.balance) + ' ' + (a.currency || '') }),
      el('span', { class: 'muted mono tiny', text: 'from ' + num(a.start_balance) }),
    ]),
    el('button', {
      class: 'btn ghost small', text: 'Select',
      onclick: () => { $('#account-select').value = String(a.id); onAccountChange(); showView('overview'); },
    }),
  ]));
  $('#acct-list').replaceChildren(...(accounts.length ? accounts
    : [el('div', { class: 'list-empty', text: 'Connect a terminal to discover its account.' })]));
}

/* -- settings ------------------------------------------------------ */
/* Fields are declared rather than hand-written so the form and the JSON stay in
 * step: adding a setting to settings.py means adding one line here. */
/* Readable names for the stored codes. Only codes that need saying out loud are
 * here - "png" and "auto" already read as themselves. */
const OPTION_LABELS = {
  dark: 'Dark — for a night session',
  light: 'Light — for a lit room',
  cyan: 'Cyan',
  violet: 'Violet',
  amber: 'Amber',
  rose: 'Rose',
  midnight: 'Midnight (dark charts)',
  daylight: 'Daylight (light charts)',
};

const SETTING_FIELDS = {
  'set-ui': [
    ['ui.theme', 'Interface theme', 'select:dark,light',
      'Dark is built for a night session. Light is for a sunlit desk.'],
    ['ui.accent', 'Accent colour', 'select:cyan,violet,amber,rose',
      'Green and red are reserved for wins and losses, so the accent never uses them.'],
    ['capture.theme', 'Chart image theme', 'select:midnight,daylight',
      'Separate from the interface: shots printed for someone else often want ' +
      'daylight while the app stays dark. Takes effect on the next capture.'],
    ['ui.auto_scan_on_start', 'Look for MetaTrader on first launch', 'bool',
      'Only runs when no terminal is known yet.'],
  ],
  'set-capture': [
    ['capture.candles_min', 'Fewest candles', 'number', 'Lower bound of the visible window.'],
    ['capture.candles_target', 'Target candles', 'number', 'What the renderer aims for.'],
    ['capture.candles_max', 'Most candles', 'number', 'Upper bound.'],
    ['capture.entry_anchor', 'Entry position in frame', 'number', '0 = left edge, 1 = right. 0.62 leaves room for what came next.'],
    ['capture.exit_anchor', 'Exit position in frame', 'number', ''],
    ['capture.open_timeframes', 'Shots at entry', 'csv', 'Comma separated, from H4 H1 M15 M5 M1.'],
    ['capture.close_timeframes', 'Shots at exit', 'csv', ''],
    ['capture.render_journey', 'Add an entry-to-exit shot', 'bool', 'One frame spanning the whole trade when it fits.'],
    ['capture.width', 'Image width', 'number', ''],
    ['capture.height', 'Image height', 'number', ''],
    ['capture.supersample', 'Supersampling', 'number', 'Draw at this multiple, then downscale. 2 is plenty.'],
    ['capture.image_format', 'Format', 'select:png,jpg,webp', ''],
    ['capture.skip_existing', 'Skip shots already on disk', 'bool', 'Turn off to force a redraw.'],
  ],
  'set-analysis': [
    ['analysis.excursion_source', 'MAE / MFE source', 'select:auto,ticks,bars',
      'Ticks are exact but slow; bars use M1 highs and lows; auto picks per trade.'],
    ['analysis.tick_trade_max_minutes', 'Use ticks up to (minutes)', 'number',
      'Above this, auto falls back to bars.'],
    ['analysis.seconds_threshold', 'Report seconds below', 'number',
      'Durations under this show as seconds.'],
    ['analysis.monte_carlo_runs', 'Monte Carlo runs', 'number', ''],
    ['analysis.monte_carlo_horizon', 'Trades simulated ahead', 'number', ''],
    ['profile.account_risk_pct', 'Risk per trade you intend (%)', 'number',
      'Used to flag trades that were sized past your own rule.'],
    ['profile.target_r', 'Reward:risk you aim for', 'number', ''],
  ],
  'set-naming': [
    ['naming.folder', 'Folder template', 'text', 'One folder per position, under the account folder.'],
    ['naming.file', 'File template', 'text', ''],
    ['naming.date_format', 'Date format', 'text', 'Python strftime, e.g. %Y-%m-%d.'],
    ['naming.time_format', 'Time format', 'text', ''],
  ],
};

const dig = (obj, dotted) => dotted.split('.').reduce((n, k) => (n || {})[k], obj);

function renderSettings() {
  for (const [hostId, fields] of Object.entries(SETTING_FIELDS)) {
    const host = document.getElementById(hostId);
    host.replaceChildren(...fields.map(([path, label, kind, hint]) => {
      const value = dig(state.settings, path);
      let input;
      if (kind === 'bool') {
        input = el('label', { class: 'check' }, [
          el('input', { type: 'checkbox', 'data-path': path, 'data-kind': kind }),
          label,
        ]);
        $('input', input).checked = Boolean(value);
        return el('div', { class: 'field' + (hint ? '' : '') }, [
          input, hint ? el('div', { class: 'hint', text: hint }) : null,
        ]);
      }
      if (kind.startsWith('select:')) {
        input = el('select', { 'data-path': path, 'data-kind': 'text' },
          kind.slice(7).split(',').map((opt) => el('option', {
            // The value stored is the code; the label is what a person reads.
            // "en" and "fa" are correct in settings.json and meaningless in a
            // dropdown, so OPTION_LABELS covers the ones that need saying.
            value: opt, text: OPTION_LABELS[opt] || opt,
          })));
        input.value = value;
      } else if (kind === 'csv') {
        input = el('input', {
          type: 'text', 'data-path': path, 'data-kind': kind,
          value: Array.isArray(value) ? value.join(', ') : String(value ?? ''),
        });
      } else {
        input = el('input', {
          type: kind === 'number' ? 'number' : 'text',
          step: kind === 'number' ? 'any' : null,
          'data-path': path, 'data-kind': kind,
          value: value ?? '',
        });
      }
      return el('label', { class: 'field' + (kind === 'text' && path.includes('naming') ? ' wide' : '') }, [
        el('span', { text: label }),
        input,
        hint ? el('div', { class: 'hint', text: hint }) : null,
      ]);
    }));
  }

  $('#set-danger').replaceChildren(
    el('div', { class: 'field wide' }, [
      el('span', { text: 'Re-analyze everything' }),
      el('div', { class: 'row-actions' }, [
        el('button', {
          class: 'btn ghost small', text: 'Clear analysis for this account',
          onclick: async () => {
            if (!state.accountId) return;
            try {
              const out = await call(`accounts/${state.accountId}/reset`, { body: {} });
              toast(`${out.reset} positions marked for re-analysis.`, 'good');
              await loadAccount();
            } catch (err) { toast(err.message, 'error'); }
          },
        }),
      ]),
      el('div', {
        class: 'hint',
        text: 'Forgets computed metrics and capture stamps. Images already on disk are ' +
          'kept — turn off “skip shots already on disk” above if you want them redrawn.',
      }),
    ]),
    el('div', { class: 'field wide' }, [
      el('span', { text: 'Where things live' }),
      el('div', { class: 'row-actions' }, [
        el('button', { class: 'btn ghost small', text: 'Charts folder', onclick: () => reveal(state.app.charts_dir) }),
        el('button', { class: 'btn ghost small', text: 'Workbooks folder', onclick: () => reveal(state.app.exports_dir) }),
        el('button', { class: 'btn ghost small', text: 'App data', onclick: () => reveal(state.app.root) }),
      ]),
      el('div', {
        class: 'hint',
        text: (state.app.portable ? 'Running portable — everything is beside the app. '
          : 'Running installed — data is in your user folder. ') + state.app.root,
      }),
    ]),
  );
}

function collectSettings() {
  const patch = {};
  for (const input of $$('[data-path]')) {
    const path = input.dataset.path;
    const kind = input.dataset.kind;
    let value;
    if (kind === 'bool') value = input.checked;
    else if (kind === 'number') value = input.value === '' ? null : Number(input.value);
    else if (kind === 'csv') {
      value = input.value.split(',').map((s) => s.trim().toUpperCase()).filter(Boolean);
    } else value = input.value;
    if (value === null || (kind === 'number' && Number.isNaN(value))) continue;
    // Rebuild the nested shape the API expects from the dotted path.
    const parts = path.split('.');
    let node = patch;
    parts.slice(0, -1).forEach((part) => { node = node[part] = node[part] || {}; });
    node[parts.at(-1)] = value;
  }
  return patch;
}

/* ── 6. charts ───────────────────────────────────────────────────── */
function svg(tag, attrs = {}) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value !== null && value !== undefined) node.setAttribute(key, String(value));
  }
  return node;
}

/** Equity curve: a filled area with the starting balance marked.
 *  Drawn by hand because it is one polyline, and a charting library would be
 *  400 KB to draw one polyline. */
function drawEquity(host, curve, startBalance) {
  const W = 620, H = 210, padL = 52, padR = 10, padT = 12, padB = 24;
  const points = [startBalance || curve[0], ...curve];
  const lo = Math.min(...points), hi = Math.max(...points);
  const span = hi - lo || 1;
  const x = (i) => padL + (i / Math.max(1, points.length - 1)) * (W - padL - padR);
  const y = (v) => padT + (1 - (v - lo) / span) * (H - padT - padB);

  const root = svg('svg', { viewBox: `0 0 ${W} ${H}`, class: 'chart-line' });
  const defs = svg('defs');
  const grad = svg('linearGradient', { id: 'eqfill', x1: '0', y1: '0', x2: '0', y2: '1' });
  grad.append(
    svg('stop', { offset: '0', 'stop-color': '#38E1D0', 'stop-opacity': '0.26' }),
    svg('stop', { offset: '1', 'stop-color': '#38E1D0', 'stop-opacity': '0' }),
  );
  defs.append(grad);
  root.append(defs);

  // Four gridlines with their values, so the curve can actually be read.
  for (let i = 0; i <= 4; i += 1) {
    const value = lo + (span * i) / 4;
    const yy = y(value);
    root.append(svg('line', { class: 'grid-line', x1: padL, y1: yy, x2: W - padR, y2: yy }));
    const label = svg('text', { class: 'axis-text', x: padL - 7, y: yy + 3, 'text-anchor': 'end' });
    label.textContent = value.toFixed(0);
    root.append(label);
  }

  if (isNum(startBalance)) {
    const yy = y(startBalance);
    root.append(svg('line', { class: 'zero-line', x1: padL, y1: yy, x2: W - padR, y2: yy }));
  }

  const line = points.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
  root.append(svg('polygon', {
    class: 'series-area',
    points: `${padL},${H - padB} ${line} ${W - padR},${H - padB}`,
  }));
  root.append(svg('polyline', { class: 'series-line', points: line }));

  const last = points.at(-1);
  root.append(svg('circle', {
    cx: x(points.length - 1), cy: y(last), r: 3.5,
    fill: last >= (startBalance || 0) ? '#3DDC97' : '#FF6B6B',
    stroke: '#0B0F17', 'stroke-width': '1.5',
  }));

  const first = svg('text', { class: 'axis-text', x: padL, y: H - 7 });
  first.textContent = 'trade 1';
  const lastLabel = svg('text', { class: 'axis-text', x: W - padR, y: H - 7, 'text-anchor': 'end' });
  lastLabel.textContent = 'trade ' + curve.length;
  root.append(first, lastLabel);

  host.replaceChildren(root);
}

/** Outcome donut. Same colours as the workbook: blue-green wins, red losses. */
function drawDonut(host, summary) {
  const slices = [
    { label: 'Wins', value: summary.wins, colour: '#3DDC97' },
    { label: 'Losses', value: summary.losses, colour: '#FF6B6B' },
    { label: 'Flat', value: summary.scratches, colour: '#8B9AB2' },
  ].filter((s) => s.value > 0);
  const total = slices.reduce((sum, s) => sum + s.value, 0) || 1;

  const S = 210, R = 82, r = 54, cx = S / 2, cy = S / 2;
  const root = svg('svg', { viewBox: `0 0 ${S} ${S}`, class: 'chart-donut' });
  let angle = -Math.PI / 2;

  for (const slice of slices) {
    const sweep = (slice.value / total) * Math.PI * 2;
    const end = angle + sweep;
    const large = sweep > Math.PI ? 1 : 0;
    const p = (radius, a) => `${(cx + radius * Math.cos(a)).toFixed(2)},${(cy + radius * Math.sin(a)).toFixed(2)}`;
    // A full circle cannot be drawn as one arc — the start and end coincide and
    // the path collapses. One slice means one ring, so draw it as a ring.
    if (slices.length === 1) {
      root.append(svg('circle', { cx, cy, r: (R + r) / 2, fill: 'none',
        stroke: slice.colour, 'stroke-width': R - r }));
    } else {
      root.append(svg('path', {
        d: `M ${p(R, angle)} A ${R} ${R} 0 ${large} 1 ${p(R, end)}` +
           ` L ${p(r, end)} A ${r} ${r} 0 ${large} 0 ${p(r, angle)} Z`,
        fill: slice.colour, stroke: '#0B0F17', 'stroke-width': '1.5',
      }));
    }
    angle = end;
  }

  const headline = svg('text', { class: 'donut-label', x: cx, y: cy - 2, 'text-anchor': 'middle' });
  headline.textContent = (summary.win_rate ?? 0).toFixed(1) + '%';
  const sub = svg('text', { class: 'donut-sub', x: cx, y: cy + 14, 'text-anchor': 'middle' });
  sub.textContent = 'win rate';
  root.append(headline, sub);

  const legend = el('div', { class: 'legend' }, slices.map((s) =>
    el('span', {}, [
      el('i', { style: 'background:' + s.colour }),
      `${s.label} ${s.value} · ${((s.value / total) * 100).toFixed(1)}%`,
    ])));
  host.replaceChildren(root, legend);
}

/* ── 7. boot ─────────────────────────────────────────────────────── */
async function reveal(path) {
  if (!path) return;
  try { await call('open', { body: { path } }); }
  catch (err) { toast(err.message, 'error'); }
}

async function loadState() {
  const data = await call('state');
  Object.assign(state, {
    app: data.app,
    terminals: data.terminals,
    accounts: data.accounts,
    settings: data.settings,
    runs: data.runs,
    exports: data.exports,
  });
  // The settings file is the source of truth for appearance. The inline script
  // in index.html has already painted from the mirrored copy; this corrects it
  // if the two ever disagree - a settings.json edited by hand, say.
  applyAppearance(state.settings.ui);

  const select = $('#account-select');
  const previous = state.accountId;
  select.replaceChildren(...(state.accounts.length
    ? state.accounts.map((a) => el('option', {
        value: String(a.id), text: `${a.login} · ${a.server || 'unknown server'}`,
      }))
    : [el('option', { value: '', text: 'no account yet' })]));
  if (state.accounts.length) {
    const keep = state.accounts.some((a) => a.id === previous) ? previous : state.accounts[0].id;
    state.accountId = keep;
    select.value = String(keep);
    const account = state.accounts.find((a) => a.id === keep);
    state.currency = account ? (account.currency || '') : '';
  } else {
    state.accountId = null;
  }

  $('#view-sub').textContent = state.app.mt5_available
    ? (VIEW_TITLES[state.view] || ['', ''])[1]
    : 'The MetaTrader5 Python package is missing — install it to connect.';

  renderTerminals();
  if (state.view === 'settings') renderSettings();
  // Anything that finished before this page loaded is history, not news: mark it
  // announced so the first poll does not toast an old run.
  if (state.announced === null) {
    const finished = (data.jobs || []).filter((j) => j.finished).map((j) => j.id);
    state.announced = finished.length ? Math.max(...finished) : 0;
  }
  // Only touch the strip when the server reports live work. When it reports
  // nothing, a just-finished strip is still on screen being read - leave it to
  // its own timer.
  if (data.job) {
    state.job = data.job;
    strip.render(data.job);
    if (!data.job.finished) pollJobs();
  }
  populateRunTerminals();
}

async function loadAccount() {
  if (!state.accountId) { renderOverview(); return; }
  try {
    state.overview = await call(`accounts/${state.accountId}/overview`);
    renderOverview();
    if (state.view === 'insights') renderInsights();
  } catch (err) {
    toast(err.message, 'error', 'Could not read the account');
  }
}

async function loadPositions() {
  if (!state.accountId) { state.positions = { rows: [], total: 0, offset: 0, limit: 100, symbols: [] }; renderTrades(); return; }
  const p = state.positions;
  const query = new URLSearchParams({
    limit: String(p.limit), offset: String(p.offset),
    search: state.filters.search, symbol: state.filters.symbol,
    outcome: state.filters.outcome,
  });
  try {
    const data = await call(`accounts/${state.accountId}/positions?${query}`);
    state.positions = { ...data, limit: p.limit };
    renderTrades();
  } catch (err) {
    toast(err.message, 'error');
  }
}

function onAccountChange() {
  const value = $('#account-select').value;
  state.accountId = value ? Number(value) : null;
  const account = state.accounts.find((a) => a.id === state.accountId);
  state.currency = account ? (account.currency || '') : '';
  state.positions.offset = 0;
  loadAccount();
  if (state.view === 'trades') loadPositions();
}

function populateRunTerminals() {
  const select = $('#run-terminal');
  const enabled = state.terminals.filter((t) => t.enabled);
  select.replaceChildren(...(enabled.length
    ? enabled.map((t) => el('option', {
        value: String(t.id), text: t.name || t.exe_path,
      }))
    : [el('option', { value: '', text: 'no terminal available' })]));
  // Prefer the terminal the selected account belongs to.
  const account = state.accounts.find((a) => a.id === state.accountId);
  if (account && enabled.some((t) => t.id === account.terminal_id)) {
    select.value = String(account.terminal_id);
  }
  $('#btn-run-go').disabled = !enabled.length;
}

/** Debounce, so typing in the search box does not fire a request per keystroke. */
function debounce(fn, wait = 260) {
  let handle;
  return (...args) => { clearTimeout(handle); handle = setTimeout(() => fn(...args), wait); };
}

function wire() {
  strip.init();

  $$('.rail-btn[data-view]').forEach((btn) =>
    btn.addEventListener('click', () => showView(btn.dataset.view)));
  $('[data-action="open-charts"]').addEventListener('click', () => reveal(state.app.charts_dir));
  $('[data-action="open-exports"]').addEventListener('click', () => reveal(state.app.exports_dir));

  $('#account-select').addEventListener('change', onAccountChange);
  $('#btn-refresh').addEventListener('click', async () => {
    await loadState();
    await loadAccount();
    if (state.view === 'trades') await loadPositions();
    toast('Reloaded from the database.', 'good');
  });

  // -- run dialog
  const modal = $('#run-modal');
  const openRun = () => {
    populateRunTerminals();
    modal.hidden = false;
  };
  $('#btn-run').addEventListener('click', openRun);
  document.body.addEventListener('click', (event) => {
    const action = event.target.closest('[data-action]');
    if (action && action.dataset.action === 'run') openRun();
    if (action && action.dataset.action === 'goto-terminals') showView('terminals');
    if (event.target.closest('[data-close]')) {
      modal.hidden = true;
      $('#drawer').hidden = true;
    }
    // Clicking the scrim, but not the card, dismisses.
    if (event.target === modal) modal.hidden = true;
    if (event.target === $('#drawer')) $('#drawer').hidden = true;
  });

  $('#btn-run-go').addEventListener('click', async () => {
    const stages = $$('#run-stages input:checked').map((i) => i.value);
    if (!stages.length) { toast('Pick at least one stage.', 'warn'); return; }
    const limit = $('#run-limit').value;
    modal.hidden = true;
    await submit('pipeline', {
      terminal_id: Number($('#run-terminal').value),
      account_id: state.accountId,
      stages,
      only_pending: $('#run-scope').value === 'pending',
      limit: limit ? Number(limit) : null,
      open_when_done: $('#run-open').checked,
    }, 'Full run');
  });

  // -- terminals
  $('#btn-scan').addEventListener('click', () => submit('terminals/scan', { deep: true }, 'Scan'));
  $('#btn-add').addEventListener('click', async () => {
    let chosen = null;
    try {
      const picked = await call('pick-folder', { body: {} });
      chosen = picked.path;
      if (!chosen && picked.unavailable) {
        chosen = window.prompt('Path to terminal64.exe or its folder:');
      }
    } catch {
      chosen = window.prompt('Path to terminal64.exe or its folder:');
    }
    if (!chosen) return;
    try {
      const out = await call('terminals/add', { body: { path: chosen } });
      state.terminals = out.terminals;
      renderTerminals();
      populateRunTerminals();
      toast('Terminal added. Press Connect to log it in.', 'good');
    } catch (err) { toast(err.message, 'error', 'Could not add that folder'); }
  });

  // -- trades
  const refilter = debounce(() => { state.positions.offset = 0; loadPositions(); });
  $('#f-search').addEventListener('input', (e) => { state.filters.search = e.target.value; refilter(); });
  $('#f-symbol').addEventListener('change', (e) => { state.filters.symbol = e.target.value; refilter(); });
  $('#f-outcome').addEventListener('change', (e) => { state.filters.outcome = e.target.value; refilter(); });
  $('#t-prev').addEventListener('click', () => {
    state.positions.offset = Math.max(0, state.positions.offset - state.positions.limit);
    loadPositions();
  });
  $('#t-next').addEventListener('click', () => {
    state.positions.offset += state.positions.limit;
    loadPositions();
  });

  // -- insights
  $$('#ins-tabs .tab').forEach((tab) => tab.addEventListener('click', () => {
    $$('#ins-tabs .tab').forEach((t) => t.classList.toggle('is-active', t === tab));
    state.insightGroup = tab.dataset.group;
    renderInsights();
  }));

  // -- drawer
  $('#d-folder').addEventListener('click', () => reveal(state.drawer.detail?.folder));
  $('#d-note-save').addEventListener('click', async () => {
    const detail = state.drawer.detail;
    if (!detail) return;
    try {
      await call(`positions/${detail.position.id}/note`, { body: { note: $('#d-note').value } });
      detail.position.note = $('#d-note').value;
      $('#d-note-status').textContent = 'Saved.';
    } catch (err) { toast(err.message, 'error'); }
  });

  // -- settings
  $('#btn-set-save').addEventListener('click', async () => {
    try {
      const out = await call('settings', { method: 'PATCH', body: collectSettings() });
      state.settings = out.settings;
      // Appearance is applied from what the server accepted, not from what the
      // form said, so a value the API rejected or clamped never leaves the
      // window looking like it was saved.
      applyAppearance(out.settings.ui);
      // Re-render from the stored values: a number the server clamped has to
      // show its clamped self, or the field keeps claiming a value that is not
      // what any run will use.
      renderSettings();
      const bad = out.rejected || [];
      if (bad.length) {
        $('#set-status').textContent = 'Saved, except ' + bad.join(', ') + '.';
        toast(bad.join(', ') + ' — left unchanged, that value is not allowed.',
          'warn', 'Saved the rest');
      } else {
        $('#set-status').textContent = 'Saved. New runs use these values.';
        toast('Settings saved.', 'good');
      }
    } catch (err) { toast(err.message, 'error'); }
  });
  $('#btn-set-reset').addEventListener('click', async () => {
    if (!window.confirm('Restore every setting to its default?')) return;
    try {
      const out = await call('settings/reset', { body: {} });
      state.settings = out.settings;
      applyAppearance(out.settings.ui);
      renderSettings();
      toast('Defaults restored.', 'good');
    } catch (err) { toast(err.message, 'error'); }
  });

  // -- keyboard
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      $('#run-modal').hidden = true;
      $('#drawer').hidden = true;
    }
    if (!$('#drawer').hidden && state.drawer.detail) {
      const shots = state.drawer.detail.shots;
      if (event.key === 'ArrowRight' && state.drawer.index < shots.length - 1) {
        state.drawer.index += 1; renderDrawer();
      }
      if (event.key === 'ArrowLeft' && state.drawer.index > 0) {
        state.drawer.index -= 1; renderDrawer();
      }
    }
    // Ctrl+R runs, which is what the muscle memory of a trader expects from a
    // window with a big Run button. The browser's own reload is Ctrl+Shift+R.
    if (event.key === 'r' && (event.ctrlKey || event.metaKey) && !event.shiftKey) {
      event.preventDefault();
      openRun();
    }
  });
}

(async function boot() {
  wire();
  try {
    await loadState();
    await loadAccount();
    showView('overview');
    if (!state.terminals.length && state.settings.ui?.auto_scan_on_start) {
      // First launch. Finding the terminals unprompted is the difference
      // between an app that works and one that asks the user to go hunting.
      toast('Looking for MetaTrader 5 on this PC…');
      submit('terminals/scan', { deep: true }, 'Scan');
    }
  } catch (err) {
    toast(err.message, 'error', 'Could not start');
  }
})();
