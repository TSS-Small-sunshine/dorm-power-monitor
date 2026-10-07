function toast(msg, ok) {
  const el = document.getElementById('action-result');
  el.innerHTML = '<div class="toast ' + (ok ? 'ok' : 'err') + '">' + msg + '</div>';
  setTimeout(() => { el.innerHTML = ''; }, 3500);
}
async function loadConfig() {
  const [scrape, push] = await Promise.all([
    fetch('/admin/api/scrape/config').then(r => r.json()),
    fetch('/admin/api/push/config').then(r => r.json()),
  ]);
  for (const k of Object.keys(scrape)) {
    const el = document.getElementById(k);
    if (el) el.value = scrape[k];
  }
  for (const k of Object.keys(push)) {
    const el = document.getElementById(k);
    if (el) el.value = push[k];
  }
}
async function saveScrape() {
  const payload = {};
  ['dorm_base_url','dorm_openid','dorm_room_id','eqprice','feishu_webhook_url']
    .forEach(k => payload[k] = document.getElementById(k).value);
  const r = await fetch('/admin/api/scrape/config', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify(payload),
  });
  toast(r.ok ? '抓包配置已保存' : '保存失败', r.ok);
}
async function savePush() {
  const payload = {};
  ['push_l2_enable','push_daily_enable','push_weekly_enable','push_monthly_enable',
   'push_daily_time','push_weekly_time','push_monthly_time',
   'quiet_hours_start','quiet_hours_end'].forEach(k => {
    const v = document.getElementById(k).value;
    payload[k] = v;
  });
  const r = await fetch('/admin/api/push/config', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify(payload),
  });
  toast(r.ok ? '推送配置已保存' : '保存失败', r.ok);
}
async function saveAdminPwd() {
  const v = document.getElementById('admin_password').value;
  if (!v) { toast('密码不能为空', false); return; }
  const r = await fetch('/admin/api/admin-password', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ admin_password: v }),
  });
  toast(r.ok ? '密码已更新' : '保存失败', r.ok);
}
async function testPush() {
  const r = await fetch('/admin/api/test-push', { method: 'POST' });
  const j = await r.json();
  toast(j.ok ? '已发送' : ('失败：' + (j.error || '')), j.ok);
}
async function forceRefresh() {
  const r = await fetch('/api/refresh', { method: 'POST' });
  const j = await r.json();
  toast(j.ok ? '抓取成功' : ('失败：' + (j.error || '')), j.ok);
}
loadConfig();
