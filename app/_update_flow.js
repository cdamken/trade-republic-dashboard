/**
 * Shared "🔄 Update Now" flow for the Dashboard local — loaded from
 * analytics.html / dividends.html / settings.html / glossary.html so the
 * button triggers the update IN PLACE instead of bouncing the user to
 * Portfolio first.
 *
 * Mirrors index.html's inline updateData() / submitMfa() / postUpdate() /
 * showProgressOverlay() helpers. On Portfolio (index.html) this module is
 * NOT loaded — index.html keeps its inline version (no behavioural change
 * there).
 *
 * Self-contained:
 *   1) Injects the MFA modal + toast + progress-bar markup into the page
 *      (those pages don't already have it).
 *   2) Wires `#update-btn` to updateData().
 *
 * Endpoints (Dashboard local): /update, no CSRF token needed (single-user
 * stdlib HTTP server, no auth).
 */
(function () {
'use strict';
if (window.__TR_UpdateFlow) return;
window.__TR_UpdateFlow = true;

const MODAL_HTML = ''
  + '<div id="progress-bar" class="progress-bar"></div>'
  + '<div id="toast" class="toast">'
  + '  <button class="t-close" id="toast-close-btn" aria-label="Close">×</button>'
  + '  <div class="t-title"><span class="spin"></span> <span id="toast-title">Updating information…</span></div>'
  + '  <div class="t-stage" id="toast-stage">Connecting…</div>'
  + '</div>'
  + '<div id="mfa-modal" class="modal-backdrop">'
  + '  <div class="modal">'
  + '    <h3>🔐 Trade Republic Security Code</h3>'
  + '    <p>Your session expired. Trade Republic needs to verify it\'s you.</p>'
  + '    <div class="hint">'
  + '      📱 <strong>Open the Trade Republic app</strong> on your phone — TR just pushed a 4-digit code.<br>'
  + '      ⏱ The code expires in ~60 seconds.'
  + '    </div>'
  + '    <input type="text" id="mfa-input" inputmode="numeric" pattern="[0-9]*" maxlength="4"'
  + '           autocomplete="one-time-code"'
  + '           data-lpignore="true" data-1p-ignore data-bwignore placeholder="0000">'
  + '    <div id="mfa-err" class="err-msg"></div>'
  + '    <label for="mfa-full-reload"'
  + '           style="display:flex; align-items:flex-start; gap:10px; cursor:pointer;'
  + '                  background:rgba(255,255,255,0.03); border:1px solid var(--border);'
  + '                  border-radius:10px; padding:12px 14px; margin-top:14px; margin-bottom:6px;'
  + '                  font-size:13px; color:var(--muted); line-height:1.45;">'
  + '      <input type="checkbox" id="mfa-full-reload"'
  + '             style="margin-top:2px; width:18px; height:18px; accent-color:#3b82f6; flex-shrink:0;">'
  + '      <span>'
  + '        <strong style="color:var(--text);">↻ Full Reload</strong> — wipe the local cache and re-download everything.<br>'
  + '        <span style="opacity:.8;">Use this if numbers look off. Takes ~1–3 min.</span>'
  + '      </span>'
  + '    </label>'
  + '    <div class="modal-actions">'
  + '      <button id="mfa-cancel-btn" class="btn-cancel">Cancel</button>'
  + '      <button id="mfa-submit-btn" class="btn-submit">Submit</button>'
  + '    </div>'
  + '  </div>'
  + '</div>';

// Minimal CSS for the injected MFA modal + toast + progress-bar.
// Mirrors the rules in index.html (which has them inline). The 4 secondary
// pages don't include those styles, so we inject them here.
const STYLES = ''
  + '@keyframes _uf_spin { to { transform: rotate(360deg); } }'
  + '@keyframes _uf_pb_slide { 0% { background-position: -50% 0; } 100% { background-position: 150% 0; } }'
  + '.spin { display:inline-block; width:12px; height:12px; border:2px solid currentColor;'
  + '  border-right-color:transparent; border-radius:50%; animation:_uf_spin 0.7s linear infinite;'
  + '  vertical-align:-1px; }'
  + '.progress-bar { position:fixed; top:0; left:0; right:0; height:2px; z-index:100;'
  + '  pointer-events:none; display:none; }'
  + '.progress-bar.active { display:block; }'
  + '.progress-bar.indet { background:linear-gradient(90deg,transparent 0%,var(--blue) 50%,transparent 100%);'
  + '  background-size:50% 100%; background-repeat:no-repeat;'
  + '  animation:_uf_pb_slide 1.6s ease-in-out infinite; }'
  + '.toast { position:fixed; top:200px; left:50%; transform:translateX(-50%) translateY(-10px);'
  + '  background:var(--card); border:1px solid var(--border); border-top:3px solid var(--blue);'
  + '  padding:12px 20px; border-radius:0 0 10px 10px; min-width:320px; max-width:520px;'
  + '  box-shadow:0 12px 32px rgba(0,0,0,0.5); display:none; z-index:90;'
  + '  font-size:14px; text-align:center; opacity:0; transition:opacity 0.18s, transform 0.18s; }'
  + '.toast.active { display:block; opacity:1; transform:translateX(-50%) translateY(0); }'
  + '.toast .t-title { font-weight:700; margin-bottom:4px; display:flex; align-items:center;'
  + '  justify-content:center; gap:10px; }'
  + '.toast .t-stage { color:var(--muted); font-size:12px; }'
  + '.toast.ok { border-top-color:var(--green); }'
  + '.toast.err { border-top-color:var(--red); }'
  + '.toast .t-close { background:none; border:none; color:var(--muted); cursor:pointer;'
  + '  position:absolute; top:6px; right:10px; font-size:16px; padding:0; }'
  + '.modal-backdrop { position:fixed; inset:0; background:rgba(0,0,0,0.7); display:none;'
  + '  align-items:center; justify-content:center; z-index:100; backdrop-filter:blur(4px); }'
  + '.modal-backdrop.open { display:flex; }'
  + '.modal { background:var(--card); border:1px solid var(--border); border-radius:16px;'
  + '  padding:32px; width:100%; max-width:460px; box-shadow:0 20px 60px rgba(0,0,0,0.5); }'
  + '.modal h3 { font-size:20px; margin-bottom:12px; display:flex; align-items:center; gap:10px; }'
  + '.modal p { color:var(--muted); font-size:14px; margin-bottom:20px; line-height:1.6; }'
  + '.modal .hint { background:rgba(96,165,250,0.08); border-left:3px solid var(--blue);'
  + '  padding:12px 14px; border-radius:4px; font-size:13px; color:var(--text); margin-bottom:20px; }'
  + '.modal input[type="text"] { width:100%; background:var(--bg); border:2px solid var(--border);'
  + '  color:var(--text); padding:16px; font-size:28px; border-radius:10px; text-align:center;'
  + '  letter-spacing:12px; font-family:monospace; font-weight:700; margin-bottom:16px;'
  + '  transition:border-color 0.2s; }'
  + '.modal input[type="text"]:focus { outline:none; border-color:var(--blue); }'
  + '.modal .err-msg { color:var(--red); font-size:13px; margin-bottom:16px; display:none; }'
  + '.modal .err-msg.show { display:block; }'
  + '.modal-actions { display:flex; gap:10px; justify-content:flex-end; }'
  + '.modal button { padding:10px 22px; border-radius:8px; font-weight:600; font-size:14px;'
  + '  border:none; cursor:pointer; transition:all 0.2s; }'
  + '.btn-cancel { background:transparent; color:var(--muted); border:1px solid var(--border) !important; }'
  + '.btn-cancel:hover { color:var(--text); }'
  + '.btn-submit { background:var(--blue); color:var(--bg); }'
  + '.btn-submit:hover:not(:disabled) { background:#7ab3ff; }'
  + '.btn-submit:disabled { opacity:0.5; cursor:wait; }'
  // Staleness chip — fresh ≤15m / warn ≤1h / stale >1h. Ported from
  // gbm-dashboard 2026-06-02. Injected into the top-bar .actions on
  // every secondary page so the user can see "how old" the snapshot
  // is right next to the Update button without going back to Portfolio.
  + '.staleness-chip { display:none; padding:3px 9px; border-radius:10px;'
  + '  font-size:11px; font-weight:600; vertical-align:middle; }'
  + '.staleness-chip.show { display:inline-block; }'
  + '.staleness-chip.fresh { background:rgba(74,222,128,0.15); color:var(--green); }'
  + '.staleness-chip.warn  { background:rgba(251,191,36,0.18); color:var(--amber); }'
  + '.staleness-chip.stale { background:rgba(248,113,113,0.22); color:var(--red); }';

function injectStylesIfMissing() {
  if (document.getElementById('update-flow-styles')) return;
  // If a .modal-backdrop rule already exists (Portfolio page or future
  // template that already ships them), skip — keep the page's own styles
  // authoritative.
  const s = document.createElement('style');
  s.id = 'update-flow-styles';
  s.textContent = STYLES;
  document.head.appendChild(s);
}

function injectModalsIfMissing() {
  if (document.getElementById('mfa-modal')) return;
  const holder = document.createElement('div');
  holder.id = 'update-flow-injected';
  holder.innerHTML = MODAL_HTML;
  document.body.appendChild(holder);
}

const $ = (id) => document.getElementById(id);
function setUpdateBtn(loading, label) {
  const b = $('update-btn');
  if (!b) return;
  b.disabled = loading;
  b.classList.toggle('loading', loading);
  const labelEl = b.querySelector('.label');
  if (labelEl) labelEl.textContent = label || 'Update Now';
  else b.textContent = '🔄 ' + (label || 'Update Now');
}
function showStatusToast(kind, msg) {
  const t = $('toast');
  if (!t) return;
  t.classList.remove('ok', 'err');
  if (kind) t.classList.add(kind);
  const title = $('toast-title');
  const stage = $('toast-stage');
  if (title) title.textContent = msg || '';
  if (stage) stage.textContent = '';
  t.classList.add('active');
  if (kind === 'ok') setTimeout(() => t.classList.remove('active'), 3000);
}

function showToast(stage, kind) {
  const t = $('toast'); if (!t) return;
  t.classList.remove('ok', 'err');
  if (kind) t.classList.add(kind);
  const stageEl = $('toast-stage');
  if (stageEl) stageEl.textContent = stage;
  t.classList.add('active');
}
function setToastTitle(title) {
  const el = $('toast-title'); if (el) el.textContent = title;
}
function hideToast() { const t = $('toast'); if (t) t.classList.remove('active'); }
function showProgressBar() { const b = $('progress-bar'); if (b) b.classList.add('active', 'indet'); }
function hideProgressBar() { const b = $('progress-bar'); if (b) b.classList.remove('active', 'indet'); }

const STAGES_NORMAL = [
  { until: 5,   text: 'Connecting to Trade Republic…' },
  { until: 15,  text: 'Verifying session…' },
  { until: 45,  text: 'Downloading portfolio and prices…' },
  { until: 90,  text: 'Resolving names and instruments…' },
  { until: 150, text: 'Downloading recent transactions…' },
  { until: Infinity, text: 'Almost done…' },
];
const STAGES_FULL = [
  { until: 5,   text: 'Connecting to Trade Republic…' },
  { until: 15,  text: 'Verifying session…' },
  { until: 45,  text: 'Downloading portfolio and prices…' },
  { until: 90,  text: 'Resolving names and instruments…' },
  { until: 240, text: 'Downloading the FULL transaction history…' },
  { until: Infinity, text: 'Almost done, thanks for the patience…' },
];
let _started = null, _timer = null;
function showProgressOverlay(opts) {
  const stages = (opts && opts.full) ? STAGES_FULL : STAGES_NORMAL;
  setToastTitle((opts && opts.full) ? 'Updating all information…' : 'Updating information…');
  showToast(stages[0].text);
  showProgressBar();
  _started = Date.now();
  _timer = setInterval(() => {
    const elapsed = (Date.now() - _started) / 1000;
    const stage = stages.find(s => elapsed < s.until) || stages[stages.length - 1];
    showToast(stage.text);
  }, 500);
}
function hideProgressOverlay() {
  if (_timer) { clearInterval(_timer); _timer = null; }
  _started = null;
  hideProgressBar();
  hideToast();
}

async function postUpdate(mfaCode, opts) {
  const body = {};
  if (mfaCode) body.mfa_code = mfaCode;
  if (opts && opts.full) body.full = true;
  const res = await fetch('/update', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  let payload = {};
  try { payload = await res.json(); } catch (e) { payload = {}; }
  return { http: res.status, state: payload.status, detail: payload.detail };
}

async function updateData() {
  setUpdateBtn(true, 'Updating…');
  let overlayShown = false;
  const overlayDelay = setTimeout(() => {
    showProgressOverlay({ full: false });
    overlayShown = true;
  }, 5500);
  const cleanupOverlay = () => {
    clearTimeout(overlayDelay);
    if (overlayShown) { hideProgressOverlay(); overlayShown = false; }
  };
  try {
    const r = await postUpdate(null);
    if (r.http === 200) {
      clearTimeout(overlayDelay);
      showStatusToast('ok', '✓ Updated — reloading');
      setTimeout(() => location.reload(), 800);
      return;
    }
    cleanupOverlay();
    if (r.state === 'mfa_required') { openMfaModal(); return; }
    if (r.state === 'rate_limited') {
      showStatusToast('err', '⚠ Rate-limited by Trade Republic — wait 15–30 min and retry');
      return;
    }
    showStatusToast('err', '✗ ' + (r.detail || r.state || ('HTTP ' + r.http)));
  } catch (e) {
    cleanupOverlay();
    showStatusToast('err', '✗ Network error');
  } finally {
    setUpdateBtn(false);
  }
}

function openMfaModal() {
  const m = $('mfa-modal'); if (!m) return;
  m.classList.add('open');
  const err = $('mfa-err'); if (err) err.classList.remove('show');
  const inp = $('mfa-input'); if (inp) inp.value = '';
  const cb = $('mfa-full-reload'); if (cb) cb.checked = false;
  setTimeout(() => { if (inp) inp.focus(); }, 100);
  setUpdateBtn(false);
}
function closeMfaModal() { const m = $('mfa-modal'); if (m) m.classList.remove('open'); }

async function submitMfa() {
  const code = $('mfa-input').value.trim();
  const errEl = $('mfa-err');
  errEl.classList.remove('show');
  if (!/^\d{4}$/.test(code)) {
    errEl.textContent = 'The code must be exactly 4 digits.';
    errEl.classList.add('show');
    return;
  }
  const submitBtn = $('mfa-submit-btn');
  submitBtn.disabled = true;
  submitBtn.textContent = 'Verifying…';
  const fullReload = !!($('mfa-full-reload') && $('mfa-full-reload').checked);
  setUpdateBtn(true, fullReload ? 'Re-downloading everything…' : 'Updating…');

  closeMfaModal();
  showProgressOverlay({ full: fullReload });

  try {
    const r = await postUpdate(code, { full: fullReload });
    if (r.http === 200) {
      showStatusToast('ok', '✓ Updated — reloading');
      setTimeout(() => location.reload(), 800);
      return;
    }
    hideProgressOverlay();
    if (r.state === 'mfa_invalid' || r.state === 'mfa_required') {
      openMfaModal();
      errEl.textContent = 'Wrong code. Check and try again.';
      errEl.classList.add('show');
      $('mfa-input').select();
    } else if (r.state === 'auth_failed') {
      openMfaModal();
      errEl.textContent = 'Invalid credentials. Check ~/.pytr/credentials.';
      errEl.classList.add('show');
    } else if (r.state === 'rate_limited') {
      openMfaModal();
      errEl.textContent = '⚠ Trade Republic rate-limited login. Wait 15–30 min and retry.';
      errEl.classList.add('show');
    } else {
      openMfaModal();
      errEl.textContent = r.detail || ('Error ' + r.http);
      errEl.classList.add('show');
    }
  } catch (e) {
    hideProgressOverlay();
    openMfaModal();
    errEl.textContent = 'Network error: ' + e.message;
    errEl.classList.add('show');
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = 'Submit';
    setUpdateBtn(false);
  }
}

// Returns {label, severity} for a "YYYY-MM-DD HH:MM:SS" timestamp, or
// null if unparseable. Severity: fresh ≤15min, warn ≤1h, stale >1h.
function stalenessHint(iso) {
  if (!iso) return null;
  const hasTz = /Z|[+-]\d{2}:?\d{2}$/.test(iso.trim());
  const parseable = hasTz ? iso.trim() : iso.trim().replace(' ', 'T');
  const d = new Date(parseable);
  if (isNaN(d.getTime())) return null;
  const mins = Math.floor((Date.now() - d.getTime()) / 60000);
  let label;
  if (mins < 1)       label = 'just now';
  else if (mins < 60) label = mins + ' min ago';
  else {
    const h = Math.floor(mins / 60);
    const m = mins % 60;
    label = m === 0 ? h + ' h ago' : h + ' h ' + m + ' min ago';
  }
  const severity = mins <= 15 ? 'fresh' : mins <= 60 ? 'warn' : 'stale';
  return { label, severity };
}

// Inject the staleness chip into the top-bar .actions on every page
// that loads _update_flow.js (the 4 secondary TR pages). Portfolio
// (index.html) has its own inline chip in the subtitle — this script
// doesn't load there, so no conflict.
async function injectStalenessChip() {
  const actions = document.querySelector('.top-bar .actions');
  if (!actions || document.getElementById('last-update-age')) return;
  const chip = document.createElement('span');
  chip.id = 'last-update-age';
  chip.className = 'staleness-chip';
  // Insert before the Update Now button so the chip reads "natural"
  // left-to-right: "5 min ago" → "🔄 Update Now".
  const updateBtn = $('update-btn');
  if (updateBtn) actions.insertBefore(chip, updateBtn);
  else actions.appendChild(chip);

  try {
    const r = await fetch('../DATA/last_update.date?t=' + Date.now());
    if (!r.ok) return;
    const ts = (await r.text()).trim();
    // Only render if the timestamp has hour granularity (post-2026-06-02
    // tr_fetch.py writes "YYYY-MM-DD HH:MM:SS"). The legacy date-only
    // format gets skipped — the chip stays hidden until the next Update.
    if (!/\d{4}-\d{2}-\d{2}[ T]\d/.test(ts)) return;
    const s = stalenessHint(ts);
    if (!s) return;
    chip.textContent = s.label;
    chip.className = 'staleness-chip show ' + s.severity;
    chip.title = 'Snapshot fetched ' + ts;
  } catch (_) { /* ignore — chip just stays hidden */ }
}

function init() {
  injectStylesIfMissing();
  injectModalsIfMissing();
  injectStalenessChip();

  const btn = $('update-btn');
  if (btn) btn.addEventListener('click', updateData);

  // Modal wiring
  const mfaInput  = $('mfa-input');
  if (mfaInput) {
    mfaInput.addEventListener('input', e => {
      e.target.value = e.target.value.replace(/[^0-9]/g, '');
    });
    mfaInput.addEventListener('keydown', e => { if (e.key === 'Enter') submitMfa(); });
  }
  const mfaCancel = $('mfa-cancel-btn'); if (mfaCancel) mfaCancel.addEventListener('click', closeMfaModal);
  const mfaSubmit = $('mfa-submit-btn'); if (mfaSubmit) mfaSubmit.addEventListener('click', submitMfa);
  const mfaBack   = $('mfa-modal');      if (mfaBack)   mfaBack.addEventListener('click', e => { if (e.target === mfaBack) closeMfaModal(); });
  const toastX    = $('toast-close-btn'); if (toastX)   toastX.addEventListener('click', hideToast);

  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    const m = $('mfa-modal');
    if (m && m.classList.contains('open')) closeMfaModal();
  });
}

// Expose for diagnostics
window.UpdateFlow = { updateData, openMfaModal };

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
})();
