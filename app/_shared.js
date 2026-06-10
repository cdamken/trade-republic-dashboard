// Shared helpers for the newer TR dashboard pages (orders.html, ledger.html
// — the ones added 2026-06-02). The older pages (index, analytics, dividends,
// settings, glossary) each have their own inline fmt* helpers with slightly
// different signatures (fmtEur vs fmtEUR, single-arg vs (n, opts)) and we
// leave them alone to avoid a risky multi-file refactor.
//
// New pages should load this BEFORE their inline <script>:
//
//   <script src="_shared.js"></script>
//   <script>... rest of page logic ...</script>
//
// Common chrome (modal, toast, staleness chip, update button wiring) still
// lives in _update_flow.js — _shared.js is just data/text helpers.

// ----------------------------------------------------------------------
// Number / currency formatters
// ----------------------------------------------------------------------
function fmtEUR(n) {
  return '€' + (Number(n) || 0).toLocaleString('en-US', {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  });
}

// Sign-aware EUR: "+€1.23" / "−€1.23" / "+€0.00" — explicit sign on
// BOTH positives and negatives. For deltas / net cash flows where the
// sign is the headline information.
//
// Uses unicode minus (U+2212). Fixed 2026-06-10 (was returning
// negatives WITHOUT any sign, relying on `.red` CSS class to
// communicate negativity — colour-blind-hostile + misleading on
// copy-paste).
function fmtSignedEUR(n) {
  const v = Number(n) || 0;
  if (v < 0) return '−' + fmtEUR(Math.abs(v));
  return '+' + fmtEUR(v);
}

// EUR with minus on negatives but NO sign on positives: "€1.23" /
// "−€1.23" / "€0.00". For values that are conventionally positive
// (dividend totals, balances) where a "+" prefix would be visual
// noise but a missing "−" on a refund would be wrong.
function fmtEURWithMinus(n) {
  const v = Number(n) || 0;
  if (v < 0) return '−' + fmtEUR(Math.abs(v));
  return fmtEUR(v);
}

// ----------------------------------------------------------------------
// Date / month helpers
// ----------------------------------------------------------------------
function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleString('en-US', {
    year: 'numeric', month: 'short', day: '2-digit',
    hour: '2-digit', minute: '2-digit',
  });
}

const monthKey = (iso) => (iso || '').slice(0, 7);

function monthLabel(k) {
  if (!k) return '';
  const [y, m] = k.split('-');
  const names = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  return `${names[parseInt(m, 10) - 1]} ${y}`;
}

// ----------------------------------------------------------------------
// CSV parser tolerant to the TR account_transactions.csv shape:
// Date;Type;Value;Note;ISIN;Shares;Fees;Taxes;ISIN2;Shares2
// Returns array of row objects.
// ----------------------------------------------------------------------
function parseCsv(text) {
  const lines = text.replace(/\r\n/g, '\n').split('\n').filter(Boolean);
  if (lines.length === 0) return [];
  const header = lines[0].split(';');
  const out = [];
  for (let i = 1; i < lines.length; i++) {
    const fields = lines[i].split(';');
    if (fields.length < header.length) continue;
    const row = {};
    for (let j = 0; j < header.length; j++) row[header[j]] = fields[j];
    out.push(row);
  }
  return out;
}
