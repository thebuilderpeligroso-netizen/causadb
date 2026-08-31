/* ── CausaDB Ledger Dashboard ── app.js ───────────────────────── */

(function () {
  'use strict';

  const QUERY_LIMIT = 500;

  // ── State ────────────────────────────────────────────────────
  const state = {
    events: [],
    autoRefresh: false,
    refreshInterval: null,
    searchQuery: '',
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
      const resp = await fetch('/api/query?limit=' + QUERY_LIMIT);
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
      const resp = await fetch('/api/score');
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
      var resp = await fetch('/api/health');
      if (!resp.ok) return;
      var data = await resp.json();
      metricTestCount.textContent = data.total_tests || data.total_events || '—';
    } catch (e) {
      metricTestCount.textContent = '—';
    }
  }

  function showScoreDetail(data) {
    scoreDetailNumbers.innerHTML = '';

    var w = data.weights_used || {};
    var weights_text = 'Churn: ' + w.churn + ' | Waste: ' + w.waste + ' | Survival: ' + w.survival;

    var corr = data.correlation_method || '';
    if (corr === 'timestamp_proximity') {
      corr = 'timestamp_proximity ⚠️ imprecisa';
    }

    scoreDetailNumbers.innerHTML =
      '<div class="score-detail-section">' +
        '<h3>Overall</h3>' +
        '<div class="score-detail-row"><span>Score</span><span class="val">' + Math.round(data.overall_score) + '/100</span></div>' +
        '<div class="score-detail-row"><span>Churn</span><span class="val">' + Math.round(data.churn_score) + '/100</span></div>' +
        '<div class="score-detail-row"><span>Waste</span><span class="val">' + Math.round(data.waste_score) + '/100</span></div>' +
        '<div class="score-detail-row"><span>Survival</span><span class="val">' + Math.round(data.survival_score) + '/100</span></div>' +
      '</div>' +
      '<div class="score-detail-section">' +
        '<h3>Config</h3>' +
        '<div class="score-detail-row"><span>Weights</span><span class="val">' + weights_text + '</span></div>' +
        '<div class="score-detail-row"><span>Method</span><span class="val">' + corr + '</span></div>' +
      '</div>';

    // Warnings
    var warnings = (data.warnings || []).filter(function(w) {
      return !w.includes('no_snapshots_for');
    });
    if ((data.warnings || []).some(function(w) { return w.includes('no_snapshots'); })) {
      warnings.push('Sin snapshots en sesiones de test (revive-test, CRI-v2, opencode-config)');
    }
    if (warnings.length > 0) {
      var warnTexts = warnings.map(function(wn) {
        if (wn.label) return wn.label;
        if (wn.includes('survival_defaulted')) return 'Survival: sin Git, asumimos 100%';
        return wn;
      });
      scoreDetailWarnings.innerHTML = '<strong>⚠️ Advertencias</strong><ul>' + warnTexts.map(function(w) { return '<li>' + w + '</li>'; }).join('') + '</ul>';
      show(scoreDetailWarnings);
    } else {
      hide(scoreDetailWarnings);
    }

    // Per-session breakdown
    var perS = data.per_session || {};
    var sessions = Object.keys(perS);
    if (sessions.length > 0) {
      var rows = sessions.map(function(ctx) {
        var s = perS[ctx];
        return '<tr><td>' + (ctx.length > 32 ? ctx.substring(0,30) + '...' : ctx) + '</td>' +
          '<td style="text-align:right">' + Math.round(s.overall_score) + '</td>' +
          '<td style="text-align:right">' + Math.round(s.churn_ratio * 100) + '%</td>' +
          '<td style="text-align:right">' + Math.round(s.waste_ratio * 100) + '%</td>' +
          '<td style="text-align:right">' + Math.round(s.survival_ratio * 100) + '%</td></tr>';
      });
      scoreDetailSessions.innerHTML = '<h4>Per Session</h4><table>' +
          '<thead><tr><th>Session</th><th>Overall</th><th>Churn</th><th>Waste</th><th>Surv.</th></tr></thead>' +
          '<tbody>' + rows.join('') + '</tbody></table>';
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
      const resp = await fetch('/api/check-update');
      if (!resp.ok) return;
      const data = await resp.json();
      if (data.needs_update) {
        show(updateBanner);
        updateBannerVersion.textContent = data.latest_version;
        updateBannerBtn.addEventListener('click', async function() {
          updateBannerBtn.textContent = 'Actualizando...';
          updateBannerBtn.disabled = true;
          try {
            await fetch('/api/update', { method: 'POST' });
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
      const resp = await fetch('/api/crashes');
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
      const resp = await fetch('/api/crashes');
      if (!resp.ok) return;
      const crashes = await resp.json();
      crashList.innerHTML = crashes.map(function (c) {
        return '<div class="crash-item">' +
          '<strong>' + c.exception_type + '</strong> (' + c.occurrences + 'x)' +
          '<p><code>' + (c.exception_msg || '') + '</code></p>' +
          '<small>' + c.timestamp + ' &mdash; ' + c.os + '</small>' +
          '</div>';
      }).join('');
    } catch (e) {}
  }

  crashSendAll.addEventListener('click', async function () {
    crashSendAll.disabled = true;
    try {
      const resp = await fetch('/api/crashes/export', { method: 'POST' });
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
      await fetch('/api/crashes', { method: 'DELETE' });
      hide(crashBanner);
      hide(crashModal);
    } catch (e) {
      alert('Delete failed: ' + e.message);
    }
  });

  // ── Telemetry (#6 Privacidad Opt-out) ────────────────────────
  async function fetchTelemetryStatus() {
    try {
      const resp = await fetch('/api/config');
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
      await fetch('/api/config', {
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
      const resp = await fetch(url);
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
    friendlyView.innerHTML =
      '<div class="efv-icon">' + h.icon + '</div>' +
      '<div class="efv-title">' + h.title + '</div>' +
      '<div class="efv-meta">' + fmtTimestamp(event.timestamp) + ' · ' + (event.event_type || 'UNKNOWN').replace('_', ' ') + ' · ' + (event.source || '') + '</div>' +
      '<div class="efv-desc">' + h.desc + '</div>' +
      (event.event_id ? '<div class="efv-id">ID: ' + event.event_id + '</div>' : '');

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
      const resp = await fetch('/api/trace', {
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
      const resp = await fetch('/api/replay', {
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
    const resp = await fetch('/api/export', {
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
      const resp = await fetch('/api/assistant', {
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
      const resp = await fetch('/api/daemon/status');
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
      var resp = await fetch('/api/daemon/' + (isRunning ? 'stop' : 'start'), { method: 'POST' });
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
      var resp = await fetch('/api/workspaces');
      if (!resp.ok) return;
      var data = await resp.json();
      if (!data.workspaces || data.workspaces.length === 0) {
        workspaceSelect.innerHTML = '<option value="">Sin proyectos</option>';
        return;
      }
      workspaceSelect.innerHTML = data.workspaces.map(function(ws) {
        var selected = ws.is_active ? ' selected' : '';
        return '<option value="' + ws.ledger_path + '"' + selected + '>' + ws.name + '</option>';
      }).join('');
    } catch {}
  }

  workspaceSelect.addEventListener('change', async function() {
    var ledgerPath = this.value;
    if (!ledgerPath) return;
    var prevValue = this.dataset.prevValue || this.querySelector('option[selected]')?.value || '';
    try {
      var resp = await fetch('/api/workspace/switch', {
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
  fetchEvents();
  fetchScore();
  fetchUpdateCheck();
  fetchCrashes();
  fetchTelemetryStatus();
  fetchDaemonStatus();
  fetchWorkspaces();
  if (state.autoRefresh) startAutoRefresh();

})();
