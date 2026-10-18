/* 用户与权限管理（设置页内嵌）
   搬自网管中心 sso-gateway/users_ui.js，形态与视觉保持一致。
   只改了这几处：
     ① 端点仍是 /portal/users、/portal/users/save —— log-ui 用同样的路径，脚本不用改
     ② 兜底管理员行锁定：显示「管理员（固定）」，不给下拉、不给删除
     ③ 多一列「yz身份」—— 判断「默认(随yz)」档会不会放行，得先看 yz 那边是不是管理员
     ④ 其余用户多一个「删除」按钮（删记录；登录日志作为审计保留）
   另外给用户名/显示名加了转义（原名直接拼 HTML）。 */
(function () {
  var ROLE = { auto: '默认(随yz)', admin: '管理员', user: '用户(禁登)', blocked: '封禁' };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  async function post(url, payload) {
    var r = await (await fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    })).json();
    if (!r.ok) alert(r.msg || '操作失败');
    return r;
  }

  async function loadUsers() {
    var d = await (await fetch('/portal/users')).json();
    var box = document.getElementById('usertbl');
    if (!box) return;
    var h = '<table style="width:100%;border-collapse:collapse;font-size:13px">'
      + '<thead><tr style="color:#9aa7bd;text-align:left"><th style="padding:6px 8px">用户名</th>'
      + '<th>显示名</th><th>yz身份</th><th>最近登录</th><th>尝试</th><th>最近结果</th><th>本地权限</th><th>操作</th></tr></thead><tbody>';
    d.users.forEach(function (u) {
      h += '<tr style="border-top:1px solid rgba(255,255,255,.08)">'
        + '<td style="padding:6px 8px">' + esc(u.username) + '</td>'
        + '<td>' + esc(u.display || '') + '</td>'
        + '<td>' + (u.yz_admin ? '管理员' : '普通') + '</td>'
        + '<td>' + esc(u.last || '') + '</td>'
        + '<td>' + u.attempts + '</td>'
        + '<td>' + esc(u.last_result || '') + (u.last_reason ? ' · ' + esc(u.last_reason) : '') + '</td>';
      if (u.locked) {
        h += '<td style="color:#7aa2ff">管理员（固定）</td><td></td>';
      } else {
        h += '<td><select data-u="' + esc(u.username) + '" style="background:#111827;color:#e5e9f0;'
          + 'border:1px solid #334155;border-radius:6px;padding:4px">';
        Object.keys(ROLE).forEach(function (r) {
          h += '<option value="' + r + '"' + (u.role === r ? ' selected' : '') + '>' + ROLE[r] + '</option>';
        });
        h += '</select></td>'
          + '<td><button class="u-del" data-u="' + esc(u.username) + '" data-n="' + esc(u.display || u.username)
          + '" style="background:#1f2937;color:#ff8a8a;border:1px solid #4b2b2b;border-radius:6px;'
          + 'padding:3px 10px;cursor:pointer;font-size:12px">删除</button></td>';
      }
      h += '</tr>';
    });
    box.innerHTML = h + '</tbody></table>';
    box.querySelectorAll('select').forEach(function (s) {
      s.addEventListener('change', async function (e) {
        await post('/portal/users/save', { username: e.target.dataset.u, role: e.target.value });
        loadUsers();
      });
    });
    box.querySelectorAll('.u-del').forEach(function (b) {
      b.addEventListener('click', async function (e) {
        var name = e.target.dataset.u;
        if (!confirm('删除用户「' + (e.target.dataset.n || name) + '」的本地记录？\n\n登录日志会保留；该用户下次登录会重新出现。')) return;
        await post('/portal/users/delete', { username: name });
        loadUsers();
      });
    });
    var lg = document.getElementById('loginlog');
    if (lg) lg.innerHTML = d.log.map(function (x) {
      return esc(x.ts) + '  ' + esc(x.u) + '  ' + esc(x.result) + '  ' + esc(x.reason || '')
        + (x.ip ? '  ' + esc(x.ip) : '');
    }).join('<br>') || '（暂无记录）';
  }
  loadUsers();
})();
