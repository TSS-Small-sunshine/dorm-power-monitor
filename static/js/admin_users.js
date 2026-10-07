/* admin_users.js — Round 64.
 *
 * User CRUD page module:
 *   - List / create / update / delete users via the JSON API.
 *   - Read CSRF token from window.__ADMIN_USERS_INITIAL__.csrf_token.
 *   - All fetch() calls carry credentials:'same-origin' (R63 cookie
 *     contract) and X-CSRF-Token header on mutating methods.
 *   - Sidebar nav is highlighted via JS — page-level nav is rendered
 *     server-side but we mark the active tab client-side after a View
 *     Transition finishes.
 *   - <dorm-toast> + <dorm-spinner> integration mirrors R66.
 */
(function () {
  'use strict';

  var INITIAL = window.__ADMIN_USERS_INITIAL__ || {};
  var CSRF = INITIAL.csrf_token || readMetaCsrf();
  var currentUserId = null;

  // ---- helpers -----------------------------------------------------------
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
    t.setAttribute('duration', '3500');
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
  function highlightTab() {
    var tabs = document.querySelectorAll('.admin-tab');
    tabs.forEach(function (t) {
      t.classList.toggle('active', t.getAttribute('data-tab') === 'users');
    });
  }
  function fillUserLabel() {
    var el = document.getElementById('admin-user-label');
    if (el && INITIAL.current_user) {
      el.textContent = '· ' + INITIAL.current_user +
        (INITIAL.current_role ? ' (' + INITIAL.current_role + ')' : '');
    }
  }
  function wireLogout() {
    var btn = document.getElementById('logout-btn');
    if (!btn) return;
    btn.addEventListener('click', function () {
      apiFetch('/api/auth/logout', { method: 'POST' }).then(function () {
        location.href = '/login';
      });
    });
  }

  // ---- CRUD --------------------------------------------------------------
  function loadUsers() {
    var tbody = document.getElementById('users-tbody');
    if (!tbody) return;
    tbody.innerHTML = '<tr class=empty><td colspan=6>加载中…</td></tr>';
    apiFetch('/api/admin/users').then(function (resp) {
      if (!resp.ok) {
        tbody.innerHTML = '<tr class=empty><td colspan=6>加载失败</td></tr>';
        showToast('加载用户失败', 'error');
        return;
      }
      renderUsers(resp.json.users || []);
    });
  }
  function renderUsers(users) {
    var total = users.length;
    var admins = users.filter(function (u) { return u.role === 'admin'; }).length;
    var viewers = total - admins;
    document.getElementById('stat-total-users').textContent = total;
    document.getElementById('stat-admin-users').textContent = admins;
    document.getElementById('stat-viewer-users').textContent = viewers;

    var tbody = document.getElementById('users-tbody');
    if (!users.length) {
      tbody.innerHTML = '<tr class=empty><td colspan=6>暂无用户</td></tr>';
      return;
    }
    var html = '';
    users.forEach(function (u) {
      html += '<tr data-id="' + u.id + '">'
        + '<td>' + u.id + '</td>'
        + '<td>' + escapeHtml(u.username) + '</td>'
        + '<td><span class="role-tag role-' + escapeHtml(u.role) + '">'
        + escapeHtml(u.role) + '</span></td>'
        + '<td>' + escapeHtml(u.created_at || '') + '</td>'
        + '<td>' + escapeHtml(u.last_login_at || '—') + '</td>'
        + '<td class=actions>'
        +   '<button class="btn-tiny btn-edit" data-id="' + u.id + '">编辑</button>'
        +   '<button class="btn-tiny btn-del" data-id="' + u.id + '">删除</button>'
        + '</td>'
        + '</tr>';
    });
    withViewTransition(function () { tbody.innerHTML = html; });
    bindRowActions(users);
  }
  function bindRowActions(users) {
    document.querySelectorAll('.btn-edit').forEach(function (b) {
      b.addEventListener('click', function () {
        var id = parseInt(b.getAttribute('data-id'), 10);
        var user = users.find(function (u) { return u.id === id; });
        if (user) openEdit(user);
      });
    });
    document.querySelectorAll('.btn-del').forEach(function (b) {
      b.addEventListener('click', function () {
        var id = parseInt(b.getAttribute('data-id'), 10);
        deleteUser(id);
      });
    });
  }
  function openEdit(user) {
    currentUserId = user.id;
    document.getElementById('edit-username').textContent = ' · ' + user.username;
    document.getElementById('ed-password').value = '';
    document.getElementById('ed-role').value = user.role;
    var panel = document.getElementById('edit-panel');
    panel.hidden = false;
    panel.scrollIntoView({ behavior: 'smooth' });
  }
  function closeEdit() {
    currentUserId = null;
    document.getElementById('edit-panel').hidden = true;
  }
  function createUser() {
    var username = document.getElementById('nu-username').value.trim();
    var password = document.getElementById('nu-password').value;
    var role = document.getElementById('nu-role').value;
    var errEl = document.getElementById('nu-error');
    errEl.textContent = '';
    if (!username || !password) {
      errEl.textContent = '用户名与密码必填';
      return;
    }
    apiFetch('/api/admin/users', {
      method: 'POST',
      body: JSON.stringify({
        username: username, password: password, role: role,
      }),
    }).then(function (resp) {
      if (resp.ok && resp.json && resp.json.ok) {
        document.getElementById('nu-username').value = '';
        document.getElementById('nu-password').value = '';
        showToast('已创建 ' + username, 'success');
        loadUsers();
      } else {
        var msg = (resp.json && resp.json.error) || '创建失败';
        errEl.textContent = msg;
        showToast(msg, 'error');
      }
    });
  }
  function saveUser() {
    if (!currentUserId) return;
    var pwd = document.getElementById('ed-password').value;
    var role = document.getElementById('ed-role').value;
    var body = {};
    if (pwd) body.password = pwd;
    body.role = role;
    apiFetch('/api/admin/users/' + currentUserId, {
      method: 'PUT', body: JSON.stringify(body),
    }).then(function (resp) {
      if (resp.ok && resp.json && resp.json.ok) {
        showToast('已保存', 'success');
        closeEdit();
        loadUsers();
      } else {
        showToast((resp.json && resp.json.error) || '保存失败', 'error');
      }
    });
  }
  function deleteUser(id) {
    if (!confirm('确定删除该用户？此操作不可撤销。')) return;
    apiFetch('/api/admin/users/' + id, { method: 'DELETE' })
      .then(function (resp) {
        if (resp.ok && resp.json && resp.json.ok) {
          showToast('已删除', 'success');
          loadUsers();
        } else {
          showToast((resp.json && resp.json.error) || '删除失败', 'error');
        }
      });
  }

  // ---- wire --------------------------------------------------------------
  document.addEventListener('DOMContentLoaded', function () {
    highlightTab();
    fillUserLabel();
    wireLogout();
    var createBtn = document.getElementById('nu-create');
    if (createBtn) createBtn.addEventListener('click', createUser);
    var saveBtn = document.getElementById('ed-save');
    if (saveBtn) saveBtn.addEventListener('click', saveUser);
    var cancelBtn = document.getElementById('ed-cancel');
    if (cancelBtn) cancelBtn.addEventListener('click', closeEdit);
    loadUsers();
  });
})();