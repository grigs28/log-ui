/* live tail — WebSocket */
var ws=new WebSocket((location.protocol==='https:'?'wss':'ws')+'://'+location.host+'/ws/tail');
var pane=document.getElementById('pane');
var tb=document.getElementById('stream');
var st=document.getElementById('status');
var MAX=300;

ws.onopen =function(){st.textContent='● 已连接';st.style.color='var(--ok,var(--success))'};
ws.onclose=function(){st.textContent='○ 已断开（刷新重连）';st.style.color='var(--err,var(--error))'};
ws.onerror=function(){st.textContent='连接错误';st.style.color='var(--err,var(--error))'};

ws.onmessage=function(ev){
  var d=JSON.parse(ev.data);
  var tr=tb.insertRow(-1);
  tr.innerHTML='<td class="time">'+h(d.time)+'</td>'
    +'<td><span class="lv lv-'+h(d.level)+'">'+h(d.level)+'</span></td>'
    +'<td>'+h(d.host)+'</td><td class="meta">'+h(d.app)+'</td>'
    +'<td class="msg">'+h(d.msg)+'</td>';
  while(tb.rows.length>MAX+1) tb.deleteRow(1);
  if(document.getElementById('autoscroll').checked) pane.scrollTop=pane.scrollHeight;
};

function clr(){while(tb.rows.length>1)tb.deleteRow(1)}
var h = escapeHtml;
function escapeHtml(s){return (s||'').replace(/[&<>]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c]})}
