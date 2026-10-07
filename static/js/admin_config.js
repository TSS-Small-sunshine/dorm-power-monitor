/* admin_config.js — Round 64.
 *
 * System config page (scrape + push + admin-password).
 * - Read CSRF token from window.__ADMIN_CONFIG_INITIAL__.csrf_token.
 * - Hydrate form fields from the server-side initial_data snapshot.
 * - Submit via PUT /api/admin/config with X-CSRF-Token header.
 * - Stat cards reflect the live values after a successful save.
 */
(function () {
  'use strict';

  var INITIAL = window.__ADMIN_CONFIG_INITIAL__ || {};
  var CSRF = INITIAL.csrf_token || readMetaCsrf();

  function withViewTransition(cb) {
    if (typeof document.startViewTransition === 'function') {
      try { document.startViewTransition(cb); return; } catch (e) { /* fall */ }
    }
    cb();
  }
  function readMetaCsrf() {
    var m = document.querySelector('meta[name="csrf-token"]');
    return m ? (m.getAttribute('content') || '') : '';
  }
  function apiFetch(path, opts) {
    opts = opts || {};
    opts.credentials = 'same-origin';
    if (!opts.headers) opts.headers = {};
    if ((opts.method || 'GET').toUpperCase() !== 'GET') {
      opts.headers['X-CSRF-Token'] = CSRF;
    }
    opts.headers['Content-Type'] = opts.headers['Content-Type'] || 'application/json';
    return fetch(path, opts).then(function (r) {
      return r.json().then(function (j) {
        return { ok: r.ok, status: r.status, json: j };
      });
    });
  }
  function showToast(msg, kind) {
    var t = document.createElement('dorm-toast');
    t.setAttribute('kind', kind || 'info');
    t.setAttribute('duration', '3000');
    t.textContent = msg;
    var stack = document.getElementById('toast-stack');
    if (stack) stack.appendChild(t); else document.body.appendChild(t);
  }
  function $(id) { return document.getElementById(id); }
  function highlightTab() {
    document.querySelectorAll('.admin-tab').forEach(function (t) {
      t.classList.toggle('active', t.getAttribute('data-tab') === 'config');
    });
  }
  function fillUserLabel() {
    var el = $('admin-user-label');
    if (el && INITIAL.current_user) {
      el.textContent = '· ' + INITIAL.current_user +
        (INITIAL.current_role ? ' (' + INITIAL.current_role + ')' : '');
    }
  }
  function wireLogout() {
    var btn = $('logout-btn');
    if (!btn) return;
    btn.addEventListener('click', function () {
      apiFetch('/api/auth/logout', { method: 'POST' }).then(function () {
        location.href = '/login';
      });
    });
  }

  // Field mappings: form id → API key
  var SCRAPE_FIELDS = {
    'cfg-base-url': 'dorm_base_url',
    'cfg-openid': 'dorm_openid',
    'cfg-room-id': 'dorm_room_id',
    'cfg-eqprice': 'eqprice',
    'cfg-webhook': 'feishu_webhook_url',
  };
  var PUSH_FIELDS = {
    'cfg-l2': 'push_l2_enable',
    'cfg-daily': 'push_daily_enable',
    'cfg-weekly': 'push_weekly_enable',
    'cfg-monthly': 'push_monthly_enable',
    'cfg-daily-time': 'push_daily_time',
    'cfg-weekly-time': 'push_weekly_time',
    'cfg-monthly-time': 'push_monthly_time',
    'cfg-quiet-start': 'quiet_hours_start',
    'cfg-quiet-end': 'quiet_hours_end',
  };

  function hydrate() {
    var data = INITIAL.data || {};
    var scrape = data.scrape || {};
    Object.keys(SCRAPE_FIELDS).forEach(function (id) {
      var el = $(id); if (!el) return;
      el.value = scrape[SCRAPE_FIELDS[id]] || '';
    });
    var push = data.push || {};
    Object.keys(PUSH_FIELDS).forEach(function (id) {
      var el = $(id); if (!el) return;
      el.value = push[PUSH_FIELDS[id]] || '';
    });
    // Stat cards
    $('stat-eqprice').textContent = (scrape.eqprice || '0.5') + ' ¥';
    $('stat-daily').textContent = (push.push_daily_enable === '1' ? '开启' : '关闭');
    $('stat-pwd').textContent = '已设置';
  }

  function saveScrape() {
    var body = { scrape: {} };
    Object.keys(SCRAPE_FIELDS).forEach(function (id) {
      var el = $(id); if (!el) return;
      body.scrape[SCRAPE_FIELDS[id]] = el.value;
    });
    submit(body, '抓包配置已保存');
  }
  function savePush() {
    var body = { push: {} };
    Object.keys(PUSH_FIELDS).forEach(function (id) {
      var el = $(id); if (!el) return;
      body.push[PUSH_FIELDS[id]] = el.value;
    });
    submit(body, '推送配置已保存');
  }
  function savePwd() {
    var pwd = $('cfg-admin-pwd').value;
    if (!pwd) { showToast('请输入新密码', 'error'); return; }
    if (pwd.length < 8) { showToast('密码至少 8 位', 'error'); return; }
    submit({ admin_password: pwd }, '密码已更新');
    $('cfg-admin-pwd').value = '';
  }
  function submit(payload, successMsg) {
    apiFetch('/api/admin/config', {
      method: 'PUT', body: JSON.stringify(payload),
    }).then(function (resp) {
      if (resp.ok && resp.json && resp.json.ok) {
        showToast(successMsg, 'success');
      } else {
        showToast((resp.json && resp.json.error) || '保存失败', 'error');
      }
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    highlightTab();
    fillUserLabel();
    wireLogout();
    hydrate();
    $('save-scrape').addEventListener('click', saveScrape);
    $('save-push').addEventListener('click', savePush);
    $('save-pwd').addEventListener('click', savePwd);
  });
})();