/* log search auto-refresh */
(function(){
  var sel=document.getElementById('refresh');
  if(!sel) return;
  var timer=null;
  function fieldVal(name){
    var el=document.querySelector('[name="'+name+'"]');
    return el&&el.value ? el.value : '';
  }
  function pollUrl(){
    var p=new URLSearchParams();
    ['host','level','q','limit'].forEach(function(n){var v=fieldVal(n);if(v)p.set(n,v)});
    return '/logs?'+p.toString();
  }
  function tick(){
    fetch(pollUrl(),{headers:{'X-Requested-With':'XMLHttpRequest'}})
      .then(function(r){return r.ok?r.text():Promise.reject();})
      .then(function(html){
        document.getElementById('logs').innerHTML=html;
        var t=document.getElementById('rtime');
        if(t)t.textContent='更新: '+new Date().toLocaleTimeString();
      }).catch(function(){});
  }
  function go(sec){if(timer){clearInterval(timer);timer=null;}if(sec>0){timer=setInterval(tick,sec*1000)}}
  sel.addEventListener('change',function(){go(parseInt(sel.value)||0)});
  go(parseInt(sel.value)||0);
})();
