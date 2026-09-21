/* ── CausaDB Dashboard — trades.js ──────────────────────────────
 *
 * Agrupado entry/exit de TRADE_EXECUTED por trade_id.
 *
 * NAMESPACE GLOBAL: CausaDBTrades = { groupTrades, TRADE_LIMIT }.
 * Este archivo es PURO: sin DOM, sin fetch, sin storage. Puede cargarse
 * ANTES o DESPUÉS de app.js. La vista (DOM) vive en app.js y consume
 * CausaDBTrades.groupTrades cuando existe.
 *
 * Contratos:
 * - Entrada: array de eventos del ledger. Acepta forma plana
 *   {event_type, payload, timestamp, event_id} (GET /api/query) y forma
 *   entrada {event: {...}} (POST /api/query / índice).
 * - Solo procesa event_type === 'TRADE_EXECUTED'; el resto se ignora.
 * - Clave de agrupado: payload.trade_id (string). Piernas sin trade_id
 *   quedan cada una en su propio grupo huérfano bajo su event_id.
 * - Salida: [{trade_id, symbol, strategy, entry, exit, legs, orphan}]
 *   con legs ordenadas por timestamp ascendente.
 */

(function () {
  'use strict';

  var TRADE_LIMIT = 1000;

  function tradeEventOf(item) {
    if (!item) return null;
    if (item.event && typeof item.event === 'object') return item.event;
    return item;
  }

  function tradeIdOf(ev) {
    var p = (ev && ev.payload) || {};
    var tid = p.trade_id;
    if (tid == null) return null;
    tid = String(tid);
    return tid === '' ? null : tid;
  }

  function tsOf(ev) {
    return (ev && ev.timestamp) || '';
  }

  function groupTrades(items) {
    var groups = {};
    var order = [];
    var orphans = 0;
    (items || []).forEach(function (item, idx) {
      var ev = tradeEventOf(item);
      if (!ev || ev.event_type !== 'TRADE_EXECUTED') return;
      var tid = tradeIdOf(ev);
      var key = tid;
      var orphan = false;
      if (key == null) {
        orphan = true;
        orphans += 1;
        key = '__orphan__' + (ev.event_id || ('noidx-' + idx)) + '#' + orphans;
      }
      if (!groups[key]) {
        groups[key] = {
          trade_id: tid != null ? tid : key,
          symbol: null,
          strategy: null,
          entry: null,
          exit: null,
          legs: [],
          orphan: orphan,
        };
        order.push(key);
      }
      var g = groups[key];
      g.legs.push(ev);
      var p = ev.payload || {};
      if (g.symbol == null && p.symbol != null) g.symbol = p.symbol;
      if (g.strategy == null && p.strategy != null) g.strategy = p.strategy;
    });
    return order.map(function (key) {
      var g = groups[key];
      g.legs.sort(function (a, b) {
        var ta = tsOf(a), tb = tsOf(b);
        if (ta < tb) return -1;
        if (ta > tb) return 1;
        return 0;
      });
      g.legs.forEach(function (leg) {
        var phase = leg && leg.payload ? leg.payload.phase : null;
        if (phase === 'entry' && g.entry == null) g.entry = leg;
        else if (phase === 'exit' && g.exit == null) g.exit = leg;
      });
      return g;
    });
  }

  var _scope = (typeof globalThis !== 'undefined') ? globalThis
    : (typeof self !== 'undefined') ? self : this;
  _scope.CausaDBTrades = {
    groupTrades: groupTrades,
    TRADE_LIMIT: TRADE_LIMIT,
  };
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { groupTrades: groupTrades, TRADE_LIMIT: TRADE_LIMIT };
  }
})();
