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

- [ ] **shared.js para TR (refactor parcial cumplido)** — `_update_flow.js`
  ya cumple el rol de "shared chrome" para las 4 páginas secundarias
  (modales, toast, progress bar, staleness chip, update button wiring).
  El index.html sigue con su propia copia inline. Para unificar al 100%
  habría que portar también index.html a `_update_flow.js`. Bajo
  prioridad — el patrón actual funciona. ~1h si se decide hacer.

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

- [ ] **Cross-validation del total** con un segundo endpoint si TR
  expone algo equivalente al `investments_groups` de GBM. Da confianza
  de que el número del dashboard coincide con la app móvil oficial.

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

- [ ] **Auto-refresh silencioso de sesión** — si tr-api expone un refresh
  token (similar al de Cognito en GBM), implementar el mismo patrón:
  detectar sesión expirada antes de cada call → refresh silencioso →
  caer al modal MFA solo si el refresh falla. Reduciría el MFA de
  "cada N horas" a "cuando expire el refresh token".

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
