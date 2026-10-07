(function () {
  /* === 模板变量注入 ============================================ */
  var currentHours = window.__DASHBOARD_DATA__.hours;
  var initialRows = window.__DASHBOARD_DATA__.rows;
  var initialDaily = window.__DASHBOARD_DATA__.daily_elec;
  var initialLatestTs = window.__DASHBOARD_DATA__.initial_latest_ts;
  var chartInstance = null;
  var dailyChartInstance = null;
  var pollFailStreak = 0;
  var STORAGE_KEY = 'dorm-power-monitor.section';

  /* === R66 — View Transitions helper ========================= */
  function withViewTransition(callback) {
    if (typeof document.startViewTransition === 'function') {
      try {
        document.startViewTransition(callback);
        return;
      } catch (e) { /* fall through to sync */ }
    }
    // Fallback: run callback synchronously.  CSS @supports handles
    // any animation needs.
    callback();
  }

  /* === 工具函数 =============================================== */
  function fmtAxisLabel(raw) {
    if (!raw) return '';
    var m = String(raw).match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/);
    if (m) return m[2] + '-' + m[3] + ' ' + m[4] + ':' + m[5];
    return raw;
  }
  function readCssVar(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name);
    v = (v || '').trim();
    return v || fallback;
  }
  function setStatText(el, v, unit) {
    if (!el) return;
    el.classList.remove('is-empty');
    if (v === null || v === undefined || (typeof v === 'number' && isNaN(v))) {
      el.classList.add('is-empty');
      el.textContent = '—';
      return;
    }
    var text;
    if (typeof v === 'number') {
      text = v.toFixed(2) + (unit ? ' <span style="font-size:14px;color:var(--text-secondary)">' + unit + '</span>' : '');
    } else {
      text = String(v);
    }
    el.innerHTML = text;
  }
  function setPill() {
    var pill = document.getElementById('status-pill');
    if (!pill) return;
    var tsRaw = pill.getAttribute('data-ts') || '';
    // R48 — ts is NAIVE LOCAL (CST, "YYYY-MM-DD HH:MM:SS"), append +08:00
    // so JS Date parses it as Asia/Shanghai wall clock independent of host TZ.
    var last = tsRaw ? new Date(tsRaw.replace(' ', 'T') + '+08:00') : null;
    var isOnline = false;
    if (last && !isNaN(last.getTime())) {
      var ageSec = (Date.now() - last.getTime()) / 1000;
      isOnline = ageSec >= 0 && ageSec < 7200;
    }
    var heroDot = document.getElementById('hero-status-dot');
    var heroText = document.getElementById('hero-status-text');
    if (heroDot) {
      heroDot.classList.remove('offline', 'warning');
      if (!isOnline) heroDot.classList.add('offline');
    }
    if (heroText) {
      heroText.textContent = isOnline ? '已连接' : '已断开';
    }
    var barDot = document.getElementById('status-bar-dot');
    var barText = document.getElementById('status-bar-text');
    var barTs = document.getElementById('status-bar-ts');
    if (barDot && barText) {
      barDot.classList.remove('offline', 'warning');
      if (!isOnline) barDot.classList.add('offline');
      barText.textContent = isOnline ? '已连接' : '已离线';
    }
    if (barTs) {
      barTs.textContent = tsRaw ? ('最近采集 ' + tsRaw) : '最近采集 —';
    }
  }
  function setLastUpdate(tsRaw) {
    var el = document.getElementById('last-update');
    if (!el) return;
    el.textContent = tsRaw ? tsRaw : '—';
  }

  /* === Toast 通知 ============================================== */
  function showToast(message, kind, duration) {
    kind = kind || 'info';
    duration = (typeof duration === 'number') ? duration : 3000;
    var container = document.getElementById('toast-container');
    if (!container) return;
    var toast = document.createElement('dorm-toast');
    toast.setAttribute('kind', kind);
    toast.setAttribute('duration', String(duration));
    toast.textContent = message;
    // Use View Transitions API for the entrance slide.
    withViewTransition(function () {
      container.appendChild(toast);
    });
  }

  /* === Chart.js 初始化 ========================================== */
  function buildChart(rows) {
    var canvas = document.getElementById('chart');
    if (!canvas) return;
    var labels = (rows || []).map(function (r) { return fmtAxisLabel(r.ts); });
    var data = (rows || []).map(function (r) {
      return (r.remain === null || r.remain === undefined) ? null : Number(r.remain);
    });
    var rawTsList = (rows || []).map(function (r) {
      return r && r.ts ? String(r.ts) : '';
    });
    if (chartInstance) {
      // Wrap re-render in View Transitions so the canvas cross-fades.
      withViewTransition(function () {
        chartInstance.data.labels = labels;
        chartInstance.data.datasets[0].data = data;
        chartInstance.update();
      });
      return;
    }
    var textSecondary = readCssVar('--text-secondary', '#a1a1aa');
    var borderColor = readCssVar('--border', 'rgba(255,255,255,0.08)');
    var tooltipBg = readCssVar('--bg-card', '#131316');
    var tooltipFg = readCssVar('--text-primary', '#fafafa');
    chartInstance = new Chart(canvas.getContext('2d'), {
      type: 'line',
      data: {
        labels: labels,
        datasets: [{
          label: '剩余电量 (kW·h)',
          data: data,
          borderColor: '#6366f1',
          backgroundColor: function (context) {
            var chart = context.chart;
            var c = chart.ctx;
            var area = chart.chartArea;
            if (!area) return 'rgba(99,102,241,0.20)';
            var g = c.createLinearGradient(0, area.top, 0, area.bottom);
            g.addColorStop(0, 'rgba(99,102,241,0.20)');
            g.addColorStop(1, 'rgba(99,102,241,0)');
            return g;
          },
          fill: true,
          tension: 0.3,
          borderWidth: 1.5,
          pointRadius: 0,
          pointHoverRadius: 4,
          pointBackgroundColor: '#6366f1',
          pointBorderColor: tooltipBg,
          pointBorderWidth: 1.5,
          spanGaps: true,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 350 },
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: tooltipBg,
            borderColor: borderColor,
            borderWidth: 1,
            titleColor: '#a1a1aa',
            bodyColor: '#fafafa',
            padding: 10,
            displayColors: false,
            callbacks: {
              title: function (items) {
                if (!items || !items.length) return '';
                var idx = items[0].dataIndex;
                return rawTsList[idx] || labels[idx] || '';
              },
              label: function (item) {
                var v = item.parsed.y;
                return (v == null ? '—' : v.toFixed(2) + ' kW·h');
              }
            }
          }
        },
        scales: {
          x: {
            grid: { display: false },
            ticks: { color: '#71717a', font: { size: 10 }, maxRotation: 0, autoSkip: true, autoSkipPadding: 20 },
            border: { color: 'rgba(255,255,255,0.05)' }
          },
          y: {
            grid: { color: 'rgba(255,255,255,0.04)' },
            ticks: { color: '#71717a', font: { size: 10 }, callback: function (v) { return v; } },
            border: { display: false }
          }
        }
      }
    });
  }

  function buildDailyChart(rows) {
    var canvas = document.getElementById('chart-daily');
    if (!canvas) return;
    var labels = (rows || []).map(function (r) { return fmtAxisLabel(r.dt); });
    var data = (rows || []).map(function (r) {
      if (r.used_today === null || r.used_today === undefined) return null;
      var v = Number(r.used_today);
      return (isNaN(v) || v < 0) ? null : v;
    });
    var rawDtList = (rows || []).map(function (r) {
      return r && r.dt ? String(r.dt) : '';
    });
    if (dailyChartInstance) {
      withViewTransition(function () {
        dailyChartInstance.data.labels = labels;
        dailyChartInstance.data.datasets[0].data = data;
        dailyChartInstance.update();
      });
      return;
    }
    var textSecondary = readCssVar('--text-secondary', '#a1a1aa');
    var borderColor = readCssVar('--border', 'rgba(255,255,255,0.08)');
    var tooltipBg = readCssVar('--bg-card', '#131316');
    var tooltipFg = readCssVar('--text-primary', '#fafafa');
    dailyChartInstance = new Chart(canvas.getContext('2d'), {
      type: 'bar',
      data: {
        labels: labels,
        datasets: [{
          label: '每日用电 (kW·h)',
          data: data,
          borderRadius: 6,
          borderSkipped: false,
          backgroundColor: function (context) {
            var chart = context.chart;
            var c = chart.ctx;
            var area = chart.chartArea;
            if (!area) return 'rgba(99,102,241,0.6)';
            var g = c.createLinearGradient(0, area.top, 0, area.bottom);
            g.addColorStop(0, 'rgba(99,102,241,0.85)');
            g.addColorStop(1, 'rgba(99,102,241,0.45)');
            return g;
          },
          borderColor: 'rgba(99,102,241,0.9)',
          borderWidth: 1,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 350 },
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: tooltipBg,
            borderColor: borderColor,
            borderWidth: 1,
            titleColor: '#a1a1aa',
            bodyColor: '#fafafa',
            padding: 10,
            displayColors: false,
            callbacks: {
              title: function (items) {
                if (!items || !items.length) return '';
                var idx = items[0].dataIndex;
                return rawDtList[idx] || labels[idx] || '';
              },
              label: function (item) {
                var v = item.parsed.y;
                return (v == null ? '—' : v.toFixed(2) + ' kW·h');
              }
            }
          }
        },
        scales: {
          x: {
            grid: { display: false },
            ticks: { color: '#71717a', font: { size: 10 }, maxRotation: 0, autoSkipPadding: 16 },
            border: { color: borderColor }
          },
          y: {
            beginAtZero: true,
            grid: { color: 'rgba(255,255,255,0.04)', drawBorder: false },
            border: { display: false },
            ticks: { color: '#71717a', font: { size: 10 }, callback: function (v) { return Number(v).toFixed(1); } }
          }
        }
      }
    });
  }

  /* === 月度预测卡片更新 (Round 26b) ============================ */
  function updateMonthlyProjection(p) {
    var valEl = document.getElementById('stat-monthly-projection');
    var priceEl = document.getElementById('stat-monthly-eqprice');
    var detailEl = document.getElementById('stat-monthly-detail');
    if (!valEl) return;
    var mp = (p && typeof p.monthly_projection === 'number') ? p.monthly_projection : null;
    var eqprice = p && p.eqprice;
    if (mp === null) {
      valEl.classList.add('is-empty');
      var bd = (p && p.monthly_breakdown) || {};
      var usedKwh = (bd.used_kwh != null) ? Number(bd.used_kwh) : null;
      var eqpriceNum = (p && p.eqprice != null) ? Number(p.eqprice) : null;
      if (usedKwh != null && usedKwh > 0 && eqpriceNum != null && eqpriceNum > 0) {
        valEl.innerHTML = '¥' + (usedKwh * eqpriceNum).toFixed(2);
        if (priceEl) priceEl.textContent = '';
        if (detailEl) detailEl.textContent = '已用 ' + usedKwh.toFixed(2) + ' kW·h（日均数据积累中）';
      } else {
        valEl.innerHTML = '¥—';
        if (priceEl) priceEl.textContent = '';
        if (detailEl) detailEl.textContent = '日均数据积累中';
      }
    } else {
      valEl.classList.remove('is-empty');
      valEl.innerHTML = '¥' + mp.toFixed(2) +
        (eqprice ? ' <span style="font-size:14px;color:var(--text-secondary)">@¥' + Number(eqprice).toFixed(3) + '/kW·h</span>' : '');
      if (detailEl) {
        var bd = (p && p.monthly_breakdown) || {};
        var used = (bd.used_kwh != null) ? bd.used_kwh.toFixed(2) : '0';
        var avg = (bd.avg_daily != null) ? bd.avg_daily.toFixed(2) : '—';
        var left = bd.days_left != null ? bd.days_left : '—';
        detailEl.textContent = '已用 ' + used + ' · 日均 ' + avg + ' · 剩 ' + left + ' 天';
      }
    }
  }

  /* === Skeleton 切换 ========================================= */
  function showSkeletons(on) {
    var cards = document.querySelectorAll('.card');
    for (var i = 0; i < cards.length; i++) {
      cards[i].style.transition = 'opacity 0.2s';
      cards[i].style.opacity = on ? '0.5' : '1';
    }
  }
  function showForceRefreshStatus(message) {
    var btn = document.getElementById('refresh-btn');
    if (!btn) return;
    if (message) {
      btn.disabled = true;
      btn.classList.add('loading');
    } else {
      btn.disabled = false;
      btn.classList.remove('loading');
    }
  }

  /* === 电表 cell 更新 ========================================== */
  function updateMeter(rs) {
    var setCell = function (id, text, unit, decimals) {
      var el = document.getElementById(id);
      if (!el) return;
      if (text === null || text === undefined || (typeof text === 'number' && isNaN(text))) {
        el.innerHTML = '—';
        el.classList.add('is-empty');
      } else {
        el.classList.remove('is-empty');
        el.innerHTML = Number(text).toFixed(decimals == null ? 2 : decimals) +
          (unit ? ' <span style="font-size:14px;color:var(--text-secondary)">' + unit + '</span>' : '');
      }
    };
    setCell('meter-vol', rs.vol, 'V', 2);
    setCell('meter-cur', rs.cur, 'A', 2);
    setCell('meter-yggl', rs.yggl, 'W', 3);
    var pillEl = document.getElementById('meter-status-pill');
    if (pillEl) {
      var label = rs.run_status || '—';
      pillEl.textContent = label;
      if (label === '在线' || label === '正常' || label === '通讯正常') {
        pillEl.style.color = 'var(--online)';
      } else if (label === '—') {
        pillEl.style.color = 'var(--warning)';
      } else {
        pillEl.style.color = 'var(--offline)';
      }
    }
    var updEl = document.getElementById('meter-update-dt');
    if (updEl) {
      updEl.textContent = rs.update_dt || '—';
      updEl.classList.toggle('is-empty', !rs.update_dt);
    }
  }

  /* === 数据流编排 ============================================== */
  function updateAll(payload) {
    var stats = payload.stats || {};
    var rows = payload.rows || [];
    var pill = document.getElementById('status-pill');
    if (pill) {
      pill.setAttribute('data-ts', rows.length ? (rows[rows.length - 1].ts || '') : '');
      setPill();
    }
    var latestTs = rows.length ? rows[rows.length - 1].ts : initialLatestTs;
    setLastUpdate(latestTs);
    setStatText(document.getElementById('stat-remain'), stats.remain, 'kW·h');
    setStatText(document.getElementById('stat-hourly'), stats.hourly_used, 'kW·h');
    setStatText(document.getElementById('stat-readtime'), stats.read_time || null);
    setStatText(document.getElementById('stat-daily'), stats.daily_avg, 'kW·h');
    buildChart(rows);
  }

  function manualRefresh(hours) {
    if (typeof hours === 'number') currentHours = hours;
    var btn = document.getElementById('refresh-btn');
    if (btn) {
      btn.disabled = true;
      btn.classList.add('loading');
    }
    fetch('/api/data?hours=' + currentHours, { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (payload) { updateAll(payload); })
      .catch(function (e) {
        if (typeof showToast === 'function') {
          showToast('刷新失败: ' + (e && e.message || '网络错误'), 'error');
        }
      })
      .then(function () {
        if (btn) {
          btn.disabled = false;
          btn.classList.remove('loading');
        }
      });
  }

  /* === Force refresh (Round 26b + R64 401 fix) ==================== */
  function forceRefresh(triggeredByUser) {
    showSkeletons(true);
    showForceRefreshStatus('🔄 正在抓取最新数据…');
    if (triggeredByUser && typeof showToast === 'function') showToast('刷新中…', 'info', 1500);

    var refreshDone = function () {
      return Promise.all([
        fetch('/api/live', { credentials: 'same-origin' }).then(function (r) {
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.json();
        }),
        fetch('/api/data?hours=' + currentHours, { credentials: 'same-origin' }).then(function (r) {
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.json();
        }),
      ]).then(function (results) {
        var live = results[0] || {};
        var data = results[1] || {};
        applyLiveUpdate(live);
        if (data && data.rows) buildChart(data.rows);
      });
    };

    // R64 — /api/refresh is now @require_auth(role='admin').  Before
    // POSTing we peek at /api/auth/me to confirm we're logged in; if
    // we get a 401 we bounce to the login page instead of letting
    // /api/refresh return 401 (which would surface as a confusing
    // "抓取失败, 使用最近缓存" toast).
    fetch('/api/auth/me', { credentials: 'same-origin' })
      .then(function (r) { return { ok: r.ok, status: r.status }; })
      .then(function (probe) {
        if (!probe.ok || probe.status === 401) {
          if (typeof showToast === 'function') {
            showToast('请登录后再手动抓取', 'warning', 2500);
          }
          // Give the toast time to paint, then navigate.
          setTimeout(function () { location.href = '/login'; }, 800);
          return null;
        }
        return fetch('/api/refresh', {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'X-CSRF-Token': readCsrfToken() },
        }).then(function (r) {
          return r.json().then(function (j) { return { ok: r.ok, j: j }; });
        });
      })
      .then(function (resp) {
        if (!resp) return null;  // bounced to login
        if (!resp.ok || !resp.j || resp.j.ok !== true) {
          var errMsg = (resp.j && resp.j.error) ? resp.j.error : 'HTTP 失败';
          // /api/refresh requires CSRF too — surface a clearer hint.
          if (errMsg === 'csrf_missing' || errMsg === 'csrf_invalid') {
            throw new Error('页面会话已过期，请刷新后重试');
          }
          throw new Error(errMsg);
        }
        if (typeof showToast === 'function') {
          showToast(
            '✓ 抓取完成 · ' + (resp.j.scrape_status || 'ok') +
            (resp.j.ts ? ' · ' + resp.j.ts : ''),
            'success', 2500
          );
        }
        return refreshDone();
      })
      .catch(function (e) {
        if (typeof showToast === 'function') {
          showToast('抓取失败,使用最近缓存: ' + (e && e.message || ''), 'error', 4000);
        }
        return refreshDone().catch(function () { /* ignore */ });
      })
      .then(function () {
        showSkeletons(false);
        showForceRefreshStatus(null);
      });
  }

  /* R64 — read the CSRF token from the <meta> tag planted by
   * web.py's @context_processor.  Returns '' when no token exists so
   * the call site can decide whether to short-circuit. */
  function readCsrfToken() {
    var m = document.querySelector('meta[name="csrf-token"]');
    return m ? (m.getAttribute('content') || '') : '';
  }

  /* === 30 秒轮询 /api/live ===================================== */
  function autorefreshLive() {
    fetch('/api/live', { credentials: 'same-origin' })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (payload) {
        pollFailStreak = 0;
        applyLiveUpdate(payload);
      })
      .catch(function (e) {
        pollFailStreak += 1;
        if (pollFailStreak === 3 && typeof showToast === 'function') {
          showToast('连续 3 次自动刷新失败: ' + (e && e.message || ''), 'error', 5000);
        }
      });
  }
  setInterval(autorefreshLive, 30000);

  function applyLiveUpdate(payload) {
    if (!payload) return;
    var pill = document.getElementById('status-pill');
    if (pill && payload.latest_ts) {
      pill.setAttribute('data-ts', payload.latest_ts);
      setPill();
    }
    if (payload.latest_ts) setLastUpdate(payload.latest_ts);
    var stats = payload.stats || {};
    setStatText(document.getElementById('stat-remain'), stats.remain, 'kW·h');
    setStatText(document.getElementById('stat-hourly'), stats.hourly_used, 'kW·h');
    setStatText(document.getElementById('stat-readtime'), stats.read_time || null);
    setStatText(document.getElementById('stat-daily'), stats.daily_avg, 'kW·h');
    updateMonthlyProjection(payload);
    if (payload.run_status) updateMeter(payload.run_status);
    fetch('/api/data?hours=' + currentHours, { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (p) { if (p && p.rows) buildChart(p.rows); })
      .catch(function () { /* swallow chart refresh errors */ });
  }

  /* === Section 切换（顶 nav）==================================== */
  function activateSection(section) {
    if (!section) return;
    var apply = function () {
      document.querySelectorAll('[data-section]').forEach(function (el) {
        el.classList.toggle('active', el.getAttribute('data-section') === section);
      });
      requestAnimationFrame(function () {
        var activeSection = document.querySelector('.section.active');
        if (!activeSection) return;
        if (chartInstance && activeSection.contains(document.getElementById('chart'))) {
          chartInstance.resize();
        }
        if (dailyChartInstance && activeSection.contains(document.getElementById('chart-daily'))) {
          dailyChartInstance.resize();
        }
      });
      try { localStorage.setItem(STORAGE_KEY, section); } catch (e) { /* ignore */ }
    };
    // Wrap class toggling in a View Transition so the tab fade plays.
    withViewTransition(apply);
  }
  document.body.addEventListener('click', function (e) {
    var trigger = e.target.closest('.tab');
    if (!trigger) return;
    e.preventDefault();
    var section = trigger.getAttribute('data-section');
    if (section) activateSection(section);
  });

  /* === 时间范围按钮 ============================================ */
  document.querySelectorAll('.range-btn').forEach(function (b) {
    b.addEventListener('click', function () {
      var h = parseInt(b.getAttribute('data-hours'), 10);
      if (!h) return;
      document.querySelectorAll('.range-btn').forEach(function (x) {
        x.classList.remove('active');
      });
      b.classList.add('active');
      manualRefresh(h);
    });
  });

  /* === Round 33d: 采集记录时间范围过滤 =========================== */
  (function () {
    var startInput = document.getElementById('records-start');
    var endInput = document.getElementById('records-end');
    var queryBtn = document.getElementById('records-query');
    var resetBtn = document.getElementById('records-reset');
    var subLabel = document.getElementById('records-sub');
    var rangeLabel = document.getElementById('records-range-label');
    var tbody = document.getElementById('records-tbody');
    var emptyState = document.getElementById('records-empty');

    if (!startInput || !endInput || !queryBtn) return;

    function pad(n) { return n < 10 ? '0' + n : '' + n; }
    function toLocalISO(d) {
      return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate())
        + 'T' + pad(d.getHours()) + ':' + pad(d.getMinutes());
    }

    (function setDefaultRange() {
      var now = new Date();
      var startOfDay = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 0, 0, 0);
      startInput.value = toLocalISO(startOfDay);
      endInput.value = toLocalISO(now);
    })();

    function escapeHtml(s) {
      return String(s == null ? '' : s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }

    function renderRows(rows) {
      if (!tbody) {
        if (subLabel) subLabel.textContent = '共 ' + (rows ? rows.length : 0) + ' 条';
        return;
      }
      if (!rows || !rows.length) {
        tbody.innerHTML = '';
        if (emptyState) emptyState.style.display = 'block';
        if (subLabel) subLabel.textContent = '共 0 条';
        return;
      }
      if (emptyState) emptyState.style.display = 'none';
      // Prefer the Web Component route when available — it animates the
      // re-render via document.startViewTransition and emits
      // <dorm-history-row> children.
      var recordsTable = tbody.closest('dorm-records-table');
      if (recordsTable && typeof recordsTable.rows !== 'undefined') {
        withViewTransition(function () { recordsTable.rows = rows; });
        if (subLabel) subLabel.textContent = '共 ' + rows.length + ' 条';
        return;
      }
      var html = '';
      rows.forEach(function (r) {
        var remain = (r.remain != null && !isNaN(r.remain))
          ? Number(r.remain).toFixed(2) : '—';
        html += '<tr>'
          + '<td class="ts">' + escapeHtml(r.ts || '—') + '</td>'
          + '<td class="ts">' + escapeHtml(r.read_time || '—') + '</td>'
          + '<td class="text-end">' + remain + '</td>'
          + '</tr>';
      });
      tbody.innerHTML = html;
      if (subLabel) subLabel.textContent = '共 ' + rows.length + ' 条';
    }

    function queryRecords() {
      var s = startInput.value;
      var e = endInput.value;
      if (!s || !e) {
        if (typeof showToast === 'function') {
          showToast('请填写起始和截止时间', 'error');
        }
        return;
      }
      // R46 — compare as Asia/Shanghai wall clock (datetime-local is TZ-naive)
      if (new Date(s + '+08:00') > new Date(e + '+08:00')) {
        if (typeof showToast === 'function') {
          showToast('起始时间不能晚于截止时间', 'error');
        }
        return;
      }
      // R46 — replace T with space (records.ts stored as "YYYY-MM-DD HH:MM:SS")
      var startStr = (s.length === 16 ? s + ':00' : s).replace('T', ' ');
      var endStr = (e.length === 16 ? e + ':59' : e).replace('T', ' ');
      var url = '/api/data?start=' + encodeURIComponent(startStr)
              + '&end=' + encodeURIComponent(endStr);
      if (rangeLabel) {
        rangeLabel.textContent = '查询范围：' + s.replace('T', ' ') + ' ~ ' + e.replace('T', ' ');
      }
      queryBtn.disabled = true;
      fetch(url, { credentials: 'same-origin' }).then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      }).then(function (payload) {
        renderRows((payload && payload.rows) || []);
      }).catch(function (err) {
        if (typeof showToast === 'function') {
          showToast('查询失败：' + (err && err.message || '网络错误'), 'error');
        }
        renderRows([]);
      }).then(function () {
        queryBtn.disabled = false;
      });
    }

    function resetRecords() {
      var now = new Date();
      var startOfDay = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 0, 0, 0);
      startInput.value = toLocalISO(startOfDay);
      endInput.value = toLocalISO(now);
      queryRecords();
    }

    queryBtn.addEventListener('click', queryRecords);
    if (resetBtn) resetBtn.addEventListener('click', resetRecords);
    [startInput, endInput].forEach(function (el) {
      el.addEventListener('change', function () {
        if (startInput.value && endInput.value) queryRecords();
      });
    });
    queryRecords();
  })();

  /* === 手动刷新按钮 ============================================ */
  var refreshBtn = document.getElementById('refresh-btn');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', function () { forceRefresh(true); });
  }

  /* === 主题切换器 (Round 27) — preview_v2 仅深色,保留 applyTheme 钩子 === */
  function applyTheme(themeKey, accent) {
    accent = accent || '#6366f1';
    var root = document.documentElement;
    root.style.setProperty('--accent', accent);
    root.style.setProperty('--accent-soft', 'rgba(99,102,241,0.10)');
    root.style.setProperty('--accent-hover', '#818cf8');
    try {
      if (chartInstance) chartInstance.update('none');
      if (dailyChartInstance) dailyChartInstance.update('none');
    } catch (e) { /* ignore */ }
  }

  var themeToggle = document.getElementById('theme-toggle');
  if (themeToggle) {
    themeToggle.addEventListener('click', function () {
      var current = document.documentElement.style.getPropertyValue('--accent') || '#6366f1';
      applyTheme('dark', current === '#6366f1' ? '#22c55e' : '#6366f1');
    });
  }

  /* === 键盘快捷键 ============================================ */
  document.addEventListener('keydown', function (e) {
    var tag = (e.target && e.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA') return;
    var key = e.key;
    var sectionMap = { '1': 'overview', '2': 'history', '3': 'finance', '4': 'meter' };
    if (sectionMap[key]) {
      e.preventDefault();
      activateSection(sectionMap[key]);
      return;
    }
    if (key === 'r' || key === 'R') {
      e.preventDefault();
      manualRefresh();
      return;
    }
  });

  /* === 初始化 ================================================ */
  setPill();
  setLastUpdate(initialLatestTs);
  buildChart(initialRows);
  buildDailyChart(initialDaily);
  showSkeletons(true);
  forceRefresh(false);
  try {
    var saved = localStorage.getItem(STORAGE_KEY);
    if (saved && saved !== 'overview') activateSection(saved);
  } catch (e) { /* ignore */ }
})();
