const stockCodeInput=$('#stock-analysis-code'),stockCodeName=$('#stock-code-name'),addAnalysisButton=$('#add-analysis-list'),includeState=$('#stock-analysis-include');
const stockPurchasePrice=document.createElement('input');
stockPurchasePrice.id='stock-purchase-price';stockPurchasePrice.type='number';stockPurchasePrice.min='0.001';stockPurchasePrice.step='0.001';stockPurchasePrice.placeholder='购入价';stockPurchasePrice.setAttribute('aria-label','购入价');
includeState.before(stockPurchasePrice);
let resolvedStock=null,profileTimer=null;
async function resolveStockProfile(){
  const symbol=stockCodeInput.value.trim();resolvedStock=null;includeState.checked=false;addAnalysisButton.disabled=true;
  if(!/^\d{6}$/.test(symbol)){stockCodeName.textContent='输入6位代码自动识别名称';return}
  stockCodeName.textContent='正在识别…';
  try{const profile=await api(`stock-profile?symbol=${symbol}`);resolvedStock=profile;stockCodeName.textContent=`${profile.name||symbol}${profile.industry?` · ${profile.industry}`:''}`;addAnalysisButton.disabled=false;addAnalysisButton.textContent='加入自选并固定'}
  catch(e){stockCodeName.textContent='未找到股票名称';console.error(e)}
}
stockCodeInput.addEventListener('input',()=>{clearTimeout(profileTimer);profileTimer=setTimeout(resolveStockProfile,250)});
addAnalysisButton.addEventListener('click',async()=>{
  if(!resolvedStock)return;
  const symbol=resolvedStock.symbol||stockCodeInput.value.trim(),purchasePrice=Number(stockPurchasePrice.value);
  if(!Number.isFinite(purchasePrice)||purchasePrice<=0){stockCodeName.textContent='请输入有效购入价';stockPurchasePrice.focus();return}
  addAnalysisButton.disabled=true;addAnalysisButton.textContent='正在加入并分析…';
  try{await setAnalysisStatus(symbol,true,purchasePrice);await analyzeCode(symbol);signals=await api('signals');renderSignals();if(typeof window.refreshWatchlist==='function')await window.refreshWatchlist();addAnalysisButton.textContent='已加入自选并固定';stockCodeName.textContent=`${resolvedStock.name||resolvedStock.symbol} · 购入价 ${purchasePrice.toFixed(3)}`}
  catch(e){let detail=e.message||'未知错误';try{const parsed=JSON.parse(detail);detail=parsed.detail||parsed.error||detail}catch(_){}addAnalysisButton.textContent='重试加入';stockCodeName.textContent=`加入失败：${detail}`;console.error(e)}finally{addAnalysisButton.disabled=false}
});
