# Backlog — Trade-Republic-Dashboard

Cosas pendientes / ideas para futuras sesiones. Edita libremente.
Items con `[ ]` están pendientes; `[x]` cuando se completen.

> Workflow: cambios visuales/funcionales aterrizar acá primero, después
> portar a `Trade-Republic-owncloud` (ver CLAUDE.md).

Casi todo viene de la comparación con `gbm-dashboard` (sesión 2026-06-02).
El orden dentro de cada sección es por impacto estimado.

---

## 1. UX / visual

- [ ] **Brand-adjacent dark theme (opción A)** — ajustar acentos para
  sentirse más "oficial" como traderepublic.com sin abandonar el dark
  theme. Cambios:
  - Refinar pesos tipográficos para un look más minimal (menos heavy
    en el cockpit, más respiración entre KPIs)
  - Logo box: probar el patrón blanco-con-bandera-DE pequeña que TR usa
  - Mantener paleta funcional (verde/rojo P&L). Solo paleta de acento +
    tipografía. ~1h.

- [x] **Staleness chip con color por antigüedad** — implementado 2026-06-02
  en las 5 páginas. Verde ≤15min, ámbar ≤1h, rojo >1h.
  - `index.html`: chip inline en el subtitle (su propia copia del helper).
  - `analytics.html`, `dividends.html`, `settings.html`, `glossary.html`:
    chip se inyecta automáticamente al top-bar `.actions` desde
    `_update_flow.js::injectStalenessChip` (justo antes del Update btn).
  - Necesitó bumpear `tr_fetch.py:905` para escribir timestamp completo
    (era solo fecha). El chip se activa después del primer ⟳ Update Now
    post-cambio.

- [x] **shared.js para TR (parcial — formatters)** — implementado
  2026-06-02. Nuevo `app/_shared.js` con `fmtEUR`, `fmtSignedEUR`,
  `fmtDate`, `monthKey`, `monthLabel`, `parseCsv`. Las nuevas páginas
  (orders.html, ledger.html) lo loadean. Las páginas viejas (index,
  analytics, dividends, settings, glossary) mantienen sus copias
  inline porque tienen firmas ligeramente diferentes (`fmtEur` vs
  `fmtEUR`, single-arg vs (n, opts)). Migrarlas requiere normalizar
  callsites — bajo prioridad. `_update_flow.js` ya hace el rol de
  shared chrome (modales, toast, progress, chip).

- [x] **Rotating progress messages** — ya existía. `_update_flow.js`
  define `STAGES_NORMAL` y `STAGES_FULL` que rotan en un `setInterval`
  cada 500ms según tiempo transcurrido. Se renderizan dentro del toast
  (no overlay full-screen como GBM, pero el texto sí cambia).
  Comparación inicial fue imprecisa.

- [x] **Decimales adaptativos por instrumento** — implementado 2026-06-02.
  Nuevo `fmtQty(qty, category)` en `index.html`: cryptos → 6-8 decimales;
  whole-share stocks/ETFs → 0 decimales ("12" no "12.0000"); fractional
  (savings plan units) → 2-4 decimales. Aplicado a las 2 tablas + position
  modal.

## 2. Features (puerto desde GBM)

- [ ] **Per-page CSV export** (idea portada desde Scalable-Capital-Dashboard,
  sesión 2026-06-06). Botón "↓ Export CSV" en los controles de cada página:
  - `/export/orders.csv` — date, side, type, isin, security, quantity, amount_eur, status
  - `/export/ledger.csv` — date, type, description, related_isin, amount_eur, currency, status
  - `/export/dividends.csv` — date, security, isin, amount_eur, currency, status
  - `/export/holdings.csv` — group, name, isin, wkn, type, quantities, fifo_price, current_price, value_eur

  GBM ya tiene un CSV agregado (`/export/transactions.csv` para SAT) que
  es excelente para impuestos pero menos util para análisis ad-hoc. La
  variante per-página resuelve el caso "quiero pegar mis órdenes en una
  spreadsheet sin pelear con 13 columnas". Implementación en
  `Scalable-Capital-Dashboard/app/server.py::_handle_export()` —
  copy verbatim adaptando los campos al schema TR (eventType /
  cashTransactionType en TR son distintos a Scalable).


- [x] **Página Órdenes dedicada** — implementado 2026-06-02 en
  `/app/orders.html`. Tabla con filtros por side (Buy/Sell), búsqueda
  por security/ISIN, filtro de mes. Cards: total trades, total
  bought (€), total sold (€), net flow. Parsea `account_transactions.csv`
  client-side (Buy/Sell rows only). El link "📋 Orders" se inyectó al
  nav de las 5 páginas existentes.
  **Nota**: TR no tiene status filter (no expone órdenes
  canceladas/pending al cliente personal, todas las del CSV están filled).

