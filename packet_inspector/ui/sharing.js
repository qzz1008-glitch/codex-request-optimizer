"use strict";

globalThis.InspectorSharing=(()=>{
  const get=id=>document.getElementById(id),tr=value=>typeof t==="function"?t(value):value;
  const esc=value=>String(value??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const label=value=>esc(tr(value)),number=value=>Number(value||0).toLocaleString();
  const dollars=n=>"$"+(Number(n||0)/1e9).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:4});
  const bytes=n=>{n=Number(n||0);return n<1000?`${number(n)} B`:n<1e6?`${(n/1000).toFixed(1)} KB`:`${(n/1e6).toFixed(2)} MB`;};
  const date=n=>n?new Date(n*1000).toLocaleString(undefined,{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"}):tr("无期限");
  const localDate=n=>{const d=new Date(n);return new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,16);};
  const minimumExpiry=()=>localDate(Math.ceil((Date.now()+864e5)/60000)*60000);
  let active=false,busy=false,current=null,timer=null,controller=null,recoveryShown=false;
  const views={quota:"out",exit:"out"};
  const message=(value,error=false)=>{const box=get("sharing-message");box.textContent=tr(value);box.hidden=!value;box.classList.toggle("error",error);const local=get("credential-edit-message");local.textContent=error?tr(value):"";local.hidden=!error;};
  const expiryIds=["quota-expiry","exit-expiry","credential-edit-expiry"];
  function expiryField(id){
    return `<div id="${id}-field" class="sharing-expiry"><span id="${id}-label" class="sharing-expiry-label">${label("期限")}</span><div class="sharing-expiry-control"><div class="sharing-expiry-options" role="group" aria-labelledby="${id}-label">${[["week","一周"],["none","无期限"],["custom","指定时间"]].map(([mode,text])=>`<button id="${id}-${mode}" type="button" data-expiry-target="${id}" data-expiry-mode="${mode}" aria-pressed="false"${mode==="custom"?` aria-haspopup="dialog" aria-controls="${id}-picker" aria-expanded="false"`:""}>${label(text)}</button>`).join("")}</div><div id="${id}-picker" class="sharing-expiry-picker" role="dialog" aria-labelledby="${id}-label" hidden><input id="${id}" type="datetime-local" aria-labelledby="${id}-label" required disabled><div class="sharing-expiry-picker-actions"><button type="button" data-expiry-target="${id}" data-expiry-action="cancel">${label("取消")}</button><button type="button" class="primary" data-expiry-target="${id}" data-expiry-action="apply">${label("确定")}</button></div></div></div></div>`;
  }
  function card(kind){
    const title=kind==="quota"?"额度":"出口";
    return `<section class="sharing-card" data-kind="${kind}"><header><h2>${label(title)}</h2>
      <div class="sharing-switch" role="group" aria-label="${label(title)}"><button type="button" data-view="out" data-kind="${kind}" aria-pressed="true">${label("借出")}</button><button type="button" data-view="in" data-kind="${kind}" aria-pressed="false">${label("借入")}</button></div></header>
      <form id="${kind}-issue" class="sharing-form"><label>${label("名称")}<input id="${kind}-label" maxlength="80" required autocomplete="off"></label>
        ${expiryField(kind+"-expiry")}
        ${kind==="quota"?`<label><span id="quota-budget-label">${label("额度")}</span><span class="sharing-amount-field"><input id="quota-budget" type="number" value="1" min="1" max="1000000" step="0.01" required aria-labelledby="quota-budget-label" aria-describedby="quota-budget-unit" title="${label("最低 1 美元。")}"><span id="quota-budget-unit" class="sharing-amount-unit">${label("美元")}</span></span></label><label class="sharing-check"><input id="quota-weekly" type="checkbox">${label("每周刷新额度")}</label>`:""}
      </form>
      <form id="${kind}-borrow" class="sharing-form" hidden><label>${label("名称")}<input id="${kind}-borrow-label" maxlength="80" required autocomplete="off"></label><label>${label("对方的凭据")}<input id="${kind}-borrow-link" type="password" autocomplete="off" spellcheck="false" required placeholder="${kind==="quota"?"codex-share":"codex-exit"}://…"></label></form>
      <div class="sharing-actions"><button class="primary" type="submit" form="${kind}-issue" id="${kind}-generate">${label("生成凭据")}</button><button class="primary" type="submit" form="${kind}-borrow" id="${kind}-activate" hidden>${label("保存并借入")}</button></div>
      ${kind==="quota"?`<button id="quota-local" class="sharing-local" type="button" data-action="local" data-kind="quota">${label("使用本机额度")}</button>`:`<div id="exit-local" class="sharing-exit-source"><button type="button" data-action="provider" data-kind="exit">${label("额度方出口")}</button><button type="button" data-action="local" data-kind="exit">${label("本机出口")}</button></div>`}
      ${kind==="exit"?`<p class="sharing-card-note">${label("每张出口凭据仅供一台机器使用，始终通过本机网络转发。")}</p>`:""}
    </section>`;
  }
  get("sharing-config").innerHTML=card("quota")+card("exit");
  get("credential-edit-expiry-field").outerHTML=expiryField("credential-edit-expiry");
  function setExpiry(id,mode,value=""){
    get(id+"-field").dataset.mode=mode;get(id).value=mode==="week"?localDate(Date.now()+7*864e5):mode==="none"?"":value;
    for(const option of ["week","none","custom"])get(id+"-"+option).setAttribute("aria-pressed",String(option===mode));
    get(id+"-custom").title=mode==="custom"?get(id).value.replace("T"," "):tr("指定时间");
  }
  function closeExpiryPicker(id,focus=false){
    const picker=get(id+"-picker");if(picker.hidden)return false;
    get(id).value=picker.dataset.originalValue;get(id).disabled=true;get(id).setCustomValidity("");picker.hidden=true;get(id+"-custom").setAttribute("aria-expanded","false");if(focus)get(id+"-custom").focus();return true;
  }
  function closeExpiryPickers(){for(const id of expiryIds)closeExpiryPicker(id);}
  function resetIssue(kind){closeExpiryPicker(kind+"-expiry");get(kind+"-label").value="";setExpiry(kind+"-expiry","week");if(kind==="quota"){get("quota-budget").value="1";get("quota-weekly").checked=false;}}
  function applyExpiry(id){
    const input=get(id);input.setCustomValidity("");if(!input.reportValidity())return;
    const value=input.value;try{expiryTime(value,id);}catch(error){input.setCustomValidity(error.message);input.reportValidity();return;}
    closeExpiryPicker(id,true);setExpiry(id,"custom",value);
  }
  for(const id of expiryIds){setExpiry(id,"week");get(id+"-picker").hidden=true;get(id).disabled=true;get(id).addEventListener("input",()=>get(id).setCustomValidity(""));}
  function expiryTime(value,id){const original=get("credential-edit-form").dataset.originalExpiry;if(id==="credential-edit-expiry"&&original&&value===localDate(Number(original)*1000))return Number(original);const n=value?new Date(value).getTime()/1000:NaN;if(!Number.isFinite(n)||n<Date.now()/1000+86400)throw new Error(tr("期限至少为一天，也可选择无期限。"));return n;}
  function expiry(id){const mode=get(id+"-field").dataset.mode;return mode==="none"?null:mode==="week"?Math.floor(Date.now()/1000)+7*86400:expiryTime(get(id).value,id);}
  function budget(id){const value=get(id).value,n=Number(value),original=get("credential-edit-form").dataset.originalBudget;if(!Number.isFinite(n)||n<1||n>1000000){if(!(id==="credential-edit-budget"&&original!==undefined&&n===Number(original)))throw new Error(tr("额度应为 1 到 1,000,000 美元。"));}return value;}
  async function request(data,signal){
    const response=await fetch("/api/sharing",{method:data?"POST":"GET",cache:"no-store",headers:data?{"Content-Type":"application/json"}:{},body:data?JSON.stringify(data):undefined,signal});
    if(response.status===404)throw new Error(tr("当前后台不支持共享，请更新软件后使用。"));
    const result=await response.json();if(!response.ok)throw new Error(tr(result.error||"共享操作失败。"));return result;
  }
  function shownRows(kind){return (current?.credentials||[]).filter(row=>row.kind===kind&&!row.internal);}
  function data(row){return row.direction==="in"?(row.snapshot||{}):row;}
  const partial=row=>(data(row).totals?.missing_responses||0)+(data(row).totals?.unpriced_responses||0)>0;
  const money=(value,rows)=>(rows.some(partial)?"≥ ":"")+dollars(value);
  function isActive(row){return row.direction==="in"&&row.id===(row.kind==="quota"?current.client?.active_id:current.egress?.selection?.active_id);}
  const lending=row=>row.enabled!==false&&(row.kind==="quota"?current?.host?.running===true:current?.egress?.host?.active===true);
  function status(row){const d=data(row);return isActive(row)?row.online===false?"连接不可用":"使用中":d.expired?"已到期":d.enabled===false||(row.direction==="out"&&!lending(row))?"已停用":d.exhausted?"额度已用完":row.direction==="in"?"未使用":"已启用";}
  function quotaUsage(row){
    const d=data(row),used=d.period_cost_nanos,budget=d.budget_nanos,knownUsed=Number.isFinite(used)&&used>=0,knownBudget=Number.isFinite(budget)&&budget>=0;
    const amount=knownUsed?money(used,[row]):"—",limit=knownBudget?budget===0?tr("不限"):dollars(budget):"—",text=amount+" / "+limit;
    const progress=knownUsed&&knownBudget&&budget>0?`<progress class="sharing-budget-progress" max="${budget}" value="${Math.min(used,budget)}" data-full="${used>=budget}" aria-label="${label("已用 / 额度")}" aria-valuetext="${esc(text)}" title="${esc(text)}"></progress>`:"";
    return `<div class="sharing-budget-cell"><span>${esc(amount)}<small> / ${esc(limit)}</small></span>${progress}</div>`;
  }
  function renderList(kind){
    const rows=shownRows(kind),quota=kind==="quota";
    get(kind+"-list-title").textContent=tr(quota?"额度凭据":"出口凭据");
    get(kind+"-credential-count").textContent=number(rows.length);
    get(kind+"-table-head").innerHTML=`<tr>${(quota?["名称","已用 / 额度","本周 / 累计用量","期限","状态","操作"]:["名称","上传 / 下载流量","连接次数","期限","状态","操作"]).map(value=>`<th>${label(value)}</th>`).join("")}</tr>`;
    get(kind+"-credential-rows").innerHTML=rows.map(row=>{const d=data(row),out=row.direction==="out",weekly=quota?tr("每周重置")+"："+(d.weekly_reset===true?tr("是"):d.weekly_reset===false?tr("否"):"—"):"";
      return `<tr class="${isActive(row)?"current":""}"><td><strong title="${esc(row.label)}">${esc(row.label)}</strong><small class="sharing-direction">${label(out?"借出":"借入")}</small></td><td>${quota?quotaUsage(row):`${esc(bytes(d.totals?.sent_bytes))}<small> / ${esc(bytes(d.totals?.received_bytes))}</small>`}</td><td>${esc(quota?money(d.week_cost_nanos,[row]):number(d.totals?.requests))}${quota?`<small> / ${esc(money(d.totals?.cost_nanos,[row]))}</small>`:""}</td><td${quota?` title="${esc(weekly)}"`:""}>${esc(d.expires_at===undefined?"—":date(d.expires_at))}${quota&&d.weekly_reset===true?`<small class="sharing-weekly-reset"> · ${label("每周重置")}</small>`:""}</td><td class="sharing-row-status">${label(status(row))}</td><td><div class="sharing-row-actions">${out?`<button type="button" data-action="edit" data-id="${row.id}">${label("编辑")}</button><button type="button" data-action="copy" data-id="${row.id}">${label("复制")}</button>${quota?`<button type="button" data-action="reset" data-id="${row.id}">${label("重置")}</button>`:""}<button type="button" data-action="toggle" data-id="${row.id}">${label(lending(row)?"停用":"启用")}</button>`:`<button type="button" data-action="use" data-id="${row.id}" ${isActive(row)?"disabled":""}>${label(isActive(row)?"使用中":"使用")}</button>`}</div></td></tr>`;
    }).join("");
    get(kind+"-empty").hidden=rows.length>0;
  }
  function render(value){
    if(!value.credentials_supported||value.protocol<4){current=null;get("sharing-cards").hidden=true;message("当前后台不支持凭据管理，请更新软件。",true);return;}
    current=value;get("sharing-cards").hidden=false;
    const hosting=value.host?.running===true,borrowing=value.client?.active===true;
    for(const kind of ["quota","exit"]){const out=views[kind]==="out";get(kind+"-issue").hidden=!out;get(kind+"-borrow").hidden=out;
      get(kind+"-generate").hidden=!out;get(kind+"-activate").hidden=out;
      for(const button of get("sharing-config").querySelectorAll(`[data-view][data-kind="${kind}"]`))button.setAttribute("aria-pressed",String(button.dataset.view===views[kind]));
      get(kind+"-generate").disabled=busy||(kind==="quota"&&borrowing);get(kind+"-activate").disabled=busy||(kind==="quota"&&hosting);
    }
    get("quota-local").hidden=!borrowing;get("quota-local").disabled=busy;get("exit-local").hidden=views.exit!=="in";
    for(const button of get("exit-local").querySelectorAll("button")){button.disabled=busy;button.setAttribute("aria-pressed",String(button.dataset.action===value.egress?.selection?.mode));}
    for(const kind of ["quota","exit"])renderList(kind);
    if(busy)for(const button of get("sharing-lists").querySelectorAll("button"))button.disabled=true;
    if(!recoveryShown&&(value.recovery_error||value.recovered)){recoveryShown=true;message(value.recovery_error||"上次共享已结束，已恢复原连接。请重新加载 Codex 配置。",!!value.recovery_error);}
  }
  async function action(button,dataValue,success=""){if(busy)return;controller?.abort();busy=true;button?.setAttribute("aria-busy","true");if(button)button.disabled=true;message("");
    try{const result=await request(dataValue);if(result.link){get("sharing-link").value=result.link;get("sharing-link-area").hidden=false;}
      render(result);if(result.reload_required===true)message("已切换额度来源。请重新加载 Codex 配置或重启 ChatGPT 应用。");else message(success===null?"":success||"已更新，新请求使用当前设置。");return result;
    }catch(error){message(error.message,true);}finally{busy=false;button?.setAttribute("aria-busy","false");if(button)button.disabled=false;if(current)render(current);}}
  async function refresh(){if(!active||document.hidden||busy)return;controller?.abort();const task=new AbortController();controller=task;try{const value=await request(null,task.signal);if(active&&!task.signal.aborted)render(value);}catch(error){if(error.name!=="AbortError")message(error.message,true);}finally{if(controller===task)controller=null;}}
  function edit(row){closeExpiryPickers();get("credential-edit-message").hidden=true;get("credential-edit-id").value=row.id;get("credential-edit-label").value=row.label;get("credential-edit-form").dataset.originalExpiry=String(row.expires_at??"");get("credential-edit-form").dataset.originalBudget=String(Number(row.budget_nanos||0)/1e9);setExpiry("credential-edit-expiry",row.expires_at?"custom":"none",row.expires_at?localDate(row.expires_at*1000):"");get("credential-edit-budget").value=Number(row.budget_nanos||0)/1e9;get("credential-edit-budget").min=Math.min(1,Number(row.budget_nanos||0)/1e9);get("credential-edit-budget").step="any";get("credential-edit-weekly").checked=row.weekly_reset===true;get("credential-edit-quota").hidden=row.kind!=="quota";get("sharing-credential-dialog").showModal();}
  async function copy(value){try{await navigator.clipboard.writeText(value);message("凭据已复制。");}catch(_){get("sharing-link").value=value;get("sharing-link-area").hidden=false;get("sharing-link").focus();get("sharing-link").select();message("请复制已选中的凭据。");}}
  get("sharing-config").addEventListener("click",event=>{const button=event.target.closest("button");if(!button||busy)return;const {kind,view,action:command}=button.dataset;
    if(view){closeExpiryPickers();views[kind]=view;if(current)render(current);return;}
    if(command==="local"||command==="provider")action(button,kind==="quota"?{action:"client_disconnect"}:{action:"egress_select",mode:command==="provider"?"provider":"local"});
  });
  for(const kind of ["quota","exit"]){
    get(kind+"-issue").addEventListener("submit",async event=>{event.preventDefault();try{const result=await action(get(kind+"-generate"),{action:"credential_issue",kind,label:get(kind+"-label").value.trim(),expires_at:expiry(kind+"-expiry"),...(kind==="quota"?{budget:budget("quota-budget"),weekly_reset:get("quota-weekly").checked}:{})},null);if(result)resetIssue(kind);}catch(error){message(error.message,true);}});
    get(kind+"-borrow").addEventListener("submit",async event=>{event.preventDefault();const result=await action(get(kind+"-activate"),{action:"credential_use",kind,label:get(kind+"-borrow-label").value.trim(),link:get(kind+"-borrow-link").value.trim()});if(result)get(kind+"-borrow-link").value="";});
  }
  get("sharing-view").addEventListener("click",event=>{const button=event.target.closest("[data-expiry-target]");if(!button||busy)return;const {expiryTarget:id,expiryMode:mode,expiryAction:command}=button.dataset;
    if(mode==="custom"){const picker=get(id+"-picker");if(closeExpiryPicker(id,true))return;closeExpiryPickers();picker.dataset.originalValue=get(id).value;get(id).value||=localDate(Date.now()+7*864e5);const original=get("credential-edit-form").dataset.originalExpiry;get(id).min=id==="credential-edit-expiry"&&original?localDate(Math.min(Number(original)*1000,Date.parse(minimumExpiry()))):minimumExpiry();get(id).disabled=false;picker.hidden=false;get(id+"-custom").setAttribute("aria-expanded","true");get(id).focus();}
    else if(mode){closeExpiryPickers();setExpiry(id,mode);}else if(command==="apply")applyExpiry(id);else if(command==="cancel")closeExpiryPicker(id,true);
  });
  document.addEventListener("click",event=>{if(!event.target.closest(".sharing-expiry"))closeExpiryPickers();});
  document.addEventListener("keydown",event=>{const id=expiryIds.find(id=>!get(id+"-picker").hidden);if(!id)return;if(event.key==="Escape"){event.preventDefault();event.stopPropagation();closeExpiryPicker(id,true);}else if(event.key==="Enter"&&event.target===get(id)){event.preventDefault();applyExpiry(id);}},true);
  get("sharing-lists").addEventListener("click",async event=>{const button=event.target.closest("[data-action]");if(!button||busy)return;const row=(current?.credentials||[]).find(row=>row.id===button.dataset.id);if(!row)return;const command=button.dataset.action;
    if(command==="edit"){edit(row);return;}if(command==="copy"){const result=await action(button,{action:"credential_copy",id:row.id});if(result?.link)await copy(result.link);return;}
    if(command==="reset")await action(button,{action:"credential_reset",id:row.id},"本期额度已重置，累计统计已保留。");
    if(command==="toggle")await action(button,{action:"credential_edit",id:row.id,enabled:!lending(row)});
    if(command==="use")await action(button,{action:"credential_use",kind:row.kind,id:row.id});
  });
  get("credential-edit-form").addEventListener("submit",async event=>{event.preventDefault();try{const result=await action(get("credential-edit-save"),{action:"credential_edit",id:get("credential-edit-id").value,label:get("credential-edit-label").value,expires_at:expiry("credential-edit-expiry"),budget:budget("credential-edit-budget"),weekly_reset:get("credential-edit-weekly").checked});if(result)get("sharing-credential-dialog").close();}catch(error){message(error.message,true);}});
  get("credential-edit-cancel").onclick=()=>get("sharing-credential-dialog").close();get("sharing-copy").onclick=()=>copy(get("sharing-link").value);get("sharing-link-close").onclick=()=>{get("sharing-link").value="";get("sharing-link-area").hidden=true;};
  document.addEventListener("visibilitychange",()=>{if(document.hidden)controller?.abort();else if(active)refresh();});
  function setActive(value){active=value;clearInterval(timer);timer=null;if(!active){closeExpiryPickers();controller?.abort();if(get("sharing-credential-dialog").open)get("sharing-credential-dialog").close();get("sharing-link").value="";get("sharing-link-area").hidden=true;return;}message("");refresh();timer=setInterval(refresh,3000);}
  return {setActive};
})();
if(typeof workspaceView!=="undefined"&&workspaceView==="sharing")globalThis.InspectorSharing.setActive(true);
