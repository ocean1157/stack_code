let signalSort={key:'confidence',direction:-1};
const signalColumns=['hot_rank','name','industry','action','probability','confidence','current_price','pct_change','signal_at'];
function compareSignalValues(a,b){let x=a[signalSort.key],y=b[signalSort.key];if(['hot_rank','probability','confidence','current_price','pct_change'].includes(signalSort.key)){x=Number(x??-Infinity);y=Number(y??-Infinity)}else{x=String(x??'');y=String(y??'')}return(x>y?1:x<y?-1:0)*signalSort.direction}
renderSignals=function(){
  const q=$('#signal-search').value.trim().toLowerCase(),f=$('#signal-filter').value;
  const rows=signals.filter(s=>(f==='ALL'||s.action===f)&&`${s.symbol} ${s.name} ${s.industry}`.toLowerCase().includes(q)).sort(compareSignalValues);
  $('#signals-body').innerHTML=rows.map(s=>`<tr data-symbol="${s.symbol}"><td>#${s.hot_rank}</td><td><strong>${s.symbol}</strong><br><small class="muted">${esc(s.name)}</small></td><td>${esc(s.industry||'—')}</td><td>${badge(s.action)}</td><td>${pct(s.probability)}</td><td>${pct(s.confidence)}</td><td>${num(s.current_price)}</td><td class="${+s.pct_change>=0?'positive':'negative'}">${s.pct_change==null?'—':num(s.pct_change)+'%'}</td><td>${s.signal_at||s.price_date}</td><td><span class="muted">每日热度前十</span></td></tr>`).join('');
  $$('#signals-body tr').forEach(r=>r.onclick=()=>showSymbol(r.dataset.symbol));
  updateSignalSortHeaders();
};
function updateSignalSortHeaders(){
  const headers=$('#signals-body').closest('table').querySelectorAll('thead th');
  headers.forEach((th,i)=>{if(i>=signalColumns.length)return;th.dataset.sort=signalColumns[i];th.classList.add('sortable-heading');const base=th.dataset.label||(th.dataset.label=th.textContent.replace(/[↑↓]$/,'').trim());th.textContent=base+(signalSort.key===signalColumns[i]?(signalSort.direction>0?' ↑':' ↓'):'')});
}
addEventListener('DOMContentLoaded',()=>{
  const head=$('#signals-body').closest('table').querySelector('thead');
  updateSignalSortHeaders();
  head.addEventListener('click',e=>{const th=e.target.closest('th[data-sort]');if(!th)return;const key=th.dataset.sort;signalSort={key,direction:signalSort.key===key?-signalSort.direction:1};renderSignals()});
});
addEventListener('DOMContentLoaded',()=>{
  const tips=['模型完成并写入系统的时间','把上涨概率转换为 BUY/SELL/HOLD 的模型决策边界','回测每个交易日最多选择的优先股票数','样本外收益按一年折算，已计交易成本','单位波动对应的风险调整后收益','样本外净值从高点至后续低点的最大跌幅','样本外上涨/下跌方向判断正确率'];
  $('#runs-body').closest('table').querySelectorAll('thead th').forEach((th,i)=>{th.title=tips[i]||'';if(i===6)th.textContent='样本外方向准确率'});
});
