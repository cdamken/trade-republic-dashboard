// =============================================================================
// Trade Republic Portfolio Extractor (v2)
// =============================================================================
// USO:
//   1. Abrir https://app.traderepublic.com/portfolio en Chrome
//   2. Esperar 5-10s a que los precios carguen
//   3. Abrir DevTools (Cmd+Opt+I) → Console
//   4. Pegar este script completo y presionar Enter
//   5. Copiar el resultado: copy(window.tr_data)  → pegar en archivo .json
//
// LIMITACIONES CONOCIDAS:
//   - La página de TR es lenta y a veces los precios aparecen como €0.00
//   - El script auto-scrollea para forzar carga, pero no garantiza 100%
//   - Si ves muchos "0.00 €" en posiciones grandes (JPM, AGNC, etc.),
//     refresca la página, espera 30s y vuelve a correr
//   - Para datos 100% confiables, usar el PDF "Account Statement" mensual
// =============================================================================

(async function() {
  console.log('🔄 Starting TR extraction... auto-scrolling first...');

  // Step 1: Auto-scroll to force lazy-loaded prices
  const scrollContainer = document.scrollingElement || document.body;
  for (let s = 0; s <= scrollContainer.scrollHeight; s += 800) {
    window.scrollTo(0, s);
    await new Promise(r => setTimeout(r, 200));
  }
  window.scrollTo(0, 0);
  await new Promise(r => setTimeout(r, 1000));
  console.log('✅ Scroll done. Parsing...');

  // Step 2: Parse text
  const text = document.body.innerText;
  const lines = text.split('\n').map(l => l.trim()).filter(l => l.length > 0);

  const startIdx = lines.findIndex(l => l === 'Investments');
  if (startIdx === -1) {
    console.error('❌ "Investments" section not found. Wrong page?');
    return null;
  }
  const endIdx = lines.findIndex((l, i) => i > startIdx &&
    (l === 'Following' || l === 'Favorites' || l === 'Discover'));
  const portfolioLines = lines.slice(startIdx + 1, endIdx > 0 ? endIdx : lines.length);

  const positions = [];
  let i = portfolioLines[0] === 'Since buy' ? 1 : 0;

  const isEUR = (s) => s && /€/.test(s);
  const isPct = (s) => s && (/%/.test(s) || s === '— %');

  while (i < portfolioLines.length) {
    const a = portfolioLines[i];
    const b = portfolioLines[i + 1];
    const c = portfolioLines[i + 2];
    const d = portfolioLines[i + 3];

    if (b && c && isEUR(c) && d && isPct(d)) {
      positions.push({ name: a, quantity: b, value_eur: c, pl_since_buy: d });
      i += 4;
      continue;
    }
    if (b && isEUR(b) && c && isPct(c)) {
      positions.push({ name: a, quantity: null, value_eur: b, pl_since_buy: c });
      i += 3;
      continue;
    }
    i++;
  }

  // Numeric helpers
  const parseEUR = (s) => parseFloat(String(s).replace(/[€,\s+]/g, ''));
  const parsePct = (s) => {
    if (!s || s === '— %') return null;
    const sign = s.includes('-') ? -1 : 1;
    const n = parseFloat(s.replace(/[%\s+\-]/g, ''));
    return isNaN(n) ? null : sign * n;
  };

  const enriched = positions.map(p => ({
    ...p,
    value_num: parseEUR(p.value_eur),
    pl_pct_num: parsePct(p.pl_since_buy),
  }));

  // Quality check: detect if prices haven't loaded
  const knownLargePositions = [
    'AGNC Investment', 'IREN', 'UiPath', 'BYD', 'Visa', 'Mastercard',
    'Meta Platforms', 'Lenovo Group', 'Krka', 'JPM', 'Nasdaq Equity', 'Global Equity'
  ];
  const dataQualityIssues = enriched.filter(p =>
    p.value_num === 0 &&
    knownLargePositions.some(k => p.name.includes(k))
  );

  // Aggregations
  const totalValue = enriched.reduce((s, p) => s + (p.value_num || 0), 0);
  const sortedByValue = [...enriched].sort((a, b) => b.value_num - a.value_num);

  const buckets = {
    over_1000: enriched.filter(p => p.value_num > 1000),
    range_500_1000: enriched.filter(p => p.value_num >= 500 && p.value_num <= 1000),
    range_100_500: enriched.filter(p => p.value_num >= 100 && p.value_num < 500),
    range_20_100: enriched.filter(p => p.value_num >= 20 && p.value_num < 100),
    under_20: enriched.filter(p => p.value_num > 0 && p.value_num < 20),
    zero_value: enriched.filter(p => p.value_num === 0),
  };

  const winners = enriched
    .filter(p => p.pl_pct_num != null && p.pl_pct_num >= 50 && /\+/.test(p.pl_since_buy))
    .sort((a, b) => b.pl_pct_num - a.pl_pct_num);
  const losers = enriched
    .filter(p => p.pl_pct_num != null && p.pl_pct_num >= 25 && !/\+/.test(p.pl_since_buy))
    .sort((a, b) => b.pl_pct_num - a.pl_pct_num);

  const result = {
    timestamp: new Date().toISOString(),
    url: window.location.href,
    quality: {
      is_data_complete: dataQualityIssues.length === 0,
      missing_prices: dataQualityIssues.map(p => p.name),
      warning: dataQualityIssues.length > 0
        ? `⚠️ ${dataQualityIssues.length} known-large positions show €0. Refresh page, wait 30s, retry.`
        : '✅ All checks passed',
    },
    summary: {
      total_positions: enriched.length,
      total_value_eur: totalValue.toFixed(2),
      bucket_counts: {
        over_1000: buckets.over_1000.length,
        range_500_1000: buckets.range_500_1000.length,
        range_100_500: buckets.range_100_500.length,
        range_20_100: buckets.range_20_100.length,
        under_20: buckets.under_20.length,
        zero_value: buckets.zero_value.length,
      },
    },
    top_25: sortedByValue.slice(0, 25),
    winners_50_plus: winners,
    losers_25_minus: losers,
    zero_value_positions: buckets.zero_value,
    all_positions: enriched,
  };

  window.tr_data = result;
  console.log('✅ Extraction complete:', result.summary);
  console.log(result.quality.warning);
  console.log('💡 copy(window.tr_data)  to copy full JSON');
  console.log('💡 console.table(window.tr_data.top_25)  to view table');
  return result;
})();
