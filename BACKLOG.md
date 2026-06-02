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

- [ ] **Rotating progress messages** durante el /update en lugar del
  toast estático. Patrón de GBM: "Conectando…" → "Descargando portafolio…"
  → "Descargando posiciones…" → "Ya casi…". Da sensación de progreso real
  vs spinner sin contexto.

- [ ] **Decimales adaptativos por instrumento** — crypto muestra 6+
  decimales, equity 2, fondos según haga falta. Hoy todo se formatea
  igual. GBM lo hace en sus tablas.

## 2. Features (puerto desde GBM)

- [ ] **Página Órdenes dedicada** — actualmente solo está el CSV combinado
  de transactions. Patrón GBM: tabla con filtros por estado
  (filled/cancelled/pending), side (buy/sell), ticker, mes. Útil para
  ver qué se canceló.

- [ ] **Vista Ledger separada con categorías** — buys, sells, FX,
  intereses, dividends, fees, depósitos, retiros — cada uno con su
  pill de color. Patrón del Libro Diario de GBM. Más rico que el CSV
  plano actual.

- [ ] **Cross-validation del total** con un segundo endpoint si TR
  expone algo equivalente al `investments_groups` de GBM. Da confianza
  de que el número del dashboard coincide con la app móvil oficial.

- [ ] **Segmentación fina de mercado** — hoy buckets son Equities/
  Bonds/Crypto/Cash/Private. GBM tiene segmentación geográfica/operativa
  más fina (BMV, SIC, extranjero, fondos deuda, fondos común). Para TR
  el equivalente sería región (DE / EU / US / EM) + tipo (Stock / ETF /
  Bond / Crypto / Cash). Más drill-down.

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

- [ ] **Endpoint /reset** — análogo al de GBM para borrar sesión local
  + DATA manualmente desde el UI (botón en Settings).

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
