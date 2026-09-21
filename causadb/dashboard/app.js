/* ── CausaDB Ledger Dashboard ── app.js ───────────────────────── */

(function () {
  'use strict';

  const QUERY_LIMIT = 500;

  // ── State ────────────────────────────────────────────────────
  const state = {
    events: [],
    trades: [],
    autoRefresh: false,
    refreshInterval: null,
    searchQuery: '',
    currentView: 'ledger',
  };

  // ── DOM refs ─────────────────────────────────────────────────
  const $ = (id) => document.getElementById(id);
  const searchInput = $('search-input');
  const searchStatus = $('search-status');
  const timelineEvents = $('timeline-events');
  const loading = $('loading');
  const emptyState = $('empty-state');
  const errorState = $('error-state');
  const eventCount = $('event-count');
  const refreshStatus = $('refresh-status');
  const modal = $('event-detail-modal');
  const modalJson = $('event-detail-json');
  const modalClose = modal.querySelector('.modal-close');
  const modalBackdrop = modal.querySelector('.modal-backdrop');
  const scorePanel = $('score-panel');
  const scoreValue = $('score-value');
  const scoreBarChurn = $('score-bar-churn');
  const scoreBarWaste = $('score-bar-waste');
  const scoreBarSurvival = $('score-bar-survival');
  const scoreRefreshBtn = $('score-refresh');
  const scoreDetailModal = $('score-detail-modal');
  const scoreModalClose = $('score-modal-close');
  const scoreDetailNumbers = $('score-detail-numbers');
  const scoreDetailWarnings = $('score-detail-warnings');
  const scoreDetailSessions = $('score-detail-sessions');
  const updateBanner = $('update-banner');
  const updateBannerVersion = $('update-banner-version');
  const updateBannerBtn = $('update-banner-btn');
  const telemetryToggle = $('telemetry-toggle');
  const crashBanner = $('crash-banner');
  const crashBannerText = $('crash-banner-text');
  const crashBannerBtn = $('crash-banner-btn');
  const crashModal = $('crash-modal');
  const crashModalClose = $('crash-modal-close');
  const crashModalBackdrop = $('crash-modal-backdrop');
  const crashList = $('crash-list');
  const crashSendAll = $('crash-send-all');
  const crashDeleteAll = $('crash-delete-all');
  const metricScoreValue = $('metric-score-value');
  const metricEventCount = $('metric-event-count');
  const metricTestCount = $('metric-test-count');
  const metricCommands = $('metric-commands');
  const metricScoreCard = $('metric-score');

  // ── Auth / login refs ──────────────────────────────────────────
  const loginView = $('login-view');
  const appView = $('app-view');
  const loginForm = $('login-form');
  const apiKeyInput = $('api-key-input');
  const loginError = $('login-error');
  const loginSaveBtn = $('login-save-btn');
  const logoutBtn = $('logout-btn');

  // ── Trades / nav refs ──────────────────────────────────────────
  const navLedger = $('nav-ledger');
  const navTrades = $('nav-trades');
  const viewLedger = $('view-ledger');
  const viewTrades = $('view-trades');
  const tradesList = $('trades-list');
  const tradesEmpty = $('trades-empty');
  const tradesError = $('trades-error');
  const tradesLoading = $('trades-loading');
  const tradesReload = $('trades-reload');

  // ── UI helpers ───────────────────────────────────────────────
  function show(el) { el.classList.remove('hidden'); }
  function hide(el) { el.classList.add('hidden'); }

  function setLoading(on) {
    on ? show(loading) : hide(loading);
  }

  function setError(msg) {
    if (msg) {
      errorState.textContent = msg;
      show(errorState);
    } else {
      hide(errorState);
    }
  }

  function setEmpty(on) {
    on ? show(emptyState) : hide(emptyState);
  }

  // ── XSS-safe helpers (Fase 3: sin innerHTML con datos del ledger) ──
  // Único punto para texto: todo dato del ledger va a textContent.
  function safeSet(el, text) {
    if (!el) return el;
    el.textContent = (text == null ? '' : String(text));
    return el;
  }
  function safeEl(tag, text, className) {
    var e = document.createElement(tag);
    if (className) e.className = className;
    if (text != null) e.textContent = String(text);
    return e;
  }
  function clearEl(el) {
    if (!el) return el;
    while (el.firstChild) el.removeChild(el.firstChild);
    return el;
  }

  // ── Auth: llave API solo en esta pestaña (sessionStorage) ──────
  // La llave vive solo en la memoria de la pestaña: nunca en disco,
  // ni en el DOM, ni en la URL, ni en logs. Solo viaja como header
  // X-API-Key en apiFetch().
  const API_KEY_STORAGE = 'causadb_api_key';

  function getApiKey() {
    try { return sessionStorage.getItem(API_KEY_STORAGE) || ''; }
    catch (e) { return ''; }
  }
  function setApiKey(k) {
    try { sessionStorage.setItem(API_KEY_STORAGE, k); } catch (e) {}
  }
  function clearApiKey() {
    try { sessionStorage.removeItem(API_KEY_STORAGE); } catch (e) {}
  }

  function showLogin(msg) {
    if (loginView) show(loginView);
    if (appView) hide(appView);
    if (loginError) safeSet(loginError, msg || '');
  }
  function showApp() {
    if (loginView) hide(loginView);
    if (appView) show(appView);
    if (loginError) safeSet(loginError, '');
  }
  function handleUnauthorized() {
    clearApiKey();
    showLogin('La llave venció o es inválida. Pegala de nuevo.');
  }

  // Wrapper central: inyecta X-API-Key; ante 401 borra la llave y
  // muestra el login. TODOS los fetch a /api/* pasan por acá.
  async function apiFetch(path, options) {
    options = options || {};
    var headers = {};
    var optHeaders = options.headers || {};
    Object.keys(optHeaders).forEach(function (k) { headers[k] = optHeaders[k]; });
    var key = getApiKey();
    if (key) headers['X-API-Key'] = key;
    var init = { method: options.method || 'GET', headers: headers };
    if (options.body !== undefined) init.body = options.body;
    var resp = await fetch(path, init);
    if (resp.status === 401) {
      handleUnauthorized();
      var err = new Error('HTTP 401: sin autorización (llave inválida o ausente)');
      err.status = 401;
      throw err;
    }
    return resp;
  }

  if (loginForm) {
    loginForm.addEventListener('submit', async function (e) {
      e.preventDefault();
      var candidate = apiKeyInput ? apiKeyInput.value : '';
      if (apiKeyInput) apiKeyInput.value = '';
      if (!candidate) {
        safeSet(loginError, 'Pegá una llave primero.');
        return;
      }
      loginSaveBtn.disabled = true;
      safeSet(loginError, 'Validando…');
      try {
        // Validación con llave candidata (no guardada todavía).
        var meResp = await fetch('/api/auth/me', { headers: { 'X-API-Key': candidate } });
        if (meResp.status === 401) {
          safeSet(loginError, 'Llave inválida.');
          return;
        }
        if (!meResp.ok) {
          safeSet(loginError, 'No se pudo validar (HTTP ' + meResp.status + ').');
          return;
        }
        setApiKey(candidate);
        showApp();
        bootstrap();
      } catch (err) {
        safeSet(loginError, 'Sin conexión al servidor.');
      } finally {
        loginSaveBtn.disabled = false;
      }
    });
  }

  if (logoutBtn) {
    logoutBtn.addEventListener('click', function () {
      clearApiKey();
      state.events = [];
      state.trades = [];
      if (apiKeyInput) apiKeyInput.value = '';
      showLogin('');
    });
  }

  // ── Colour map ───────────────────────────────────────────────
  const TYPE_COLORS = {
    FILE_MODIFIED:       '#58a6ff',
    COMMAND_RUN:         '#3fb950',
    SYSTEM:              '#d29922',
    GOVERNANCE_DECISION: '#bc8cff',
    STREAM_INTERRUPTED:  '#f85149',
    HUMAN_FEEDBACK:      '#f0883e',
    AGENT_STATE:         '#79c0ff',
    REASONING_STEP:      '#56d364',
  };

  function typeColor(type) {
    return TYPE_COLORS[type] || '#8b949e';
  }

  // ── Formatting ───────────────────────────────────────────────
  function fmtTimestamp(ts) {
    if (!ts) return '—';
    try {
      return new Date(ts).toLocaleString(undefined, {
        year: 'numeric', month: 'short', day: 'numeric',
        hour: '2-digit', minute: '2-digit', second: '2-digit',
      });
    } catch { return ts; }
  }

  function truncatePayload(payload, maxLen) {
    if (payload == null) return '{}';
    try {
      const str = JSON.stringify(payload);
      if (str.length <= (maxLen || 100)) return str;
      return str.slice(0, maxLen) + '…';
    } catch { return String(payload); }
  }

  // ── Fetch events ─────────────────────────────────────────────
  async function fetchEvents() {
    setLoading(true);
    setError(null);
    setEmpty(false);

    try {
      const resp = await apiFetch('/api/query?limit=' + QUERY_LIMIT);
      if (!resp.ok) {
        let detail = '';
        try { const e = await resp.json(); detail = e.error || ''; } catch {}
        throw new Error(`HTTP ${resp.status}${detail ? ': ' + detail : ''}`);
      }
      state.events = await resp.json();
      renderTimeline(state.events);
      eventCount.textContent = state.events.length + ' event' + (state.events.length !== 1 ? 's' : '');
      searchStatus.textContent = state.searchQuery ? '(filtered)' : '';
      metricEventCount.textContent = state.events.length;
      var nCommands = state.events.filter(function (ev) { return ev.event_type === 'COMMAND_RUN'; }).length;
      metricCommands.textContent = nCommands;
    } catch (err) {
      setError('Failed to load events: ' + err.message);
      eventCount.textContent = '—';
    } finally {
      setLoading(false);
    }
  }

  // ── Score ─────────────────────────────────────────────────────
  async function fetchScore() {
    scoreRefreshBtn.disabled = true;
    try {
      const resp = await apiFetch('/api/score');
      if (!resp.ok) {
        show(scorePanel);
        scoreValue.textContent = 'err';
        scoreValue.className = 'score-value low';
        scoreBarChurn.style.width = '0%';
        scoreBarWaste.style.width = '0%';
        scoreBarSurvival.style.width = '0%';
        metricScoreValue.textContent = 'err';
        return;
      }
      const data = await resp.json();
      renderScore(data);
      window._lastScoreData = data;
      fetchTestCount();
    } catch (err) {
      scoreValue.textContent = '—';
      scoreValue.className = 'score-value';
      scoreBarChurn.style.width = '0%';
      scoreBarWaste.style.width = '0%';
      scoreBarSurvival.style.width = '0%';
    } finally {
      scoreRefreshBtn.disabled = false;
    }
  }

  function renderScore(data) {
    show(scorePanel);
    const overall = Math.round(data.overall_score);
    scoreValue.textContent = overall;
    metricScoreValue.textContent = overall;

    var color;
    if (overall >= 70) color = 'high';
    else if (overall >= 40) color = 'mid';
    else color = 'low';
    scoreValue.className = 'score-value ' + color;

    scoreBarChurn.style.width = Math.round(data.churn_score) + '%';
    scoreBarWaste.style.width = Math.round(data.waste_score) + '%';
    scoreBarSurvival.style.width = Math.round(data.survival_score) + '%';
  }

  $('score-main').addEventListener('click', function() {
    if (!window._lastScoreData) return;
    showScoreDetail(window._lastScoreData);
  });

  metricScoreCard.addEventListener('click', function() {
    if (!window._lastScoreData) return;
    showScoreDetail(window._lastScoreData);
  });

  async function fetchTestCount() {
    try {
      var resp = await apiFetch('/api/health');
      if (!resp.ok) return;
      var data = await resp.json();
      metricTestCount.textContent = data.total_tests || data.total_events || '—';
    } catch (e) {
      metricTestCount.textContent = '—';
    }
  }

  function scoreRow(label, valueText) {
    var row = document.createElement('div');
    row.className = 'score-detail-row';
    row.appendChild(safeEl('span', label));
    var v = safeEl('span', valueText, 'val');
    row.appendChild(v);
    return row;
  }

  function showScoreDetail(data) {
    clearEl(scoreDetailNumbers);

    var w = data.weights_used || {};
    var weights_text = 'Churn: ' + w.churn + ' | Waste: ' + w.waste + ' | Survival: ' + w.survival;

    var corr = data.correlation_method || '';
    if (corr === 'timestamp_proximity') {
      corr = 'timestamp_proximity ⚠️ imprecisa';
    }

    var sec1 = document.createElement('div');
    sec1.className = 'score-detail-section';
    sec1.appendChild(safeEl('h3', 'Overall'));
    sec1.appendChild(scoreRow('Score', Math.round(data.overall_score) + '/100'));
    sec1.appendChild(scoreRow('Churn', Math.round(data.churn_score) + '/100'));
    sec1.appendChild(scoreRow('Waste', Math.round(data.waste_score) + '/100'));
    sec1.appendChild(scoreRow('Survival', Math.round(data.survival_score) + '/100'));
    scoreDetailNumbers.appendChild(sec1);

    var sec2 = document.createElement('div');
    sec2.className = 'score-detail-section';
    sec2.appendChild(safeEl('h3', 'Config'));
    sec2.appendChild(scoreRow('Weights', weights_text));
    sec2.appendChild(scoreRow('Method', corr));
    scoreDetailNumbers.appendChild(sec2);

    // Warnings (solo texto vía safeSet)
    var warnings = (data.warnings || []).filter(function(w) {
      return !String(w.includes ? w : '').includes('no_snapshots_for');
    });
    if ((data.warnings || []).some(function(w) { return String(w).includes('no_snapshots'); })) {
      warnings.push('Sin snapshots en sesiones de test (revive-test, CRI-v2, opencode-config)');
    }
    if (warnings.length > 0) {
      var warnTexts = warnings.map(function(wn) {
        if (wn && wn.label) return wn.label;
        if (String(wn).includes('survival_defaulted')) return 'Survival: sin Git, asumimos 100%';
        return String(wn);
      });
      clearEl(scoreDetailWarnings);
      scoreDetailWarnings.appendChild(safeEl('strong', '⚠️ Advertencias'));
      var ul = document.createElement('ul');
      warnTexts.forEach(function(wt) {
        ul.appendChild(safeEl('li', wt));
      });
      scoreDetailWarnings.appendChild(ul);
      show(scoreDetailWarnings);
    } else {
      hide(scoreDetailWarnings);
    }

    // Per-session breakdown (ctx como texto)
    var perS = data.per_session || {};
    var sessions = Object.keys(perS);
    if (sessions.length > 0) {
      clearEl(scoreDetailSessions);
      scoreDetailSessions.appendChild(safeEl('h4', 'Per Session'));
      var table = document.createElement('table');
      var thead = document.createElement('thead');
      var hr = document.createElement('tr');
      ['Session', 'Overall', 'Churn', 'Waste', 'Surv.'].forEach(function(h) {
        hr.appendChild(safeEl('th', h));
      });
      thead.appendChild(hr);
      table.appendChild(thead);
      var tbody = document.createElement('tbody');
      sessions.forEach(function(ctx) {
        var s = perS[ctx] || {};
        var tr = document.createElement('tr');
        var shortCtx = (ctx.length > 32 ? ctx.substring(0, 30) + '...' : ctx);
        tr.appendChild(safeEl('td', shortCtx));
        var tdO = safeEl('td', String(Math.round(s.overall_score || 0)));
        tdO.style.textAlign = 'right';
        tr.appendChild(tdO);
        var tdC = safeEl('td', Math.round((s.churn_ratio || 0) * 100) + '%');
        tdC.style.textAlign = 'right';
        tr.appendChild(tdC);
        var tdW = safeEl('td', Math.round((s.waste_ratio || 0) * 100) + '%');
        tdW.style.textAlign = 'right';
        tr.appendChild(tdW);
        var tdS = safeEl('td', Math.round((s.survival_ratio || 0) * 100) + '%');
        tdS.style.textAlign = 'right';
        tr.appendChild(tdS);
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      scoreDetailSessions.appendChild(table);
      show(scoreDetailSessions);
    } else {
      hide(scoreDetailSessions);
    }

    show(scoreDetailModal);
  }

  scoreModalClose.addEventListener('click', function() { hide(scoreDetailModal); });
  document.querySelector('#score-detail-modal .modal-backdrop').addEventListener('click', function() { hide(scoreDetailModal); });

  scoreRefreshBtn.addEventListener('click', function() {
    fetchScore();
  });

  // ── Update Check ──────────────────────────────────────────────
  async function fetchUpdateCheck() {
    try {
      const resp = await apiFetch('/api/check-update');
      if (!resp.ok) return;
      const data = await resp.json();
      if (data.needs_update) {
        show(updateBanner);
        updateBannerVersion.textContent = data.latest_version;
        updateBannerBtn.addEventListener('click', async function() {
          updateBannerBtn.textContent = 'Actualizando...';
          updateBannerBtn.disabled = true;
          try {
            await apiFetch('/api/update', { method: 'POST' });
            updateBannerBtn.textContent = 'Reiniciar daemon';
          } catch (e) {
            updateBannerBtn.textContent = 'Error';
          }
        });
      } else {
        hide(updateBanner);
      }
    } catch (e) {
      // Silently ignore — update check is non-critical
    }
  }

  // ── Crash Reporter ────────────────────────────────────────────
  async function fetchCrashes() {
    try {
      const resp = await apiFetch('/api/crashes');
      if (!resp.ok) return;
      const crashes = await resp.json();
      if (crashes.length > 0) {
        show(crashBanner);
        const total = crashes.reduce(function (sum, c) { return sum + c.occurrences; }, 0);
        crashBannerText.textContent = 'Tenés ' + total + ' crash report' + (total > 1 ? 's' : '') + ' sin enviar';
      } else {
        hide(crashBanner);
      }
    } catch (e) {}
  }

  crashBannerBtn.addEventListener('click', function () {
    show(crashModal);
    renderCrashList();
  });

  crashModalClose.addEventListener('click', function () {
    hide(crashModal);
  });

  crashModalBackdrop.addEventListener('click', function () {
    hide(crashModal);
  });

  async function renderCrashList() {
    try {
      const resp = await apiFetch('/api/crashes');
      if (!resp.ok) return;
      const crashes = await resp.json();
      clearEl(crashList);
      crashes.forEach(function (c) {
        var item = document.createElement('div');
        item.className = 'crash-item';
        var head = document.createElement('div');
        head.appendChild(safeEl('strong', c.exception_type));
        head.appendChild(document.createTextNode(' (' + c.occurrences + 'x)'));
        item.appendChild(head);
        var p = document.createElement('p');
        p.appendChild(safeEl('code', c.exception_msg || ''));
        item.appendChild(p);
        safeSet(item.appendChild(document.createElement('small')),
          (c.timestamp || '') + ' — ' + (c.os || ''));
        crashList.appendChild(item);
      });
    } catch (e) {}
  }

  crashSendAll.addEventListener('click', async function () {
    crashSendAll.disabled = true;
    try {
      const resp = await apiFetch('/api/crashes/export', { method: 'POST' });
      if (resp.ok) {
        alert('Crash reports exported. You can find them in ~/.causadb/crashes/');
      } else {
        alert('Export failed.');
      }
    } catch (e) {
      alert('Export failed: ' + e.message);
    } finally {
      crashSendAll.disabled = false;
    }
  });

  crashDeleteAll.addEventListener('click', async function () {
    if (!confirm('Borrar todos los crash reports?')) return;
    try {
      await apiFetch('/api/crashes', { method: 'DELETE' });
      hide(crashBanner);
      hide(crashModal);
    } catch (e) {
      alert('Delete failed: ' + e.message);
    }
  });

  // ── Telemetry (#6 Privacidad Opt-out) ────────────────────────
  async function fetchTelemetryStatus() {
    try {
      const resp = await apiFetch('/api/config');
      if (!resp.ok) return;
      const config = await resp.json();
      telemetryToggle.checked = config.telemetry_enabled !== false;
    } catch (e) {
      // Silently ignore — telemetry is non-critical
    }
  }

  telemetryToggle.addEventListener('change', async function () {
    const enabled = this.checked;
    try {
      await apiFetch('/api/config', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ telemetry_enabled: enabled }),
      });
    } catch (e) {
      // Revert on failure
      this.checked = !enabled;
    }
  });

  // ── Search ───────────────────────────────────────────────────
  async function searchEvents(query) {
    state.searchQuery = query;
    setLoading(true);
    setError(null);
    setEmpty(false);

    try {
      const url = query
        ? '/api/query?q=' + encodeURIComponent(query) + '&limit=' + QUERY_LIMIT
        : '/api/query?limit=' + QUERY_LIMIT;
      const resp = await apiFetch(url);
      if (!resp.ok) {
        let detail = '';
        try { const e = await resp.json(); detail = e.error || ''; } catch {}
        throw new Error(`HTTP ${resp.status}${detail ? ': ' + detail : ''}`);
      }
      const results = await resp.json();
      state.events = results;
      renderTimeline(results);
      eventCount.textContent = results.length + ' event' + (results.length !== 1 ? 's' : '');
      searchStatus.textContent = query ? '(filtered)' : '';
    } catch (err) {
      setError('Search failed: ' + err.message);
    } finally {
      setLoading(false);
    }
  }

  // ── Humanize events ─────────────────────────────────────────
  function humanizeEvent(event) {
    var p = event.payload || {};
    switch (event.event_type) {
      case 'COMMAND_RUN':
        var cmd = p.command || '';
        return { icon: '>', title: cmd.length > 70 ? cmd.substring(0, 67) + '…' : cmd, desc: 'Comando ejecutado' };
      case 'GOVERNANCE_DECISION':
        var reason = (p.reasoning || '').substring(0, 120);
        return { icon: '⚖', title: reason, desc: 'Decision · ' + (p.impact || '') + ' · ' + (p.decision_type || '') };
      case 'FILE_MODIFIED':
        return { icon: '◈', title: p.path || '', desc: 'Archivo ' + (p.action || 'modificado') };
      case 'SYSTEM_BOOT':
        return { icon: '⬡', title: 'Sistema iniciado', desc: p.action || 'CausaDB boot' };
      case 'SCORE_RECORDED':
        return { icon: '⚡', title: 'Score: ' + (p.score || '—') + '/100', desc: 'Metrica de productividad' };
      case 'PROJECT_SNAPSHOT':
        return { icon: '◉', title: 'Snapshot', desc: (p.total_events || 0) + ' eventos, ' + (p.total_tests || 0) + ' tests' };
      case 'HUMAN_FEEDBACK':
        return { icon: '✎', title: (p.text || '').substring(0, 100), desc: 'Feedback del operador' };
      case 'REASONING_STEP':
        return { icon: '…', title: p.intent || '', desc: (p.text || '').substring(0, 100) };
      case 'STREAM_INTERRUPTED':
        return { icon: '⚠', title: 'Stream interrumpido', desc: p.reason || '' };
      case 'AGENT_STATE':
        return { icon: '⚙', title: p.state || '', desc: 'Estado de agente' };
      case 'SESSION_SUMMARY':
        return { icon: '◈', title: 'Session: ' + ((p.tool || p.session_id) || ''), desc: (p.turn_count || 0) + ' turnos, ' + (p.tokens_used || 0) + ' tokens' };
      default:
        return { icon: '·', title: (event.event_type || 'Evento').replace('_', ' '), desc: JSON.stringify(p).substring(0, 120) };
    }
  }

  // ── Render timeline ──────────────────────────────────────────
  function renderTimeline(events) {
    timelineEvents.innerHTML = '';

    if (!events || events.length === 0) {
      setEmpty(true);
      return;
    }
    setEmpty(false);

    events.forEach(function (event, i) {
      var h = humanizeEvent(event);
      const bubble = document.createElement('div');
      bubble.className = 'timeline-bubble ' + (i % 2 === 0 ? 'left' : 'right');

      // Title row (icon + text)
      const titleRow = document.createElement('div');
      titleRow.className = 'event-title-row';
      const iconEl = document.createElement('span');
      iconEl.className = 'event-icon';
      iconEl.textContent = h.icon;
      const titleEl = document.createElement('span');
      titleEl.className = 'event-title-text';
      titleEl.textContent = h.title;
      titleRow.appendChild(iconEl);
      titleRow.appendChild(titleEl);

      // Description
      const desc = document.createElement('div');
      desc.className = 'event-desc';
      desc.textContent = h.desc;

      // Badge + source
      const metaRow = document.createElement('div');
      metaRow.className = 'event-meta-row';
      const badge = document.createElement('span');
      badge.className = 'event-type-badge';
      badge.style.backgroundColor = typeColor(event.event_type);
      badge.textContent = (event.event_type || 'UNKNOWN').replace('_', ' ');
      metaRow.appendChild(badge);
      if (event.source) {
        var srcSpan = document.createElement('span');
        srcSpan.className = 'event-source';
        srcSpan.textContent = event.source;
        metaRow.appendChild(srcSpan);
      }

      // Timestamp
      const time = document.createElement('div');
      time.className = 'event-timestamp';
      time.textContent = fmtTimestamp(event.timestamp);

      // Assemble bubble
      bubble.appendChild(titleRow);
      bubble.appendChild(desc);
      bubble.appendChild(metaRow);
      bubble.appendChild(time);

      // Click → detail modal
      bubble.addEventListener('click', function () {
        showEventDetail(event);
      });

      // Dot
      const dot = document.createElement('div');
      dot.className = 'timeline-dot';
      dot.style.borderColor = typeColor(event.event_type);

      // Spacer (mirror)
      const spacer = document.createElement('div');
      spacer.className = 'timeline-spacer';

      // Item wrapper
      const item = document.createElement('div');
      item.className = 'timeline-item';

      if (i % 2 === 0) {
        // Bubble left, dot center, spacer right
        item.appendChild(bubble);
        item.appendChild(dot);
        item.appendChild(spacer);
      } else {
        // Spacer left, dot center, bubble right
        item.appendChild(spacer);
        item.appendChild(dot);
        item.appendChild(bubble);
      }

      timelineEvents.appendChild(item);
    });
  }

  // ── Modal ────────────────────────────────────────────────────
  const traceSection = $('trace-section');
  const traceTree = $('trace-tree');
  const traceButton = $('trace-button');
  const traceStatus = $('trace-status');
  let lastTracedEvent = null;

  function showEventDetail(event) {
    var h = humanizeEvent(event);
    var friendlyView = document.createElement('div');
    friendlyView.className = 'event-friendly-view';
    friendlyView.appendChild(safeEl('div', h.icon, 'efv-icon'));
    friendlyView.appendChild(safeEl('div', h.title, 'efv-title'));
    friendlyView.appendChild(safeEl('div',
      fmtTimestamp(event.timestamp) + ' · ' + (event.event_type || 'UNKNOWN').replace('_', ' ') + ' · ' + (event.source || ''),
      'efv-meta'));
    friendlyView.appendChild(safeEl('div', h.desc, 'efv-desc'));
    if (event.event_id) {
      friendlyView.appendChild(safeEl('div', 'ID: ' + event.event_id, 'efv-id'));
    }

    modalJson.textContent = JSON.stringify(event, null, 2);
    modalJson.parentNode.insertBefore(friendlyView, modalJson);

    // Toggle raw JSON
    var toggleBtn = document.createElement('button');
    toggleBtn.className = 'efv-toggle-btn';
    toggleBtn.textContent = 'Ver JSON';
    toggleBtn.onclick = function() {
      if (modalJson.classList.contains('hidden')) {
        modalJson.classList.remove('hidden');
        toggleBtn.textContent = 'Ocultar JSON';
      } else {
        modalJson.classList.add('hidden');
        toggleBtn.textContent = 'Ver JSON';
      }
    };
    modalJson.classList.add('hidden');
    modalJson.parentNode.insertBefore(toggleBtn, modalJson);

    show(modal);
    document.body.style.overflow = 'hidden';

    // Reset trace section
    show(traceSection);
    traceTree.innerHTML = '';
    traceStatus.textContent = '';
    lastTracedEvent = event;

    // Cleanup on close
    var origClose = function() {
      var fv = document.querySelector('.event-friendly-view');
      if (fv) fv.remove();
      var tb = document.querySelector('.efv-toggle-btn');
      if (tb) tb.remove();
      modalJson.classList.remove('hidden');
    };
    modalClose.addEventListener('click', origClose, {once: true});
    modalBackdrop.addEventListener('click', origClose, {once: true});
  }

  traceButton.addEventListener('click', async function () {
    if (!lastTracedEvent) return;
    traceStatus.textContent = 'Loading…';
    traceButton.disabled = true;
    try {
      const resp = await apiFetch('/api/trace', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ event_id: lastTracedEvent.event_id }),
      });
      if (!resp.ok) {
        let detail = '';
        try { const e = await resp.json(); detail = e.error || ''; } catch {}
        throw new Error('HTTP ' + resp.status + (detail ? ': ' + detail : ''));
      }
      const data = await resp.json();
      renderTraceTree(data);
      traceStatus.textContent = 'Trace loaded';
    } catch (err) {
      traceStatus.textContent = 'Trace failed: ' + err.message;
    } finally {
      traceButton.disabled = false;
    }
  });

  function renderTraceTree(data) {
    traceTree.innerHTML = '';

    // Render parents (reversed so root is first)
    var parents = data.parents || [];
    var children = data.children || [];
    var grandchildren = data.grandchildren || [];

    // Parents
    parents.reverse().forEach(function (p, i) {
      var depth = i;
      var node = createTraceNode(p, depth, false, i < parents.length - 1);
      traceTree.appendChild(node);
    });

    // Target event
    var targetNode = createTraceNode(data.event, parents.length, true, children.length > 0 || grandchildren.length > 0);
    targetNode.classList.add('target');
    traceTree.appendChild(targetNode);

    // Children
    children.forEach(function (c, i) {
      var depth = parents.length + 1;
      var hasMore = (i < children.length - 1) || grandchildren.length > 0;
      var node = createTraceNode(c, depth, false, hasMore);
      traceTree.appendChild(node);
    });

    // Grandchildren
    grandchildren.forEach(function (gc, i) {
      var depth = parents.length + 2;
      var node = createTraceNode(gc, depth, false, i < grandchildren.length - 1);
      traceTree.appendChild(node);
    });
  }

  function createTraceNode(event, depth, isTarget, hasConnector) {
    var wrapper = document.createElement('div');
    wrapper.className = 'trace-node';
    wrapper.style.paddingLeft = (depth * 24) + 'px';

    var line = document.createElement('div');
    line.className = 'trace-node-line';

    // Connector
    var conn = document.createElement('span');
    conn.className = 'trace-connector';
    if (depth === 0) {
      conn.textContent = '';
    } else if (hasConnector) {
      conn.textContent = '├─ ';
    } else {
      conn.textContent = '└─ ';
    }
    line.appendChild(conn);

    // Badge
    var badge = document.createElement('span');
    badge.className = 'event-type-badge trace-badge';
    badge.style.backgroundColor = typeColor(event.event_type);
    badge.textContent = event.event_type || 'UNKNOWN';
    line.appendChild(badge);

    // Timestamp
    var time = document.createElement('span');
    time.className = 'trace-time';
    time.textContent = fmtTimestamp(event.timestamp);
    line.appendChild(time);

    // Event ID (truncated)
    var eid = document.createElement('span');
    eid.className = 'trace-eid';
    eid.textContent = (event.event_id || '').slice(0, 8) + '…';
    eid.title = event.event_id || '';
    line.appendChild(eid);

    wrapper.appendChild(line);
    return wrapper;
  }

  function hideEventDetail() {
    hide(modal);
    document.body.style.overflow = '';
  }

  modalClose.addEventListener('click', hideEventDetail);
  modalBackdrop.addEventListener('click', hideEventDetail);
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') hideEventDetail();
  });

  // ── Search debounce ──────────────────────────────────────────
  let searchTimer = null;

  searchInput.addEventListener('input', function () {
    clearTimeout(searchTimer);
    const q = this.value.trim();
    if (q === state.searchQuery) return;

    searchTimer = setTimeout(function () {
      if (q) {
        searchEvents(q);
      } else {
        fetchEvents();
      }
    }, 300);
  });

  // ── Auto-refresh toggle ──────────────────────────────────────
  refreshStatus.addEventListener('click', function () {
    state.autoRefresh = !state.autoRefresh;
    this.textContent = state.autoRefresh ? 'auto-refresh on' : 'auto-refresh off';
    if (state.autoRefresh) {
      startAutoRefresh();
    } else {
      stopAutoRefresh();
    }
  });

  function startAutoRefresh() {
    stopAutoRefresh();
    state.refreshInterval = setInterval(function () {
      // Only refresh if search is empty (search already calls API on input)
      if (!state.searchQuery) {
        fetchEvents();
      }
    }, 5000);
  }

  function stopAutoRefresh() {
    if (state.refreshInterval) {
      clearInterval(state.refreshInterval);
      state.refreshInterval = null;
    }
  }

  // ── Revive ────────────────────────────────────────────────────
  const reviveButton = $('revive-button');
  const reviveDatetime = $('revive-datetime');
  const reviveResult = $('revive-result');
  const reviveStatus = $('revive-status');

  reviveButton.addEventListener('click', async function () {
    const val = reviveDatetime.value;
    if (!val) {
      reviveStatus.textContent = 'Please select a date/time';
      return;
    }
    reviveStatus.textContent = 'Reviving…';
    reviveButton.disabled = true;
    try {
      const isoString = new Date(val).toISOString();
      const resp = await apiFetch('/api/replay', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ to_time: isoString }),
      });
      if (!resp.ok) {
        let detail = '';
        try { const e = await resp.json(); detail = e.error || ''; } catch {}
        throw new Error('HTTP ' + resp.status + (detail ? ': ' + detail : ''));
      }
      const state = await resp.json();
      reviveResult.textContent = JSON.stringify(state, null, 2);
      show(reviveResult);
      reviveStatus.textContent = 'Revived to ' + new Date(val).toLocaleString();
    } catch (err) {
      reviveStatus.textContent = 'Revive failed: ' + err.message;
    } finally {
      reviveButton.disabled = false;
    }
  });

  // ── Export ────────────────────────────────────────────────────
  async function exportEvents(format) {
    const resp = await apiFetch('/api/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ format: format }),
    });
    if (!resp.ok) {
      let detail = '';
      try { const e = await resp.json(); detail = e.error || ''; } catch {}
      throw new Error('Export failed: HTTP ' + resp.status + (detail ? ': ' + detail : ''));
    }
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'causadb_export.' + (format === 'csv' ? 'csv' : 'json');
    a.click();
    URL.revokeObjectURL(url);
  }

  $('export-csv').addEventListener('click', function () {
    exportEvents('csv').catch(function (err) {
      setError(err.message);
    });
  });

  $('export-json').addEventListener('click', function () {
    exportEvents('json').catch(function (err) {
      setError(err.message);
    });
  });

  // ── Chat Assistant ──────────────────────────────────────────
  const chatBtn = $('chat-btn');
  const chatModal = $('chat-modal');
  const chatModalClose = $('chat-modal-close');
  const chatMessages = $('chat-messages');
  const chatInput = $('chat-input');
  const chatSendBtn = $('chat-send-btn');
  const chatStatus = $('chat-status');

  chatBtn.onclick = function () {
    show(chatModal);
    chatInput.focus();
  };

  chatModalClose.onclick = function () { hide(chatModal); };

  // Close on Escape for chat too
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape' && !chatModal.classList.contains('hidden')) hide(chatModal);
  });

  // Close on click outside
  chatModal.addEventListener('click', function (e) {
    if (e.target === chatModal) hide(chatModal);
  });

  chatSendBtn.onclick = sendChatMessage;
  chatInput.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') sendChatMessage();
  });

  async function sendChatMessage() {
    const text = chatInput.value.trim();
    if (!text) return;

    // Add user message
    addChatMessage('user', text);
    chatInput.value = '';
    chatSendBtn.disabled = true;
    show(chatStatus);
    chatStatus.textContent = 'Pensando...';

    try {
      const resp = await apiFetch('/api/assistant', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: text })
      });

      if (!resp.ok) {
        let detail = '';
        try { const e = await resp.json(); detail = e.error || ''; } catch {}
        addChatMessage('assistant', detail || 'Error al consultar el asistente.');
      } else {
        const data = await resp.json();
        addChatMessage('assistant', data.response || '(sin respuesta)');
      }
    } catch (err) {
      addChatMessage('assistant', 'Error de conexión. ¿Está Ollama corriendo en el puerto 11434?');
    } finally {
      hide(chatStatus);
      chatSendBtn.disabled = false;
      chatInput.focus();
    }
  }

  function addChatMessage(role, text) {
    const div = document.createElement('div');
    div.className = 'chat-msg ' + role;
    div.textContent = text;
    chatMessages.appendChild(div);
    chatMessages.scrollTop = chatMessages.scrollHeight;
  }

  // ── Daemon control ────────────────────────────────────────────

  const daemonIndicator = $('daemon-indicator');
  const daemonToggleBtn = $('daemon-toggle-btn');
  const daemonSubservices = document.querySelectorAll('.subservice');

  async function fetchDaemonStatus() {
    try {
      const resp = await apiFetch('/api/daemon/status');
      if (!resp.ok) return;
      const data = await resp.json();
      daemonIndicator.className = 'daemon-indicator ' + (data.running ? 'running' : 'stopped');
      daemonIndicator.title = data.running ? 'Daemon activo' : 'Daemon detenido';
      daemonToggleBtn.textContent = data.running ? 'Detener Daemon' : 'Iniciar Daemon';
      daemonSubservices.forEach(function(el) {
        var name = el.getAttribute('data-service');
        var active = data[name];
        el.className = 'subservice ' + (active ? 'active' : 'inactive');
      });
    } catch {}
  }

  daemonToggleBtn.addEventListener('click', async function() {
    var isRunning = daemonIndicator.className.includes('running');
    daemonToggleBtn.disabled = true;
    daemonToggleBtn.textContent = 'Procesando...';
    try {
      var resp = await apiFetch('/api/daemon/' + (isRunning ? 'stop' : 'start'), { method: 'POST' });
      var data = await resp.json();
      if (data.status === 'started' || data.status === 'stopped') {
        await fetchDaemonStatus();
      }
    } catch {}
    daemonToggleBtn.disabled = false;
  });

  // ── Workspace selector ────────────────────────────────────────

  const workspaceSelect = $('workspace-select');

  async function fetchWorkspaces() {
    try {
      var resp = await apiFetch('/api/workspaces');
      if (!resp.ok) return;
      var data = await resp.json();
      clearEl(workspaceSelect);
      if (!data.workspaces || data.workspaces.length === 0) {
        var emptyOpt = document.createElement('option');
        safeSet(emptyOpt, 'Sin proyectos');
        emptyOpt.value = '';
        workspaceSelect.appendChild(emptyOpt);
        return;
      }
      data.workspaces.forEach(function(ws) {
        var opt = document.createElement('option');
        opt.value = ws.ledger_path || '';
        safeSet(opt, ws.name || '');
        if (ws.is_active) opt.selected = true;
        workspaceSelect.appendChild(opt);
      });
    } catch {}
  }

  workspaceSelect.addEventListener('change', async function() {
    var ledgerPath = this.value;
    if (!ledgerPath) return;
    var prevValue = this.dataset.prevValue || this.querySelector('option[selected]')?.value || '';
    try {
      var resp = await apiFetch('/api/workspace/switch', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ledger_path: ledgerPath}),
      });
      var data = await resp.json();
      if (data.status === 'switched') {
        workspaceSelect.dataset.prevValue = ledgerPath;
        fetchEvents();
        fetchScore();
        fetchDaemonStatus();
        fetchCrashes();
        fetchTelemetryStatus();
      } else {
        workspaceSelect.value = prevValue;
      }
    } catch {
      workspaceSelect.value = prevValue;
    }
  });

  // ── Bootstrap ────────────────────────────────────────────────
  // ── Trades: ficha de evidencia (solo safeSet/safeEl/textContent) ──
  function setTradesLoading(on) {
    if (!tradesLoading) return;
    on ? show(tradesLoading) : hide(tradesLoading);
  }
  function setTradesError(msg) {
    if (!tradesError) return;
    if (msg) {
      safeSet(tradesError, msg);
      show(tradesError);
    } else {
      hide(tradesError);
    }
  }
  function setTradesEmpty(on) {
    if (!tradesEmpty) return;
    on ? show(tradesEmpty) : hide(tradesEmpty);
  }

  function fmtTradeVal(v) {
    if (v == null) return '—';
    if (typeof v === 'object') {
      try { return JSON.stringify(v); } catch (e) { return String(v); }
    }
    return String(v);
  }

  function tradeField(grid, key, val) {
    var f = document.createElement('div');
    f.className = 'trade-field';
    f.appendChild(safeEl('span', key, 'trade-field-key'));
    f.appendChild(safeEl('span', fmtTradeVal(val), 'trade-field-val'));
    grid.appendChild(f);
  }

  function tradeLegBox(card, title, leg) {
    var p = (leg && leg.payload) || {};
    var box = document.createElement('div');
    box.className = 'trade-leg';
    box.appendChild(safeEl('div', title, 'trade-leg-title'));
    var grid = document.createElement('div');
    grid.className = 'trade-grid';
    tradeField(grid, 'phase', p.phase);
    tradeField(grid, 'side', p.side);
    tradeField(grid, 'qty', p.qty);
    tradeField(grid, 'price', p.price);
    tradeField(grid, 'enter_tag', p.enter_tag);
    tradeField(grid, 'exit_reason', p.exit_reason);
    tradeField(grid, 'indicators_entry', p.indicators_entry);
    tradeField(grid, 'indicators_provenance', p.indicators_provenance);
    tradeField(grid, 'timestamp', (leg && leg.timestamp) || null);
    tradeField(grid, 'event_id', (leg && leg.event_id) || null);
    box.appendChild(grid);
    card.appendChild(box);
  }

  function tradePnlLine(entry, exit) {
    var pe = entry && entry.payload ? entry.payload : {};
    var px = exit && exit.payload ? exit.payload : {};
    var entryPx = Number(pe.price), exitPx = Number(px.price), qty = Number(pe.qty || px.qty);
    if (!isFinite(entryPx) || !isFinite(exitPx) || !isFinite(qty)) return null;
    var side = String(pe.side || px.side || '').toLowerCase();
    var dir = (side === 'sell' || side === 'short') ? -1 : 1;
    var pnl = (exitPx - entryPx) * qty * dir;
    var line = document.createElement('div');
    line.className = 'trade-pnl ' + (pnl >= 0 ? 'trade-pnl-profit' : 'trade-pnl-loss');
    var sign = pnl >= 0 ? '+' : '';
    safeSet(line, 'P&L aprox.: ' + sign + pnl + ' (salida ' + exitPx + ' − entrada ' + entryPx + ' × ' + qty + ')');
    return line;
  }

  function renderTradeCard(g) {
    var card = document.createElement('div');
    card.className = 'trade-card';
    var head = document.createElement('div');
    head.className = 'trade-head';
    head.appendChild(safeEl('span', g.trade_id || 'Operación sin ID', 'trade-title'));
    if (g.symbol != null) {
      head.appendChild(safeEl('span', g.symbol, 'event-type-badge'));
    }
    if (g.strategy != null) {
      head.appendChild(safeEl('span', g.strategy, 'event-type-badge'));
    }
    card.appendChild(head);
    var grid = document.createElement('div');
    grid.className = 'trade-grid';
    tradeField(grid, 'trade_id', g.trade_id);
    tradeField(grid, 'symbol', g.symbol);
    tradeField(grid, 'strategy', g.strategy);
    card.appendChild(grid);
    if (g.entry) tradeLegBox(card, 'Entrada', g.entry);
    if (g.exit) tradeLegBox(card, 'Salida', g.exit);
    var pnl = (g.entry && g.exit) ? tradePnlLine(g.entry, g.exit) : null;
    if (pnl) card.appendChild(pnl);
    // Sección fija: lo que el ledger NO puede dar (siempre visible).
    var nr = document.createElement('div');
    nr.className = 'trade-no-recover';
    nr.appendChild(safeEl('strong', 'No recuperable desde el ledger'));
    nr.appendChild(safeEl('span',
      'Versión de estrategia, configuración del bot y velas originales no quedan ' +
      'guardadas en el ledger. Esta ficha solo muestra lo que trae el evento TRADE_EXECUTED.'));
    card.appendChild(nr);
    return card;
  }

  function renderTrades(groups) {
    if (!tradesList) return;
    clearEl(tradesList);
    if (!groups || groups.length === 0) {
      setTradesEmpty(true);
      return;
    }
    setTradesEmpty(false);
    groups.forEach(function (g) {
      tradesList.appendChild(renderTradeCard(g));
    });
  }

  async function fetchTrades() {
    setTradesLoading(true);
    setTradesError(null);
    setTradesEmpty(false);
    try {
      var helper = (typeof CausaDBTrades !== 'undefined') ? CausaDBTrades : null;
      if (!helper || !helper.groupTrades) {
        throw new Error('No se cargó trades.js (groupTrades ausente).');
      }
      var resp = await apiFetch('/api/query?type=TRADE_EXECUTED&limit=1000');
      if (!resp.ok) {
        var detail = '';
        try { var e = await resp.json(); detail = e.error || ''; } catch (ign) {}
        throw new Error('HTTP ' + resp.status + (detail ? ': ' + detail : ''));
      }
      var items = await resp.json();
      var groups = helper.groupTrades(items);
      state.trades = groups;
      renderTrades(groups);
    } catch (err) {
      if (err && err.status === 401) return; // login ya visible
      setTradesError('No se pudieron cargar operaciones: ' + err.message);
    } finally {
      setTradesLoading(false);
    }
  }

  // ── Tabs Ledger / Trades ─────────────────────────────────────────
  function switchView(name) {
    state.currentView = name;
    var isTrades = name === 'trades';
    if (navLedger) navLedger.classList.toggle('active', !isTrades);
    if (navTrades) navTrades.classList.toggle('active', isTrades);
    if (viewLedger) { isTrades ? hide(viewLedger) : show(viewLedger); }
    if (viewTrades) { isTrades ? show(viewTrades) : hide(viewTrades); }
    if (isTrades) fetchTrades();
  }

  if (navLedger) navLedger.addEventListener('click', function () { switchView('ledger'); });
  if (navTrades) navTrades.addEventListener('click', function () { switchView('trades'); });
  if (tradesReload) tradesReload.addEventListener('click', function () { fetchTrades(); });

  function bootstrap() {
    fetchEvents();
    fetchScore();
    fetchUpdateCheck();
    fetchCrashes();
    fetchTelemetryStatus();
    fetchDaemonStatus();
    fetchWorkspaces();
    if (state.autoRefresh) startAutoRefresh();
    if (state.currentView === 'trades') fetchTrades();
  }

  // ── Init con puerta: sin llave no se pide ningún dato ────────────
  if (getApiKey()) {
    showApp();
    bootstrap();
  } else {
    showLogin('');
  }

})();
