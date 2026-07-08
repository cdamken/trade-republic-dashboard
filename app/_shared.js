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

// ----------------------------------------------------------------------
// HTML escaping — for interpolating broker/user-supplied strings
// (instrument names, ISINs, notes, account nicknames, ids, event types)
// into innerHTML builders. Prevents self-XSS. Numbers we format
// ourselves (fmtEUR etc.) do NOT need escaping.
// ----------------------------------------------------------------------
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}

const monthKey = (iso) => (iso || '').slice(0, 7);

function monthLabel(k) {
  if (!k) return '';
  const [y, m] = k.split('-');
  const names = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  // Clamp to 0..11 — a malformed key like "2026-13" would otherwise
  // index past the array and render "undefined 2026".
  const idx = Math.max(0, Math.min(11, parseInt(m, 10) - 1));
  return `${names[idx]} ${y}`;
}

// ----------------------------------------------------------------------
// CSV parser for the TR account_transactions.csv shape:
// Date;Type;Value;Note;ISIN;Shares;Fees;Taxes;ISIN2;Shares2
// Returns array of row objects.
//
// Quote-aware: Python's csv writer (QUOTE_MINIMAL, delimiter=';')
// wraps any field containing `;` or `"` in double quotes, escaping
// embedded quotes by doubling. A note like `Dividend; gross €1000`
// arrives as `"Dividend; gross €1000"` — a naive split(';') would
// shear that row apart. This parser walks the line char-by-char and
// only splits on unquoted semicolons.
// ----------------------------------------------------------------------
function splitCsvLine(line) {
  const fields = [];
  let cur = '';
  let inQuotes = false;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (inQuotes) {
      if (c === '"') {
        if (line[i + 1] === '"') { cur += '"'; i++; }  // escaped quote
        else inQuotes = false;
      } else {
        cur += c;
      }
    } else if (c === '"' && cur === '') {
      inQuotes = true;
    } else if (c === ';') {
      fields.push(cur);
      cur = '';
    } else {
      cur += c;
    }
  }
  fields.push(cur);
  return fields;
}

function parseCsv(text) {
  const lines = text.replace(/\r\n/g, '\n').split('\n').filter(Boolean);
  if (lines.length === 0) return [];
  const header = splitCsvLine(lines[0]);
  const out = [];
  for (let i = 1; i < lines.length; i++) {
    const fields = splitCsvLine(lines[i]);
    if (fields.length < header.length) continue;
    const row = {};
    for (let j = 0; j < header.length; j++) row[header[j]] = fields[j];
    out.push(row);
  }
  return out;
}
