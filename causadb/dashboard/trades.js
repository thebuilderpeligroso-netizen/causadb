/* CausaDB dashboard (copia de diseno) - trades.js
 *
 * Agrupado entry/exit de eventos del servidor por payload.trade_id.
 * Funcion PURA: sin DOM, sin fetch, sin storage. Ejecutable en node
 * sin DOM y en navegador como global groupTrades (app.js la consume).
 *
 * Entrada: array de eventos del servidor con payload.trade_id (string)
 *   y payload.phase ('entry' / 'exit').
 * Salida: array de grupos {trade_id, entry, exit} en orden de aparición.
 *   Los eventos sin trade_id se ignoran (no forman grupo).
 */
'use strict';
function groupTrades(events) {
  var groups = {};
  var order = [];
  (events || []).forEach(function (ev) {
    if (!ev) return;
    var p = ev.payload || ev.data || {};
    var tid = p.trade_id;
    if (tid == null || String(tid) === '') return;
    tid = String(tid);
    if (!groups[tid]) {
      groups[tid] = { trade_id: tid, entry: null, exit: null };
      order.push(tid);
    }
    var g = groups[tid];
    var phase = p.phase;
    if (phase === 'entry' && g.entry == null) g.entry = ev;
    else if (phase === 'exit' && g.exit == null) g.exit = ev;
  });
  return order.map(function (tid) { return groups[tid]; });
}
