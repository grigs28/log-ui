/* overview charts — expects global `D` (injected by template) */
(function(){
var cs=getComputedStyle(document.body);
var ax=cs.getPropertyValue('--muted').trim()||'#64748b';
var accent=cs.getPropertyValue('--accent').trim()||'#38bdf8';
var errC=cs.getPropertyValue('--error').trim()||'#f87171';
var warnC=cs.getPropertyValue('--warning').trim()||'#fbbf24';
var infoC=cs.getPropertyValue('--info').trim()||'#38bdf8';
var mutedC=cs.getPropertyValue('--muted').trim()||'#64748b';

function bar(id,dict,color){
  var e=Object.entries(dict||{}).sort(function(a,b){return b[1]-a[1]}).slice(0,12);
  echarts.init(document.getElementById(id)).setOption({
    grid:{left:10,right:30,top:10,bottom:5,containLabel:true},
    xAxis:{type:'value',axisLine:{lineStyle:{color:ax}},splitLine:{lineStyle:{color:ax,opacity:.15}}},
    yAxis:{type:'category',data:e.map(function(x){return x[0]}).reverse(),axisLine:{lineStyle:{color:ax}}},
    series:[{type:'bar',data:e.map(function(x){return x[1]}).reverse(),itemStyle:{color:color}}],
    tooltip:{trigger:'axis'}
  });
}

var tr=(D.trend||[]).map(function(p){return [p[0]*1000,p[1]]});
echarts.init(document.getElementById('trend')).setOption({
  grid:{left:10,right:20,top:10,bottom:30,containLabel:true},color:[accent],
  xAxis:{type:'time',axisLine:{lineStyle:{color:ax}}},
  yAxis:{type:'value',axisLine:{lineStyle:{color:ax}},splitLine:{lineStyle:{color:ax,opacity:.15}}},
  series:[{type:'line',data:tr,showSymbol:false,areaStyle:{opacity:.2,color:accent}}],
  tooltip:{trigger:'axis'}
});

var lv=Object.entries(D.by_level||{}).map(function(kv){return {name:kv[0],value:kv[1]}});
var lvc={error:errC,warning:warnC,info:infoC,debug:mutedC};
echarts.init(document.getElementById('level')).setOption({
  series:[{type:'pie',radius:['45%','70%'],data:lv,color:lv.map(function(x){return lvc[x.name]||mutedC}),label:{color:ax}}],
  legend:{bottom:0,textStyle:{color:ax}}
});

bar('host',D.by_host,accent);
bar('errhost',D.errors_by_host,errC);
})();