- [x] **Vista Ledger separada con categorías** — implementado 2026-06-02
  en `/app/ledger.html`. Tabla de los 6000+ rows del CSV con pills de
  color por categoría (Buy/Sell/Dividend/Interest/Deposit/Withdrawal/
  Removal/Tax Refund). Filtros: búsqueda, categoría, mes, page size
  (200/500/1000/All). Cards summary: total events, cash flow neto
  (deposits − withdrawals − removals + tax refund), dividend+interest,
  card spending. Link "📒 Ledger" insertado al nav de las 6 páginas
  existentes (orden: Portfolio → Analytics → Orders → Ledger →
  Dividends → Settings → Glossary).

- [x] **Cross-validation del total** — investigado 2026-06-02 leyendo
  `tr-api/src/tr_api/portfolio.py`. TR **no expone** un endpoint
  "autoritativo" separado equivalente al `investments_groups` de GBM.
  El total siempre se computa localmente de `compactPortfolio.positions`
  (suma de `netSize × currentPrice`) + `cash`. Los topics disponibles
  son: `compactPortfolio`, `compactPortfolioByType`, `cash`,
  `availableCashForPayout`. La única "validación" posible es comparar
  manualmente con la app móvil de TR (misma backend, debería coincidir
  por construcción). Cerrado — no hay nada para implementar.

- [x] **Segmentación fina de mercado** — implementado 2026-06-02 en
  analytics.html. Nuevo chart horizontal "Geographic allocation
  (ISIN domicile)" después del top grid. Mapea el prefijo ISIN[:2] a
  país (US/DE/FR/IE/LU/...). Top 12 + "Other (N countries)" bucket
  para evitar abarrotar. Caveat documentado en el substat: es por
  domicilio del emisor, no por revenue exposure (un US tracker UCITS
  domiciliado en IE cuenta como Ireland aunque invierta en US).

## 3. Seguridad / robustez (port verbatim de GBM)

- [x] **CSRF check en POST endpoints** — implementado 2026-06-02.
  Aplica a `/update`, `/setup`, `/reset`, `/download_docs`, `/settings`.
  Ver `server.py::_ALLOWED_ORIGINS` + `do_POST`.

- [ ] **Bindear server.py a 127.0.0.1 solamente** — actualmente bindea
  a `""` (0.0.0.0, todas las interfaces). Cualquiera en tu mismo Wi-Fi
  puede hacer requests directos al puerto 8085 con curl/wget sin Origin
  header (que el CSRF check permite). Cambiar `ThreadedServer(("", PORT))`
  → `ThreadedServer(("127.0.0.1", PORT))` en server.py:757.
  **Cuidado**: si usas el dashboard desde tu teléfono en LAN, este cambio
  te lo rompe. Confirmar con Carlos antes de hacer.

- [x] **Auto-refresh silencioso de sesión** — investigado 2026-06-02.
  TR-Dashboard YA tiene auto-refresh, solo que con un modelo distinto
  al de GBM:
  - GBM (Cognito): on-demand — `GbmClient.from_saved()` detecta
    `is_expired` y refresca antes de la request si tiene refresh_token.
  - TR: proactivo — `server.py::_session_keepalive_loop` corre como
    daemon thread y llama `tr-api auth refresh` cada 290s (justo bajo
    el TTL de cookies de TR de ~5 min). `tr_api/auth.py::refresh_session`
    hace el GET `/api/v1/auth/web/session` que rota `JSESSIONID` +
    `tr_session` server-side.
  Cookies persisten en `~/.tr-api/profiles/<phone>/cookies.txt`. El
  MFA solo se pide cuando las cookies mueren del todo (servidor TR
  las invalida, o el keepalive falla por horas). Cerrado — funciona.

- [x] **Endpoint /reset** — ya existía con UX más estricta que el de
  GBM. Botón "🗑 Wipe data + credentials" en settings.html requiere
  doble confirmación (confirm + prompt "delete"). Wipea credenciales,
  cookies y DATA — fuerza re-setup completo. GBM en cambio solo wipea
  sesión + DATA (mantiene credenciales). Comportamientos diferentes
  pero ambos son correctos para su flujo.

## 4. UX confirmaciones

- [x] **Top-bar `position: sticky`** — ya configurado. Si las tabs
  parecen moverse, lo que se va es el cockpit (los KPIs grandes) —
  pérdida de sticky intencional para no comer el viewport. Top-bar SÍ
  se queda.

- [ ] **Verificar visualmente que el sticky engage** — pendiente abrir
  en navegador y hacer scroll real para confirmar que no hay otro
  problema. Si igual se ve mal, considerar `position: fixed` en lugar
  de sticky.

---

*Última actualización: 2026-06-02. Ver también `BACKLOG.md` en
`gbm-dashboard/` para el sibling repo.*
