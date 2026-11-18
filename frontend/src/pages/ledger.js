import {store} from '../stores/entities.js';
import {session} from '../stores/session.js';
import {ledgerAPI} from '../api/entities.js';
import {table} from '../components/common/table.js';
import {esc} from '../utils/html.js';
import {message} from '../hooks/action.js';

// 演练页的已发布账本区间查询：走随恢复生成的持久索引，同值按点号排序，凭据续翻
const state={versions:[],page:null,busy:false,token:null,versionId:null,logSetId:null};

function versionBadge(v){
  const map={current:'当前版',previous:'旧版',pending:'等候发布',failed:'重建失败'};
  return `<span class="badge ${esc(v.state)}">${esc(map[v.state]||v.state)}</span>`;
}

async function loadVersions(logSetId){
  state.logSetId=logSetId??null;
  state.versions=logSetId?await ledgerAPI.versions(logSetId):[];
  state.versionId=null;
}

export async function ledgerPanel(){
  const defaultId=store.logs[0]?.id??null;
  if(state.logSetId===null||!store.logs.some(s=>s.id===state.logSetId)){await loadVersions(defaultId);}
  const sets=store.logs;
  const p=state.page;
  return `<section class="ledger-panel">
    <div class="section-title"><h2>已发布账本 · 按数值区间翻查采样点</h2><button class="secondary" id="ledger-refresh">刷新版本</button></div>
    <p>区间查询走随恢复生成的持久索引，同值按点号升序；翻页凭据有效期内始终查凭据绑定的原版，不漏点、不重读，过期拒绝续翻。同一日志集最多保留一份旧版。</p>
    <form id="ledger-query-form" class="fields">
      <label>日志集<select name="log_set_id">${sets.map(s=>`<option value="${s.id}" ${s.id===state.logSetId?'selected':''}>${esc(s.name)}</option>`).join('')}</select></label>
      <label>账本版本<select name="ledger_version_id"><option value="">当前版（默认）</option>${state.versions.filter(v=>['current','previous'].includes(v.state)).map(v=>`<option value="${v.id}" ${v.id===state.versionId?'selected':''}>第${v.version_no}版 · ${v.state==='current'?'当前版':'旧版'}</option>`).join('')}</select></label>
      <label>数值下界<input name="low_value" type="number" value="${p?esc(p.range.low):'-1000000000'}" required></label>
      <label>数值上界<input name="high_value" type="number" value="${p?esc(p.range.high):'1000000000'}" required></label>
      <label>每页条数<input name="page_size" type="number" min="1" max="200" value="${p?esc(p.page_size):'5'}"></label>
      <button type="submit" ${state.busy?'disabled':''}>按区间查询 →</button>
    </form>
    ${state.token?`<div class="toolbar"><button id="ledger-next" ${state.busy?'disabled':''}>续翻下一页（凭据 ${state.token.slice(0,8)}…）</button><button class="secondary" id="ledger-reset">放弃凭据，重新查询</button></div>`:''}
    ${p?`<div class="section-title"><h2>第${p.version_no}版 <small>${esc(p.version_state==='current'?'当前版':'旧版')}</small></h2><span>区间 ${p.range.low} — ${p.range.high} · ${p.has_more?'还有后续点':'已到末页'}</span></div>
      ${table(['点号','采样点键','数值'],p.points.map(x=>[x.point_no,esc(x.point_key),x.value]))}
      <p class="checksum">凭据有效期 ${p.token_expires_in_seconds} 秒${p.next_cursor?` · 续翻锚点 (值 ${p.next_cursor.value}, 点号 ${p.next_cursor.point_no})`:''}${p.has_more?'':' · 无下一页凭据'}</p>`:''}
    <h2>版本发布情况</h2>
    ${table(['版本','状态','发布时间','失败原因','操作'],state.versions.length?state.versions.map(v=>[
      '第'+v.version_no+'版',
      versionBadge(v),
      v.published_at?esc(v.published_at):'—',
      esc(v.last_error||'—'),
      v.state==='failed'&&session.role!=='viewer'?`<button class="secondary ledger-retry" data-id="${v.id}">重试重建</button>`:'—',
    ]):[['—','—','—','—','暂无版本，完成一次恢复后随恢复生成']])}
  </section>`;
}

export function bindLedgerPanel(render){
  document.querySelector('#ledger-refresh')?.addEventListener('click',render);
  document.querySelector('#ledger-reset')?.addEventListener('click',()=>{state.token=null;state.page=null;render();});
  document.querySelector('#ledger-query-form select[name=log_set_id]')?.addEventListener('change',async e=>{
    state.token=null;state.page=null;
    try{await loadVersions(Number(e.target.value));}catch(err){message(err.message,true);}
    render();
  });
  document.querySelector('#ledger-next')?.addEventListener('click',async()=>{
    if(!state.token)return;
    state.busy=true;
    try{
      // 续翻只带凭据：后端锁定凭据原版与原区间，忽略版本切换
      state.page=await ledgerAPI.query({log_set_id:state.page.log_set_id,page_token:state.token});
      state.token=state.page.next_token;
    }catch(e){message(e.message,true);if(/过期|作废|下架|已使用/.test(e.message)){state.token=null;state.page=null;}}
    finally{state.busy=false;render();}
  });
  document.querySelectorAll('.ledger-retry').forEach(btn=>btn.addEventListener('click',async()=>{
    try{await ledgerAPI.retry(Number(btn.dataset.id));message('已提交重试，稍后刷新查看');}catch(e){message(e.message,true);}
    render();
  }));
  document.querySelector('#ledger-query-form')?.addEventListener('submit',async e=>{
    e.preventDefault();
    const f=e.target,d=Object.fromEntries(new FormData(f));
    state.busy=true;
    try{
      const payload={log_set_id:Number(d.log_set_id),low_value:Number(d.low_value),high_value:Number(d.high_value),page_size:Number(d.page_size)||20};
      if(d.ledger_version_id)payload.ledger_version_id=Number(d.ledger_version_id);
      state.versionId=payload.ledger_version_id||null;
      state.page=await ledgerAPI.query(payload);
      state.token=state.page.next_token;
    }catch(err){message(err.message,true);}
    finally{state.busy=false;render();}
  });
}
