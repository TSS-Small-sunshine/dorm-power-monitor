/* login.js — Round 64.
 *
 * AJAX login submission:
 *   - Reads CSRF token from window.__LOGIN_INITIAL__.csrf_token.
 *   - Sends { username, password, csrf_token } JSON body.
 *   - On 200 ok:true → redirect /admin.
 *   - On 401 / 403 / 4xx → render inline error in the card.
 *
 * Fallback: if the user has JS disabled, the form has method=POST
 * action=/login and the server-side route handles it (legacy form
 * encoded path).
 */
(function () {
  'use strict';

  var INITIAL = window.__LOGIN_INITIAL__ || {};
  var CSRF = INITIAL.csrf_token || readMetaCsrf();

  function readMetaCsrf() {
    var m = document.querySelector('meta[name="csrf-token"]');
    return m ? (m.getAttribute('content') || '') : '';
  }
  function $(id) { return document.getElementById(id); }

  function showInlineError(msg) {
    var el = $('login-error');
    if (!el) return;
    el.textContent = msg;
    el.hidden = false;
  }
  function clearError() {
    var el = $('login-error');
    if (!el) return;
    el.textContent = '';
    el.hidden = true;
  }

  function submit(ev) {
    if (ev) ev.preventDefault();
    clearError();
    var u = $('login-username');
    var p = $('login-password');
    if (!u || !p || !u.value || !p.value) {
      showInlineError('请输入用户名与密码');
      return;
    }
    var btn = $('login-submit');
    if (btn) { btn.disabled = true; btn.classList.add('loading'); }
    fetch('/api/auth/login', {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': CSRF,
      },
      body: JSON.stringify({
        username: u.value.trim(),
        password: p.value,
        csrf_token: CSRF,
      }),
    }).then(function (r) {
      return r.json().then(function (j) {
        return { ok: r.ok, status: r.status, json: j };
      });
    }).then(function (resp) {
      if (resp.ok && resp.json && resp.json.ok) {
        // Plant the cookie + redirect to /admin.
        location.href = '/admin';
      } else {
        var err = (resp.json && resp.json.error) || '登录失败';
        if (err === 'locked') showInlineError('账号已锁定，请 15 分钟后再试');
        else if (err === 'invalid_credentials') showInlineError('用户名或密码错误');
        else if (err === 'csrf_invalid' || err === 'csrf_missing') showInlineError('CSRF 校验失败，请刷新页面重试');
        else showInlineError(err);
        if (btn) { btn.disabled = false; btn.classList.remove('loading'); }
      }
    }).catch(function () {
      showInlineError('网络错误');
      if (btn) { btn.disabled = false; btn.classList.remove('loading'); }
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    var form = $('login-form');
    if (form) form.addEventListener('submit', submit);
    // Allow Enter inside the password field to submit.
    var pwd = $('login-password');
    if (pwd) pwd.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') submit(e);
    });
  });
})();