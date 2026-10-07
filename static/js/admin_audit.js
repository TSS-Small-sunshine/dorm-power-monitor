/* admin_audit.js — Round 64.
 *
 * Audit log viewer — paginated + filterable.
 * - Reads CSRF from window.__ADMIN_AUDIT_INITIAL__.csrf_token.
 * - GET /api/admin/audit (still requires X-CSRF-Token per @require_csrf).
 * - Pager: prev / next buttons; current page + total + per_page shown.
 */
(function () {
  'use strict';

  var INITIAL = window.__ADMIN_AUDIT_INITIAL__ || {};
  var CSRF = INITIAL.csrf_token || readMetaCsrf();

  var state = {
    page: 1,
    per_page: 50,
    action: '',
    user_id: '',
    since: '',
  };

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
  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }
  function $(id) { return document.getElementById(id); }
  function highlightTab() {
    document.querySelectorAll('.admin-tab').forEach(function (t) {
      t.classList.toggle('active', t.getAttribute('data-tab') === 'audit');
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

  function buildQuery() {
    var q = '?page=' + encodeURIComponent(state.page)
      + '&per_page=' + encodeURIComponent(state.per_page);
    if (state.action) q += '&action=' + encodeURIComponent(state.action);
    if (state.user_id) q += '&user_id=' + encodeURIComponent(state.user_id);
    if (state.since) q += '&since=' + encodeURIComponent(state.since);
    return q;
  }

  function load() {
    var tbody = $('audit-tbody');
    if (tbody) tbody.innerHTML = '<tr class=empty><td colspan=6>加载中…</td></tr>';
    apiFetch('/api/admin/audit' + buildQuery()).then(function (resp) {
      if (!resp.ok) {
        if (tbody) tbody.innerHTML = '<tr class=empty><td colspan=6>加载失败</td></tr>';
        showToast('加载审计日志失败', 'error');
        return;
      }
      render(resp.json);
    });
  }
  function render(payload) {
    var rows = payload.rows || [];
    var total = payload.total || 0;
    var perPage = payload.per_page || state.per_page;
    var page = payload.page || state.page;
    var totalPages = Math.max(1, Math.ceil(total / perPage));

    $('stat-total-rows').textContent = total;
    $('stat-page-info').textContent = page + ' / ' + perPage;
    $('stat-total-pages').textContent = totalPages;

    var tbody = $('audit-tbody');
    if (!rows.length) {
      tbody.innerHTML = '<tr class=empty><td colspan=6>暂无记录</td></tr>';
    } else {
      var html = '';
      rows.forEach(function (r) {
        html += '<tr>'
          + '<td>' + escapeHtml(r.created_at) + '</td>'
          + '<td>' + escapeHtml(r.user_id == null ? '—' : String(r.user_id)) + '</td>'
          + '<td><span class="action-tag">' + escapeHtml(r.action) + '</span></td>'
          + '<td>' + escapeHtml(r.target || '') + '</td>'
          + '<td>' + escapeHtml(r.ip || '') + '</td>'
          + '<td class=details>' + escapeHtml(r.details || '') + '</td>'
          + '</tr>';
      });
      withViewTransition(function () { tbody.innerHTML = html; });
    }
    $('pg-label').textContent = '第 ' + page + ' / ' + totalPages + ' 页';
    $('pg-prev').disabled = page <= 1;
    $('pg-next').disabled = page >= totalPages;
  }

  function applyFilters() {
    state.action = $('f-action').value.trim();
    state.user_id = $('f-user-id').value.trim();
    state.since = $('f-since').value.trim();
    state.per_page = parseInt($('f-per-page').value, 10) || 50;
    state.page = 1;
    load();
  }
  function gotoPage(delta) {
    state.page = Math.max(1, state.page + delta);
    load();
  }

  document.addEventListener('DOMContentLoaded', function () {
    highlightTab();
    fillUserLabel();
    wireLogout();
    $('f-apply').addEventListener('click', applyFilters);
    $('pg-prev').addEventListener('click', function () { gotoPage(-1); });
    $('pg-next').addEventListener('click', function () { gotoPage(1); });
    load();
  });
})();