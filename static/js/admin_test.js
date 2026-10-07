/* admin_test.js — Round 64.
 *
 * Manual-trigger page (test-scrape + test-push).
 * - Both buttons trigger a CSRF-protected POST.
 * - Result toasts render via <dorm-toast>.
 * - Stat cards are best-effort summaries populated from /api/live.
 */
(function () {
  'use strict';

  var INITIAL = window.__ADMIN_TEST_INITIAL__ || {};
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
    t.setAttribute('duration', '4000');
    t.textContent = msg;
    var stack = document.getElementById('toast-stack');
    if (stack) stack.appendChild(t); else document.body.appendChild(t);
  }
  function $(id) { return document.getElementById(id); }
  function highlightTab() {
    document.querySelectorAll('.admin-tab').forEach(function (t) {
      t.classList.toggle('active', t.getAttribute('data-tab') === 'test');
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

  function runPush() {
    apiFetch('/api/admin/test-push', { method: 'POST' })
      .then(function (resp) {
        if (resp.ok && resp.json && resp.json.ok) {
          var notice = resp.json.notice ? '（' + resp.json.notice + '）' : '';
          showToast('已发送 ' + notice, 'success');
        } else {
          showToast((resp.json && resp.json.error) || '推送失败', 'error');
        }
      });
  }
  function runScrape() {
    var slot = $('action-result');
    slot.textContent = '抓取中…';
    apiFetch('/api/admin/test-scrape', { method: 'POST' })
      .then(function (resp) {
        if (resp.ok && resp.json && resp.json.ok) {
          slot.textContent = '';
          showToast('抓取成功 · ' + (resp.json.scrape_status || 'ok'), 'success');
          refreshSummary();
        } else {
          slot.textContent = '';
          showToast((resp.json && resp.json.error) || '抓取失败', 'error');
        }
      });
  }
  function refreshSummary() {
    fetch('/api/live', { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        $('stat-last-scrape').textContent = data.latest_ts || '—';
      })
      .catch(function () { /* ignore */ });
  }

  document.addEventListener('DOMContentLoaded', function () {
    highlightTab();
    fillUserLabel();
    wireLogout();
    $('run-push').addEventListener('click', runPush);
    $('run-scrape').addEventListener('click', runScrape);
    refreshSummary();
  });
})();