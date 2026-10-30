"use strict";

globalThis.InspectorSharing=(()=>{
  const get=id=>document.getElementById(id),tr=value=>typeof t==="function"?t(value):value;
  const esc=value=>String(value??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const label=value=>esc(tr(value)),number=value=>Number(value||0).toLocaleString();
  const dollars=n=>"$"+(Number(n||0)/1e9).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:4});
  const bytes=n=>{n=Number(n||0);return n<1000?`${number(n)} B`:n<1e6?`${(n/1000).toFixed(1)} KB`:`${(n/1e6).toFixed(2)} MB`;};
  const date=n=>n?new Date(n*1000).toLocaleString(undefined,{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"}):tr("永久");
  const localDate=n=>{const d=new Date(n);return new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,16);};
  let active=false,busy=false,current=null,timer=null,controller=null,resource="quota",focusId="",recoveryShown=false;
  const views={quota:"out",exit:"out"};
  const message=(value,error=false)=>{const box=get("sharing-message");box.textContent=tr(value);box.hidden=!value;box.classList.toggle("error",error);const local=get("credential-edit-message");local.textContent=error?tr(value):"";local.hidden=!error;};
  function card(kind){
    const title=kind==="quota"?"额度":"出口";
    return `<section class="sharing-card" data-kind="${kind}"><header><h2>${label(title)}</h2>
      <div class="sharing-switch" role="group" aria-label="${label(title)}"><button type="button" data-view="out" data-kind="${kind}" aria-pressed="true">${label("借出")}</button><button type="button" data-view="in" data-kind="${kind}" aria-pressed="false">${label("借入")}</button></div></header>
      <form id="${kind}-issue" class="sharing-form"><label>${label("名称")}<input id="${kind}-label" maxlength="80" required autocomplete="off"></label>
        <label>${label("期限")}<input id="${kind}-expiry" type="datetime-local"></label>
        <div class="sharing-presets" data-expiry-target="${kind}-expiry"><button type="button" data-days="1">${label("一天")}</button><button type="button" data-days="7">${label("一周")}</button><button type="button" data-days="0">${label("永久")}</button></div>
        ${kind==="quota"?`<label><span id="quota-budget-label">${label("额度")}</span><span class="sharing-amount-field"><input id="quota-budget" type="number" value="0" min="0" max="1000000" step="0.01" required aria-labelledby="quota-budget-label" aria-describedby="quota-budget-unit" title="${label("0 表示不限。")}"><span id="quota-budget-unit" class="sharing-amount-unit">${label("美元")}</span></span></label><label class="sharing-check"><input id="quota-weekly" type="checkbox">${label("每周刷新额度")}</label>`:""}
      </form>
      <form id="${kind}-borrow" class="sharing-form" hidden><label>${label("名称")}<input id="${kind}-borrow-label" maxlength="80" required autocomplete="off"></label><label>${label("对方的凭据")}<input id="${kind}-borrow-link" type="password" autocomplete="off" spellcheck="false" required placeholder="${kind==="quota"?"codex-share":"codex-exit"}://…"></label></form>
      <div class="sharing-actions"><button class="primary" type="submit" form="${kind}-issue" id="${kind}-generate">${label("生成凭据")}</button><button class="primary" type="submit" form="${kind}-borrow" id="${kind}-activate" hidden>${label("保存并借入")}</button><button type="button" class="sharing-service" id="${kind}-service" data-action="service" data-kind="${kind}" hidden>${label("暂停借出")}</button></div>
      ${kind==="quota"?`<button id="quota-local" class="sharing-local" type="button" data-action="local" data-kind="quota">${label("使用本机额度")}</button>`:`<div id="exit-local" class="sharing-exit-source"><button type="button" data-action="provider" data-kind="exit">${label("额度方出口")}</button><button type="button" data-action="local" data-kind="exit">${label("本机出口")}</button></div>`}
      <p class="sharing-card-note">${label(kind==="quota"?"每张凭据仅供一位同事使用。":"借出的出口始终使用本机网络。")}</p>
    </section>`;
  }
  get("sharing-config").innerHTML=card("quota")+card("exit");
  for(const kind of ["quota","exit"]){get(kind+"-expiry").value=localDate(Date.now()+7*864e5);}
  get("sharing-stat-count").innerHTML=`<select id="sharing-stat-filter" aria-label="${label("统计范围")}"></select>`;
  function expiry(id){const value=get(id).value;if(!value)return null;const n=new Date(value).getTime()/1000;if(!Number.isFinite(n)||n<=Date.now()/1000)throw new Error(tr("请选择未来的到期时间，或选择永久。"));return n;}
  async function request(data,signal){
    const response=await fetch("/api/sharing",{method:data?"POST":"GET",cache:"no-store",headers:data?{"Content-Type":"application/json"}:{},body:data?JSON.stringify(data):undefined,signal});
    if(response.status===404)throw new Error(tr("当前后台不支持共享，请更新软件后使用。"));
    const result=await response.json();if(!response.ok)throw new Error(tr(result.error||"共享操作失败。"));return result;
  }
  function shownRows(){return (current?.credentials||[]).filter(row=>row.kind===resource&&row.direction===views[resource]&&!row.internal);}
  function data(row){return row.direction==="in"?(row.snapshot||{}):row;}
  const partial=row=>(data(row).totals?.missing_responses||0)+(data(row).totals?.unpriced_responses||0)>0;
  const money=(value,rows)=>(rows.some(partial)?"≥ ":"")+dollars(value);
  function isActive(row){return row.direction==="in"&&row.id===(row.kind==="quota"?current.client?.active_id:current.egress?.selection?.active_id);}
  function status(row){const d=data(row);return isActive(row)?row.online===false?"连接不可用":"使用中":d.expired?"已到期":d.enabled===false?"已停用":d.exhausted?"额度已用完":row.direction==="in"?"未使用":d.bound?"已绑定":"待绑定";}
  function draw(rows){
    const today=new Date();today.setHours(0,0,0,0);
    const keys=Array.from({length:14},(_,i)=>{const d=new Date(today);d.setDate(d.getDate()-13+i);return localDate(d.getTime()).slice(0,10);});
    const values=keys.map(key=>rows.reduce((sum,row)=>{const day=data(row).daily?.[key]||{};return sum+(resource==="quota"?day.cost_nanos||0:(day.sent_bytes||0)+(day.received_bytes||0));},0));
    const max=Math.max(...values,1),points=values.map((v,i)=>`${24+i*752/13},${130-v/max*102}`).join(" ");
    get("sharing-chart").innerHTML=`<div class="sharing-chart-heading"><span>${label("最近 14 天")}</span><strong>${esc(resource==="quota"?dollars(Math.max(...values)):bytes(Math.max(...values)))}</strong></div><svg viewBox="0 0 800 164" role="img" aria-label="${label("每日用量")}"><path d="M24 130H776" class="sharing-chart-axis"/><polyline points="${points}" class="sharing-chart-line"/>${values.map((v,i)=>`<circle cx="${24+i*752/13}" cy="${130-v/max*102}" r="3"><title>${esc(keys[i])} · ${esc(resource==="quota"?dollars(v):bytes(v))}</title></circle>`).join("")}<text x="24" y="155">${keys[0].slice(5)}</text><text x="776" y="155" text-anchor="end">${keys[13].slice(5)}</text></svg>`;
  }
  function renderStats(){
    const rows=shownRows();if(!rows.some(row=>row.id===focusId))focusId="";
    get("sharing-stat-title").textContent=tr(resource==="quota"?"额度":"出口")+" · "+tr(views[resource]==="out"?"借出":"借入");
    const filter=get("sharing-stat-filter");filter.innerHTML=`<option value="">${label("全部凭据")}</option>`+rows.map(row=>`<option value="${esc(row.id)}">${esc(row.label)}</option>`).join("");filter.value=focusId;
    const selected=focusId?rows.filter(row=>row.id===focusId):rows;
    const sum=key=>selected.reduce((n,row)=>n+Number(data(row)[key]||0),0),total=key=>selected.reduce((n,row)=>n+Number(data(row).totals?.[key]||0),0);
    const unlimited=selected.some(row=>!data(row).budget_nanos);
    const summaries=resource==="quota"?[["本期已用",money(sum("period_cost_nanos"),selected)],["本周已用",money(sum("week_cost_nanos"),selected)],["累计已用",money(total("cost_nanos"),selected)],["总额度",unlimited?tr("不限"):dollars(sum("budget_nanos"))]]:[["累计上传",bytes(total("sent_bytes"))],["累计下载",bytes(total("received_bytes"))],["连接次数",number(total("requests"))],["凭据",number(selected.length)]];
    get("sharing-summary").innerHTML=summaries.map(([name,value])=>`<div><span>${label(name)}</span><strong>${esc(value)}</strong></div>`).join("");draw(selected);
    get("sharing-table-head").innerHTML=`<tr>${(resource==="quota"?["凭据","期限","本期 / 额度","本周 / 累计","操作"]:["凭据","期限","上传 / 下载","连接次数","操作"]).map(value=>`<th>${label(value)}</th>`).join("")}</tr>`;
    get("sharing-credential-rows").innerHTML=rows.map(row=>{const d=data(row),quota=resource==="quota",out=row.direction==="out",limit=d.budget_nanos?dollars(d.budget_nanos):tr("不限");
      return `<tr class="${isActive(row)?"current":""}"><td><strong>${esc(row.label)}</strong><small>${label(status(row))}${d.weekly_reset?" · "+label("每周刷新"):""}</small></td><td>${esc(d.expires_at===undefined?"—":date(d.expires_at))}</td><td>${esc(quota?money(d.period_cost_nanos,[row]):bytes(d.totals?.sent_bytes))}<small>${esc(quota?limit:bytes(d.totals?.received_bytes))}</small></td><td>${esc(quota?money(d.week_cost_nanos,[row]):number(d.totals?.requests))}${quota?`<small>${esc(money(d.totals?.cost_nanos,[row]))}</small>`:""}</td><td><div class="sharing-row-actions">${out?`<button type="button" data-action="edit" data-id="${row.id}">${label("编辑")}</button><button type="button" data-action="copy" data-id="${row.id}">${label("复制")}</button>${quota?`<button type="button" data-action="reset" data-id="${row.id}">${label("重置")}</button>`:""}<button type="button" data-action="toggle" data-id="${row.id}">${label(d.enabled?"停用":"启用")}</button>`:`<button type="button" data-action="use" data-id="${row.id}" ${isActive(row)?"disabled":""}>${label(isActive(row)?"使用中":"使用")}</button>`}</div></td></tr>`;
    }).join("");
    get("sharing-empty").hidden=rows.length>0;
    const missing=total("missing_responses"),unpriced=total("unpriced_responses");
    get("sharing-stat-note").textContent=resource==="quota"?tr("按 API 公开价格估算，含缓存；不代表 ChatGPT 订阅余额。")+" "+tr("每周一零点刷新；手动重置保留累计统计。")+(missing||unpriced?` ${tr("用量缺失")} ${number(missing)} · ${tr("未计价")} ${number(unpriced)}`:""):tr("出口只统计流量，不读取对话。借入记录显示最近同步值。");
  }
  function render(value){
    if(!value.credentials_supported||value.protocol<4){current=null;get("sharing-cards").hidden=true;message("当前后台不支持凭据管理，请更新软件。",true);return;}
    current=value;get("sharing-cards").hidden=false;
    const hosting=value.host?.running===true,borrowing=value.client?.active===true,exitHosting=value.egress?.host?.active===true;
    for(const kind of ["quota","exit"]){const out=views[kind]==="out";get(kind+"-issue").hidden=!out;get(kind+"-borrow").hidden=out;
      get(kind+"-generate").hidden=!out;get(kind+"-activate").hidden=out;
      for(const button of get("sharing-config").querySelectorAll(`[data-view][data-kind="${kind}"]`))button.setAttribute("aria-pressed",String(button.dataset.view===views[kind]));
      get(kind+"-generate").disabled=busy||(kind==="quota"&&borrowing);get(kind+"-activate").disabled=busy||(kind==="quota"&&hosting);
      const running=kind==="quota"?hosting:exitHosting;get(kind+"-service").hidden=!running&&!(value.credentials||[]).some(row=>row.kind===kind&&row.direction==="out");get(kind+"-service").textContent=tr(running?"暂停借出":"恢复借出");get(kind+"-service").disabled=busy||(kind==="quota"&&borrowing);
    }
    get("quota-local").hidden=!borrowing;get("quota-local").disabled=busy;get("exit-local").hidden=views.exit!=="in";
    for(const button of get("exit-local").querySelectorAll("button")){button.disabled=busy;button.setAttribute("aria-pressed",String(button.dataset.action===value.egress?.selection?.mode));}
    for(const button of get("sharing-credential-rows").querySelectorAll("button"))button.disabled=busy;
    renderStats();
    if(!recoveryShown&&(value.recovery_error||value.recovered)){recoveryShown=true;message(value.recovery_error||"上次共享已结束，已恢复原连接。请重新加载 Codex 配置。",!!value.recovery_error);}
  }
  async function action(button,dataValue,success=""){if(busy)return;controller?.abort();busy=true;button?.setAttribute("aria-busy","true");if(button)button.disabled=true;message("");
    try{const result=await request(dataValue);if(result.link){get("sharing-link").value=result.link;get("sharing-link-area").hidden=false;}
      render(result);if(result.reload_required===true)message("已切换额度来源。请重新加载 Codex 配置或重启 ChatGPT 应用。");else message(success||"已更新，新请求使用当前设置。");return result;
    }catch(error){message(error.message,true);}finally{busy=false;button?.setAttribute("aria-busy","false");if(current)render(current);}}
  async function refresh(){if(!active||document.hidden||busy)return;controller?.abort();const task=new AbortController();controller=task;try{const value=await request(null,task.signal);if(active&&!task.signal.aborted)render(value);}catch(error){if(error.name!=="AbortError")message(error.message,true);}finally{if(controller===task)controller=null;}}
  function edit(row){get("credential-edit-message").hidden=true;get("credential-edit-id").value=row.id;get("credential-edit-label").value=row.label;get("credential-edit-expiry").value=row.expires_at?localDate(row.expires_at*1000):"";get("credential-edit-budget").value=Number(row.budget_nanos||0)/1e9;get("credential-edit-weekly").checked=row.weekly_reset===true;get("credential-edit-quota").hidden=row.kind!=="quota";get("sharing-credential-dialog").showModal();}
  async function copy(value){try{await navigator.clipboard.writeText(value);message("凭据已复制。");}catch(_){get("sharing-link").value=value;get("sharing-link-area").hidden=false;get("sharing-link").focus();get("sharing-link").select();message("请复制已选中的凭据。");}}
  get("sharing-config").addEventListener("click",event=>{const button=event.target.closest("button");if(!button||busy)return;const {kind,view,action:command}=button.dataset;
    if(view){views[kind]=view;resource=kind;focusId="";if(current)render(current);return;}
    if(command==="service"){const running=kind==="quota"?current?.host?.running:current?.egress?.host?.active;action(button,{action:kind==="quota"?(running?"host_stop":"host_resume"):(running?"egress_stop":"egress_resume")});}
    if(command==="local"||command==="provider")action(button,kind==="quota"?{action:"client_disconnect"}:{action:"egress_select",mode:command==="provider"?"provider":"local"});
  });
  for(const kind of ["quota","exit"]){
    get(kind+"-issue").addEventListener("submit",async event=>{event.preventDefault();resource=kind;try{await action(get(kind+"-generate"),{action:"credential_issue",kind,label:get(kind+"-label").value.trim(),expires_at:expiry(kind+"-expiry"),...(kind==="quota"?{budget:get("quota-budget").value,weekly_reset:get("quota-weekly").checked}:{})},"凭据已生成，仅交给一位同事使用。");}catch(error){message(error.message,true);}});
    get(kind+"-borrow").addEventListener("submit",async event=>{event.preventDefault();resource=kind;const result=await action(get(kind+"-activate"),{action:"credential_use",kind,label:get(kind+"-borrow-label").value.trim(),link:get(kind+"-borrow-link").value.trim()});if(result)get(kind+"-borrow-link").value="";});
  }
  get("sharing-view").addEventListener("click",event=>{const button=event.target.closest("[data-days]");if(!button)return;const id=button.closest("[data-expiry-target]").dataset.expiryTarget;get(id).value=Number(button.dataset.days)?localDate(Date.now()+Number(button.dataset.days)*864e5):"";});
  get("sharing-stat-filter").addEventListener("change",()=>{focusId=get("sharing-stat-filter").value;renderStats();});
  get("sharing-credential-rows").addEventListener("click",async event=>{const button=event.target.closest("[data-action]");if(!button||busy)return;const row=(current?.credentials||[]).find(row=>row.id===button.dataset.id);if(!row)return;const command=button.dataset.action;
    if(command==="edit"){edit(row);return;}if(command==="copy"){const result=await action(button,{action:"credential_copy",id:row.id});if(result?.link)await copy(result.link);return;}
    if(command==="reset")await action(button,{action:"credential_reset",id:row.id},"本期额度已重置，累计统计已保留。");
    if(command==="toggle")await action(button,{action:"credential_edit",id:row.id,enabled:!row.enabled});
    if(command==="use")await action(button,{action:"credential_use",kind:row.kind,id:row.id});
  });
  get("credential-edit-form").addEventListener("submit",async event=>{event.preventDefault();try{const result=await action(get("credential-edit-save"),{action:"credential_edit",id:get("credential-edit-id").value,label:get("credential-edit-label").value,expires_at:expiry("credential-edit-expiry"),budget:get("credential-edit-budget").value,weekly_reset:get("credential-edit-weekly").checked});if(result)get("sharing-credential-dialog").close();}catch(error){message(error.message,true);}});
  get("credential-edit-cancel").onclick=()=>get("sharing-credential-dialog").close();get("sharing-copy").onclick=()=>copy(get("sharing-link").value);get("sharing-link-close").onclick=()=>{get("sharing-link").value="";get("sharing-link-area").hidden=true;};
  document.addEventListener("visibilitychange",()=>{if(document.hidden)controller?.abort();else if(active)refresh();});
  function setActive(value){active=value;clearInterval(timer);timer=null;if(!active){controller?.abort();if(get("sharing-credential-dialog").open)get("sharing-credential-dialog").close();get("sharing-link").value="";get("sharing-link-area").hidden=true;return;}message("");refresh();timer=setInterval(refresh,3000);}
  return {setActive};
})();
if(typeof workspaceView!=="undefined"&&workspaceView==="sharing")globalThis.InspectorSharing.setActive(true);
