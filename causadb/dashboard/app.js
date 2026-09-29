/* CausaDB dashboard (copia de diseno): Resumen / Ledger / Flota / Trades.
 *
 * Auth: la llave API vive SOLO en sessionStorage bajo 'causadb_api_key'.
 * Todo fetch a /api/* pasa por apiFetch (inyecta X-API-Key; ante 401
 * borra la llave y muestra el login). Unica excepcion: la validacion de
 * la llave candidata en el login contra GET /api/auth/me.
 * Render seguro: solo safeSet / safeEl / textContent, sin HTML inyectado.
 * El render DOM de trades vive aqui y consume groupTrades (trades.js).
 */
(function(){'use strict';
var API_KEY_STORAGE='causadb_api_key';
var TRADES_QUERY='/api/query?type=TRADE_EXECUTED&limit=1000';
var EVENTS_QUERY='/api/query?limit=1000';
var $=function(s){return document.querySelector(s)};
var state={events:[],trades:[],view:'summary',range:'Hoy',query:'',summaryScore:'—',summaryTests:'—'};
var detailEvent=null;

/* ── Render seguro: unico punto para texto (textContent) ── */
function safeSet(el,text){if(!el)return el;el.textContent=(text==null?'':String(text));return el}
function safeEl(tag,cls,text){var n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined&&text!==null)n.textContent=String(text);return n}

/* ── Auth: llave solo en la pestana ── */
function getApiKey(){try{return sessionStorage.getItem(API_KEY_STORAGE)||''}catch(e){return''}}
function clearApiKey(){try{sessionStorage.removeItem(API_KEY_STORAGE)}catch(e){}}
function showLogin(msg){var v=$('#login-view');if(v)v.hidden=false;var app=$('#app');if(app)app.hidden=true;safeSet($('#login-error'),msg||'')}
function showApp(){var v=$('#login-view');if(v)v.hidden=true;var app=$('#app');if(app)app.hidden=false;safeSet($('#login-error'),'')}

/* Wrapper central: inyecta X-API-Key; ante 401 borra la llave y muestra el login. */
function apiFetch(path,opts){
  opts=opts||{};
  var headers={'Content-Type':'application/json'};
  var extra=opts.headers||{};
  Object.keys(extra).forEach(function(k){headers[k]=extra[k]});
  var key=getApiKey();
  if(key)headers['X-API-Key']=key;
  var init={method:opts.method||'GET',headers:headers};
  if(opts.body!==undefined)init.body=opts.body;
  return fetch(path,init).then(function(r){
    if(r.status===401){clearApiKey();showLogin('La llave vencio o es invalida. Pegala de nuevo.');var err=new Error('AUTH');err.status=401;throw err}
    if(!r.ok)throw new Error('API');
    return r.json();
  });
}

/* ── Agrupado puro por sesion (testeable en node, sin DOM) ── */
function groupSessions(events){
  var map={};
  var order=[];
  (events||[]).forEach(function(e){
    if(!e)return;
    var data=e.data||e.payload||{};
    var id=e.session_id||data.session_id;
    if(!id)return;
    id=String(id);
    if(!map[id]){map[id]={id:id,events:[],eventCount:0,filesModified:0};order.push(id)}
    var g=map[id];
    g.events.push(e);
    g.eventCount+=1;
    var t=e.type||e.event_type;
    if(t==='FILE_MODIFIED')g.filesModified+=1;
  });
  return order.map(function(id){return map[id]});
}
function sessions(){return groupSessions(state.events)}

/* ── Resumen: medidores puros (testeables en node, sin DOM) ── */
function countByType(events,type){var n=0;(events||[]).forEach(function(e){if(!e)return;var t=e.event_type||e.type;if(t===type)n+=1});return n}
function pickTestCount(data){var d=data||{};return d.total_tests||d.total_events||'—'}

var escDate=function(d){try{return new Date(d).toLocaleTimeString('es-ES',{hour:'2-digit',minute:'2-digit'})}catch(e){return'—'}};
function refocus(){var s=document.querySelector('#page-content .search');if(s){s.focus();try{s.setSelectionRange(s.value.length,s.value.length)}catch(e){}}}
function iconFor(type){return type==='FILE_MODIFIED'?'▧':type==='TOOL_CALLED'?'⌘':type==='SESSION_SUMMARY'?'◉':'·'}
function simpleEvent(e){var type=e.type||e.event_type||'EVENT';var data=e.data||e.payload||{};var tool=e.tool||data.tool||'asistente';var file=e.path||data.path;if(type==='FILE_MODIFIED')return file?'Tu asistente actualizo '+file:'Tu asistente modifico un archivo';if(type==='TOOL_CALLED')return'Tu asistente ejecuto '+tool;if(type==='SESSION_SUMMARY')return'Tu asistente termino una sesion';return'Actividad registrada en el ledger'}
function render(){var root=$('#page-content');root.replaceChildren();safeSet($('#view-title'),{summary:'Resumen',ledger:'Ledger',fleet:'Flota',trades:'Trades'}[state.view]);({summary:renderSummary,ledger:renderLedger,fleet:renderFleet,trades:renderTrades}[state.view])()}
function head(kicker,title,sub,action){var d=safeEl('div','page-head');var copy=safeEl('div');copy.append(safeEl('p','eyebrow',kicker),safeEl('h1',null,title));copy.append(safeEl('p',null,sub));d.append(copy);if(action)d.append(action);return d}
function metric(label,value,detail,trend){var d=safeEl('article','metric');var top=safeEl('div','metric-top',label);if(trend)top.append(safeEl('span','metric-trend',trend));d.append(top,safeEl('div','metric-value',String(value)),safeEl('small',null,detail));return d}

function renderSummary(){
  var root=$('#page-content');
  root.append(head('RESUMEN DE ACTIVIDAD','Entiende el dia','Una lectura clara de lo que ocurrio en tu workspace.'));
  var m=safeEl('section','metrics');
  m.append(metric('SCORE CAUSAL',state.summaryScore,'pendiente de datos'),metric('EVENTOS',state.events.length,'registrados','En vivo'),metric('TESTS',state.summaryTests,'sin dato disponible'),metric('COMANDOS',countByType(state.events,'COMMAND_RUN'),'sin dato disponible'));
  root.append(m);
  var grid=safeEl('div','grid-2');
  var panel=safeEl('section','panel');
  var ph=safeEl('div','panel-head');
  ph.append(safeEl('h2',null,'Que hizo tu asistente'),safeEl('div','filters'));
  ['Ayer','Hoy','7 dias'].forEach(function(x){var b=safeEl('button','filter '+(state.range===x?'active':''),x);b.onclick=function(){state.range=x;render()};ph.lastChild.append(b)});
  panel.append(ph);
  var timeline=safeEl('div','timeline');
  var q=state.query.toLowerCase();
  var events=state.events.filter(function(e){return simpleEvent(e).toLowerCase().includes(q)});
  if(!events.length){var empty=safeEl('div','empty');empty.append(safeEl('strong',null,'Todavia no hay actividad para mostrar'),safeEl('p',null,'Cuando tu asistente trabaje, aqui veras el relato en lenguaje simple.'));timeline.append(empty)}
  events.slice(0,8).forEach(function(e){
    var row=safeEl('article','event');
    row.append(safeEl('span','event-icon',iconFor(e.type||e.event_type)),safeEl('div'),safeEl('time',null,escDate(e.created_at||e.timestamp)));
    row.children[1].append(safeEl('h3',null,simpleEvent(e)),safeEl('p',null,'Ver prueba · '+(e.session_id||'sesion no identificada')));
    row.onclick=function(){openDetail(e)};
    timeline.append(row);
  });
  panel.append(timeline);
  var side=safeEl('section','panel');
  var sh=safeEl('div','panel-head');
  sh.append(safeEl('h2',null,'Busqueda'),safeEl('span',null,'en espanol'));
  side.append(sh);
  var sb=safeEl('div','search-box');
  sb.style.padding='18px';
  var search=safeEl('input','search');
  search.placeholder='Buscar eventos, archivos o agentes';
  search.value=state.query;
  search.oninput=function(e){state.query=e.target.value;render();refocus()};
  sb.append(search);
  side.append(sb);
  grid.append(panel,side);
  root.append(grid);
}

function renderLedger(){
  var root=$('#page-content');
  var search=safeEl('input','search');
  search.placeholder='Buscar en el ledger…';
  search.value=state.query;
  search.oninput=function(e){state.query=e.target.value;render();refocus()};
  root.append(head('LEDGER','Todo el rastro, en un lugar','Eventos crudos con su evidencia causal.',search));
  var p=safeEl('section','panel table-wrap');
  var t=safeEl('table','data-table');
  var thead=safeEl('thead');
  thead.append(safeEl('tr'));
  ['Tipo','Actividad','Sesion','Momento','Estado'].forEach(function(x){thead.firstChild.append(safeEl('th',null,x))});
  var body=safeEl('tbody');
  state.events.filter(function(e){return simpleEvent(e).toLowerCase().includes(state.query.toLowerCase())}).forEach(function(e){
    var tr=safeEl('tr');
    [e.type||e.event_type,simpleEvent(e),e.session_id||'—',escDate(e.created_at||e.timestamp),'Verificado'].forEach(function(x,i){
      var td=safeEl('td',null,x);
      if(i===4)td.replaceChildren(safeEl('span','tag',x));
      tr.append(td);
    });
    tr.onclick=function(){openDetail(e)};
    body.append(tr);
  });
  t.append(thead,body);
  p.append(t);
  if(!body.children.length)p.append(safeEl('div','empty',null));
  root.append(p);
}

function renderFleet(){
  var root=$('#page-content');
  root.append(head('FLOTA','Tu flota, de un vistazo','Sesiones agrupadas desde la actividad real del ledger.'));
  var grid=safeEl('div','fleet-grid');
  groupSessions(state.events).forEach(function(s,i){
    var c=safeEl('article','fleet-card');
    var h=safeEl('header');
    h.append(safeEl('span','agent-avatar',String(i+1).padStart(2,'0')),safeEl('div'));
    var toolEv=s.events.find(function(e){return e.tool});
    h.lastChild.append(safeEl('strong',null,(toolEv&&toolEv.tool)||'Agente de trabajo'),safeEl('small',null,s.id));
    c.append(h);
    var dl=safeEl('dl');
    dl.append(safeEl('div',null),safeEl('div',null));
    dl.children[0].append(safeEl('dt',null,'Eventos'),safeEl('dd',null,String(s.eventCount)));
    dl.children[1].append(safeEl('dt',null,'Archivos tocados'),safeEl('dd',null,String(s.filesModified)));
    c.append(dl,safeEl('footer',null,'Ultimo visto '+escDate((s.events[s.events.length-1]||{}).created_at||(s.events[s.events.length-1]||{}).timestamp)));
    grid.append(c);
  });
  if(!grid.children.length)grid.append(safeEl('div','empty',null));
  root.append(grid);
}

/* ── Trades: ficha de evidencia (DOM aqui, agrupado en trades.js) ── */
function tradeField(label,value){
  var wrap=safeEl('div','trade-field');
  wrap.append(safeEl('span','trade-label',label));
  var text=(value!==undefined&&value!==null&&typeof value==='object')?JSON.stringify(value):String(value==null?'—':value);
  wrap.append(safeEl('span','trade-value',text));
  return wrap;
}
function renderTradeCard(g){
  var card=safeEl('article','trade-card');
  card.append(safeEl('h3',null,'Operacion '+g.trade_id));
  card.append(tradeField('trade_id',g.trade_id));
  var entry=(g.entry||{}).payload||(g.entry||{}).data||{};
  var exit=(g.exit||{}).payload||(g.exit||{}).data||{};
  card.append(tradeField('enter_tag',entry.enter_tag));
  card.append(tradeField('exit_reason',exit.exit_reason));
  card.append(tradeField('indicators_entry',entry.indicators_entry));
  card.append(tradeField('indicators_provenance',entry.indicators_provenance));
  var nr=safeEl('div','trade-no-recover');
  nr.append(safeEl('strong',null,'No recuperable desde el ledger'));
  nr.append(safeEl('p',null,'La configuracion del bot y las velas originales no quedan guardadas en el ledger.'));
  card.append(nr);
  return card;
}
function renderTrades(){
  var root=$('#page-content');
  root.append(head('TRADES','Evidencia de cambios','Revisa que se hizo, por que y con que resultado.'));
  var p=safeEl('section','panel');
  p.append(safeEl('div','panel-head','Trades recientes'));
  var groups=(typeof groupTrades==='function')?groupTrades(state.trades):[];
  if(!groups.length){
    var empty=safeEl('div','empty');
    empty.append(safeEl('strong',null,'Este ledger no tiene operaciones'));
    empty.append(safeEl('p',null,'Mostrando hasta 1000 eventos recientes del ledger. Sin paginacion.'));
    p.append(empty);
    root.append(p);
    return;
  }
  groups.forEach(function(g){p.append(renderTradeCard(g))});
  root.append(p);
}
function fetchTrades(){
  return apiFetch(TRADES_QUERY).then(function(data){
    var items=Array.isArray(data)?data:(data.events||data.results||[]);
    state.trades=(typeof groupTrades==='function')?groupTrades(items):[];
    if(state.view==='trades')render();
  }).catch(function(e){
    state.trades=[];
    if(state.view==='trades')render();
  });
}

/* ── Resumen: SCORE/TESTS via apiFetch (fail-soft '—', sin fetch nuevo p/COMANDOS) ── */
function fetchSummaryScore(){
  return apiFetch('/api/score').then(function(data){
    var v=data?(data.overall_score!=null?data.overall_score:(data.score!=null?data.score:(data.overall!=null?data.overall:'—'))):'—';
    if(v==null||v==='')v='—';
    state.summaryScore=v;
    if(state.view==='summary')render();
  }).catch(function(){state.summaryScore='—';if(state.view==='summary')render()});
}
function fetchSummaryTests(){
  return apiFetch('/api/health').then(function(data){
    state.summaryTests=pickTestCount(data);
    if(state.view==='summary')render();
  }).catch(function(){state.summaryTests='—';if(state.view==='summary')render()});
}

function openDetail(e){detailEvent=e;safeSet($('#modal-title'),simpleEvent(e));var body=$('#modal-body');body.replaceChildren(safeEl('pre','evidence',JSON.stringify(e,null,2)));$('#detail-modal').hidden=false}
document.querySelectorAll('[data-close-modal]').forEach(function(b){b.onclick=function(){detailEvent=null;$('#detail-modal').hidden=true}});
$('#revive-button').onclick=function(){var q=detailEvent&&(detailEvent.session_id||(detailEvent.data&&detailEvent.data.session_id));$('#detail-modal').hidden=true;detailEvent=null;if(q){state.query=q;state.view='ledger';document.querySelectorAll('.nav-item').forEach(function(n){n.classList.toggle('active',n.dataset.view==='ledger')});render()}};
document.querySelectorAll('.nav-item[data-view]').forEach(function(b){b.onclick=function(){document.querySelectorAll('.nav-item').forEach(function(n){n.classList.remove('active')});b.classList.add('active');state.view=b.dataset.view;if(state.view==='trades'){fetchTrades()}if(state.view==='summary'){fetchSummaryScore();fetchSummaryTests()}render()}});
$('#logout').onclick=function(){clearApiKey();state.events=[];state.trades=[];var input=$('#api-key-input');if(input)input.value = '';showLogin('')};
$('#telemetry-toggle').onclick=function(e){var on=e.currentTarget.querySelector('b');on.textContent=on.textContent==='ON'?'OFF':'ON'};

function init(){
  var key=getApiKey();
  if(!key){showLogin('');return}
  apiFetch('/api/auth/me').then(function(me){
    showApp();
    safeSet($('#user-name'),me.name||me.email||'Sesion local');
    safeSet($('#user-email'),me.email||'API autenticada');
    return apiFetch(EVENTS_QUERY);
  }).then(function(data){
    state.events=Array.isArray(data)?data:(data.events||data.results||[]);
    safeSet($('#event-count'),String(state.events.length));
    safeSet($('#fleet-count'),String(groupSessions(state.events).length));
    render();
    fetchSummaryScore();
    fetchSummaryTests();
  }).catch(function(e){
    if(e&&e.message==='AUTH'){showLogin('')}
    else{showApp();render()}
  });
}
$('#login-form').onsubmit=function(e){
  e.preventDefault();
  var input=$('#api-key-input');
  var candidate=input?input.value.trim():'';
  if(input)input.value = '';
  if(!candidate){safeSet($('#login-error'),'Pega una llave primero.');return}
  fetch('/api/auth/me',{headers:{'X-API-Key':candidate}}).then(function(r){
    if(r.status===401){safeSet($('#login-error'),'Llave invalida.');return null}
    if(!r.ok){safeSet($('#login-error'),'No se pudo validar.');return null}
    return r.json();
  }).then(function(me){
    if(!me)return;
    try{sessionStorage.setItem(API_KEY_STORAGE,candidate)}catch(err){}
    showApp();
    safeSet($('#user-name'),me.name||me.email||'Sesion local');
    init();
  }).catch(function(){safeSet($('#login-error'),'Sin conexion al servidor.')});
};
init();
})();
