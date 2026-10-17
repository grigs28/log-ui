/* 网管中心全局顶栏渲染（各系统端口自动高亮，链接走 /go 继承 SSO 免密） */
(function () {
  if (window.top !== window.self || document.getElementById('nm-topbar-inner')) return;
  var GW = 'http://192.168.0.6';   // ← log-ui 跑在 .14，网关在 .6，不能按 location 推算
  var SYS = [
    ['netbox',   'NetBox',    8100],
    ['librenms', 'LibreNMS',  8101],
    ['zabbix',   'Zabbix',    8102],
    ['portgrid', 'PortGrid',  8103],
    ['netdisco', 'NetDisco',  8104],
    ['semaphore','Semaphore', 8105],
    ['logui',    'LOG-UI',    0, 'http://192.168.0.14/disks']
  ];
  var bar = document.getElementById('nm-topbar');
  if (!bar) return;
  var html = '<div id="nm-topbar-inner" style="display:flex;align-items:center;width:100%">'
    + '<a class="nm-brand" href="' + GW + '/portal">⌂ 网管中心</a>';
  SYS.forEach(function (s) {
    var cur = '';
    if (s[3]) {                       // 外部系统：按主机名判断（logui 指向 .14，没有端口）
      try { if (new URL(s[3]).hostname === location.hostname) cur = ' class="nm-cur"'; } catch (_) {}
    } else if (String(location.port || '80') === String(s[2])) {
      cur = ' class="nm-cur"';
    }
    var href = s[3] ? s[3] : (GW + '/go/' + s[0]);
    html += '<a' + cur + ' id="nm-svc-' + s[0] + '" href="' + href + '">'
          + s[1] + '<i class="nm-dot" title="检测中…"></i></a>';
  });
  html += '<span class="nm-right">'
    + '<a href="' + GW + '/portal">门户</a>'
    + '<a href="' + GW + '/portal/status">状态</a>'
    + '<a class="nm-logout" href="' + GW + '/logout">退出</a>'
    + '</span></div>';
  bar.innerHTML = html;

  // ── 服务健康徽章：每 60 秒检测一次 /portal/status ──
  function paint(status) {
    SYS.forEach(function (s) {
      var a = document.getElementById('nm-svc-' + s[0]);
      if (!a) return;
      var dot = a.querySelector('.nm-dot');
      if (!dot) return;
      if (!status) { dot.className = 'nm-dot'; dot.title = '状态未知（检测失败）'; return; }
      var ok = status[s[0]] === true;
      dot.className = 'nm-dot ' + (ok ? 'ok' : 'bad');
      dot.title = s[1] + (ok ? ' 正常' : ' 异常！');
    });
  }
  function refresh() {
    fetch('/portal/status', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(paint)
      .catch(function () { paint(null); });
  }
  refresh();
  setInterval(refresh, 60000);
})();
