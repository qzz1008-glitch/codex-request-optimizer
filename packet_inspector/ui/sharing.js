"use strict";

globalThis.InspectorSharing=(()=>{
  const get=id=>document.getElementById(id),tr=value=>typeof t==="function"?t(value):value;
  const esc=value=>String(value??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const label=value=>esc(tr(value)),number=value=>Number(value||0).toLocaleString();
  const dollars=n=>"$"+(Number(n||0)/1e9).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:4});
  const bytes=n=>{n=Number(n||0);return n<1000?`${number(n)} B`:n<1e6?`${(n/1000).toFixed(1)} KB`:`${(n/1e6).toFixed(2)} MB`;};
  const localHour=n=>{const d=new Date(n);return new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,13)+":00";};
  const date=n=>n===null?tr("无期限"):Number.isFinite(n)?localHour(n*1000).slice(0,13).replace("T"," ")+tr("时"):"—";
  const expiryHours=Array.from({length:8},(_,n)=>String(n*3).padStart(2,"0"));
  const futureHour=days=>{const d=new Date(Date.now()+days*864e5),hour=d.getHours()+(d.getMinutes()||d.getSeconds()||d.getMilliseconds()?1:0);d.setHours(Math.ceil(hour/3)*3,0,0,0);return localHour(d.getTime());};
  const minimumExpiry=()=>futureHour(1),defaultExpiry=()=>futureHour(7);
  let active=false,busy=false,current=null,timer=null,controller=null,recoveryShown=false,editing=null,deleting=null,copiedKey=null,copyTimer=null,calendar=null;
  const views={quota:"out",exit:"out"};
  const message=(value,error=false)=>{const box=get("sharing-message");box.textContent=tr(value);box.hidden=!value;box.classList.toggle("error",error);for(const id of ["sharing-inline-message","sharing-delete-message"]){const local=get(id);local.textContent=error?tr(value):"";local.hidden=!error;}};
  const expiryIds=["quota-expiry","exit-expiry","sharing-inline-expiry"];
  function expiryControl(id,labelId){
    return `<div class="sharing-expiry-control" role="group" aria-labelledby="${labelId}"><input id="${id}" type="hidden"><input id="${id}-hour" type="hidden"><button id="${id}-date" class="sharing-expiry-date" type="button" aria-haspopup="dialog" aria-expanded="false" aria-controls="${id}-calendar" aria-labelledby="${labelId} ${id}-display"><span id="${id}-display"></span><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" aria-hidden="true"><rect x="2.5" y="3.5" width="11" height="10" rx="1.5"/><path d="M5 2v3m6-3v3M3 7h10"/></svg></button>
      <div id="${id}-calendar" class="sharing-calendar" role="dialog" aria-labelledby="${id}-month" hidden><div class="sharing-calendar-header"><button id="${id}-previous" type="button" data-month="-1" aria-label="${label("上个月")}">‹</button><strong id="${id}-month" aria-live="polite"></strong><button type="button" data-month="1" aria-label="${label("下个月")}">›</button></div><div id="${id}-weekdays" class="sharing-calendar-weekdays" aria-hidden="true"></div><div id="${id}-days" class="sharing-calendar-days" role="group" aria-labelledby="${id}-month"></div><div class="sharing-calendar-time"><span id="${id}-hours-label">${label("小时")}</span><div id="${id}-hours" class="sharing-calendar-hours" role="group" aria-labelledby="${id}-hours-label"></div></div><div class="sharing-calendar-footer"><button id="${id}-apply" class="primary" type="button" data-apply="true">${label("确定")}</button><label class="sharing-expiry-unlimited"><input id="${id}-unlimited" type="checkbox">${label("无期限")}</label></div></div></div>`;
  }
  function expiryField(id){
    return `<div class="sharing-expiry"><label id="${id}-label" for="${id}-date">${label("期限")}</label>${expiryControl(id,id+"-label")}</div>`;
  }
  function copyCaption(text){return `<span class="sharing-button-original">${label(text)}</span><span class="sharing-button-copied">${label("已复制")}</span>`;}
  function clearCopied(){clearTimeout(copyTimer);copyTimer=null;copiedKey=null;for(const button of get("sharing-cards").querySelectorAll("[data-copied=true]"))button.setAttribute("data-copied","false");}
  function card(kind){
    return `<div class="sharing-card" data-kind="${kind}">
      <form id="${kind}-issue" class="sharing-form"><label>${label("名称")}<input id="${kind}-label" maxlength="80" required autocomplete="off"></label>
        ${expiryField(kind+"-expiry")}
        ${kind==="quota"?`<div class="sharing-budget-row"><label id="quota-budget-label" for="quota-budget">${label("额度")}</label><span class="sharing-amount-field"><input id="quota-budget" type="number" value="1" min="1" max="1000000" step="0.01" required aria-labelledby="quota-budget-label" aria-describedby="quota-budget-unit" title="${label("最低 1 美元。")}"><span id="quota-budget-unit" class="sharing-amount-unit">${label("美元")}</span></span><label class="sharing-check"><input id="quota-weekly" type="checkbox">${label("每周刷新")}</label></div>`:""}
      </form>
      <form id="${kind}-borrow" class="sharing-form" hidden><label>${label("名称")}<input id="${kind}-borrow-label" maxlength="80" required autocomplete="off"></label><label>${label("对方的凭据")}<input id="${kind}-borrow-link" type="password" autocomplete="off" spellcheck="false" required placeholder="${kind==="quota"?"codex-share":"codex-exit"}://…"></label></form>
      <div class="sharing-actions"><button class="primary sharing-copy-button" type="submit" form="${kind}-issue" id="${kind}-generate">${copyCaption("生成凭据")}</button><button class="primary" type="submit" form="${kind}-borrow" id="${kind}-activate" hidden>${label("保存并借入")}</button></div>
      ${kind==="quota"?`<button id="quota-local" class="sharing-local" type="button" data-action="local" data-kind="quota">${label("使用本机额度")}</button>`:`<div id="exit-local" class="sharing-exit-source"><button type="button" data-action="provider" data-kind="exit">${label("额度方出口")}</button><button type="button" data-action="local" data-kind="exit">${label("本机出口")}</button></div>`}
      <p class="sharing-card-note">${label(kind==="quota"?"每张额度凭据仅供一台机器使用。":"每张出口凭据仅供一台机器使用，始终通过本机网络转发。")}</p>
    </div>`;
  }
  for(const kind of ["quota","exit"])get(kind+"-config").innerHTML=card(kind);
  get("sharing-inline-expiry-container").innerHTML=expiryControl("sharing-inline-expiry","sharing-inline-label");
  function disableExpiry(id,disabled=false){
    get(id+"-unlimited").disabled=disabled;
    get(id+"-date").disabled=disabled;get(id+"-hour").disabled=disabled||get(id).dataset.unlimited==="true";
  }
  function setExpiry(id,value){
    const input=get(id),finite=value??input.dataset.finiteValue??defaultExpiry();
    input.dataset.finiteValue=finite;input.value=value??"";input.dataset.unlimited=String(value===null);
    const original=id==="sharing-inline-expiry"&&editing?.field==="expires_at"&&Number.isFinite(editing.original)?editing.original*1000:Infinity;
    get(id+"-date").value=finite.slice(0,10);input.dataset.minimumDay=localHour(Math.min(original,new Date(minimumExpiry()).getTime())).slice(0,10);
    get(id+"-display").textContent=value===null?tr("无期限"):finite.slice(0,13).replace("T"," ")+tr("时");
    get(id+"-hour").value=finite.slice(11,13);get(id+"-unlimited").checked=value===null;disableExpiry(id);
  }
  function syncExpiry(id){
    const input=get(id),day=get(id+"-date").value,hour=get(id+"-hour").value,value=day&&hour?day+"T"+hour+":00":"";
    input.value=input.dataset.unlimited==="true"?"":value;
    if(value)input.dataset.finiteValue=value;
  }
  function resetIssue(kind){get(kind+"-label").value="";setExpiry(kind+"-expiry",defaultExpiry());if(kind==="quota"){get("quota-budget").value="1";get("quota-weekly").checked=false;}}
  function closeCalendar(focus=true){
    if(!calendar)return;
    const id=calendar.id;calendar=null;get(id+"-unlimited").checked=get(id).dataset.unlimited==="true";get(id+"-calendar").hidden=true;get(id+"-date").setAttribute("aria-expanded","false");if(focus)get(id+"-date").focus();
  }
  function positionCalendar(){
    if(!calendar)return;
    const box=get(calendar.id+"-calendar"),anchor=get(calendar.id+"-date").getBoundingClientRect(),rect=box.getBoundingClientRect(),margin=12;
    box.style.left=Math.max(margin,Math.min(anchor.left,innerWidth-rect.width-margin))+"px";
    box.style.top=(anchor.bottom+6+rect.height<=innerHeight-margin?anchor.bottom+6:Math.max(margin,anchor.top-rect.height-6))+"px";
  }
  function renderCalendar(focus=false){
    if(!calendar)return;
    const {id,month}=calendar,locale=document.documentElement?.lang||"zh-CN",year=month.getFullYear(),index=month.getMonth(),minimum=get(id).dataset.minimumDay;
    get(id+"-month").textContent=new Intl.DateTimeFormat(locale,{year:"numeric",month:"long"}).format(month);
    get(id+"-weekdays").innerHTML=Array.from({length:7},(_,n)=>`<span>${esc(new Intl.DateTimeFormat(locale,{weekday:"short"}).format(new Date(2023,0,1+n)))}</span>`).join("");
    const today=localHour(Date.now()).slice(0,10),selected=calendar.unlimited?null:calendar.day;
    const days=Array.from({length:new Date(year,index,1).getDay()},()=>'<span aria-hidden="true"></span>');
    for(let n=1;n<=new Date(year,index+1,0).getDate();n++){
      const day=localHour(new Date(year,index,n,12).getTime()).slice(0,10);
      days.push(`<button type="button" data-day="${day}" ${day<minimum?"disabled":""} tabindex="${day===calendar.focused?"0":"-1"}" aria-label="${day}" aria-pressed="${day===selected}" ${day===today?'aria-current="date"':""}>${n}</button>`);
    }
    get(id+"-days").innerHTML=days.join("");get(id+"-previous").disabled=localHour(new Date(year,index,0,12).getTime()).slice(0,10)<minimum;
    get(id+"-hours").innerHTML=expiryHours.map(hour=>`<button type="button" data-hour="${hour}" ${selectableExpiry(id,calendar.day,hour)?"":"disabled"} aria-pressed="${!calendar.unlimited&&calendar.hour===hour}">${hour}${label("时")}</button>`).join("");
    get(id+"-unlimited").checked=calendar.unlimited;get(id+"-apply").disabled=calendar.unlimited||!selectableExpiry(id,calendar.day,calendar.hour);
    positionCalendar();if(focus)get(id+"-days").querySelector(`[data-day="${calendar.focused}"]`)?.focus();
  }
  function openCalendar(id){
    if(busy||get(id+"-date").disabled)return;
    if(calendar?.id===id){closeCalendar();return;}
    closeCalendar(false);controller?.abort();
    const day=get(id+"-date").value,focused=day<get(id).dataset.minimumDay?get(id).dataset.minimumDay:day;
    calendar={id,month:new Date(focused+"T12:00"),focused,day,hour:get(id+"-hour").value,unlimited:get(id).dataset.unlimited==="true"};get(id+"-date").setAttribute("aria-expanded","true");get(id+"-calendar").hidden=false;renderCalendar(true);
  }
  function selectableExpiry(id,day,hour){
    if(!expiryHours.includes(hour))return false;
    const original=id==="sharing-inline-expiry"&&editing?.field==="expires_at"?editing.original:undefined;
    try{expiryTime(day+"T"+hour+":00",original);return true;}catch(_){return false;}
  }
  for(const id of expiryIds){
    setExpiry(id,defaultExpiry());get(id+"-calendar").hidden=true;
    get(id+"-date").addEventListener("click",()=>openCalendar(id));
    get(id+"-unlimited").addEventListener("change",()=>{setExpiry(id,get(id+"-unlimited").checked?null:get(id).dataset.finiteValue||defaultExpiry());get("sharing-inline-message").hidden=true;closeCalendar();});
    get(id+"-calendar").addEventListener("click",event=>{
      const button=event.target.closest("[data-month],[data-day],[data-hour],[data-apply]");if(!button||button.disabled||busy||calendar?.id!==id)return;
      if(button.dataset.month){calendar.month=new Date(calendar.month.getFullYear(),calendar.month.getMonth()+Number(button.dataset.month),1,12);calendar.focused=localHour(calendar.month.getTime()).slice(0,10);if(calendar.focused<get(id).dataset.minimumDay)calendar.focused=get(id).dataset.minimumDay;renderCalendar();return;}
      if(button.dataset.apply){if(!selectableExpiry(id,calendar.day,calendar.hour))return;setExpiry(id,calendar.day+"T"+calendar.hour+":00");get("sharing-inline-message").hidden=true;closeCalendar();return;}
      if(button.dataset.hour){if(!selectableExpiry(id,calendar.day,button.dataset.hour))return;calendar.hour=button.dataset.hour;calendar.unlimited=false;renderCalendar();get(id+"-hours").querySelector(`[data-hour="${calendar.hour}"]`)?.focus();return;}
      if(button.dataset.day<get(id).dataset.minimumDay)return;
      calendar.day=button.dataset.day;calendar.focused=calendar.day;calendar.unlimited=false;renderCalendar(true);
    });
    get(id+"-calendar").addEventListener("keydown",event=>{
      if(!calendar||calendar.id!==id||busy)return;
      if(event.key==="Escape"){event.preventDefault();event.stopPropagation();closeCalendar();return;}
      if(event.target?.closest?.("[data-hour],[data-apply]"))return;
      const offsets={ArrowLeft:-1,ArrowRight:1,ArrowUp:-7,ArrowDown:7},day=new Date(calendar.focused+"T12:00");
      if(event.key in offsets)day.setDate(day.getDate()+offsets[event.key]);
      else if(event.key==="Home"||event.key==="End")day.setDate(day.getDate()-day.getDay()+(event.key==="End"?6:0));
      else if(event.key==="PageUp"||event.key==="PageDown"){const n=day.getDate();day.setDate(1);day.setMonth(day.getMonth()+(event.key==="PageUp"?-1:1));day.setDate(Math.min(n,new Date(day.getFullYear(),day.getMonth()+1,0).getDate()));}
      else return;
      event.preventDefault();let value=localHour(day.getTime()).slice(0,10);if(value<get(id).dataset.minimumDay)value=get(id).dataset.minimumDay;
      calendar.focused=value;calendar.month=new Date(value+"T12:00");renderCalendar(true);
    });
  }
  // Keep legacy minute/second precision when the displayed hour has not changed.
  function expiryTime(value,original){if(Number.isFinite(original)&&value===localHour(original*1000))return original;const d=new Date(value),n=value?d.getTime()/1000:NaN;if(!Number.isFinite(n)||n<Date.now()/1000+86400)throw new Error(tr("期限至少为一天，也可选择无期限。"));if(d.getHours()%3||d.getMinutes()||d.getSeconds())throw new Error(tr("请选择 0、3、6、9、12、15、18 或 21 时。"));return n;}
  function expiry(id,original){syncExpiry(id);return get(id).dataset.unlimited==="true"?null:expiryTime(get(id).value,original);}
  function budget(id){const value=get(id).value,n=Number(value);if(!Number.isFinite(n)||n<1||n>1000000)throw new Error(tr("额度应为 1 到 1,000,000 美元。"));return value;}
  function credentialName(kind,value,identifier=null){
    const name=String(value??"").trim();
    if(!name||name.length>80)throw new Error(tr("名称应为 1 到 80 个字符。"));
    const original=(current?.credentials||[]).find(row=>row.id===identifier);
    if(name!==original?.label&&(current?.credentials||[]).some(row=>row.kind===kind&&!row.internal&&row.id!==identifier&&String(row.label??"").trim().toLowerCase()===name.toLowerCase()))throw new Error(tr("同类型凭据名称已存在，请使用其他名称。"));
    return name;
  }
  async function request(data,signal){
    const response=await fetch("/api/sharing",{method:data?"POST":"GET",cache:"no-store",headers:data?{"Content-Type":"application/json"}:{},body:data?JSON.stringify(data):undefined,signal});
    if(response.status===404)throw new Error(tr("当前后台不支持共享，请更新软件后使用。"));
    const result=await response.json();if(!response.ok){if(data?.action==="credential_delete"&&result.error==="invalid sharing action")throw new Error(tr("当前后台不支持删除凭据，请更新软件后重试。"));throw new Error(tr(result.error||"共享操作失败。"));}return result;
  }
  function shownRows(kind){return (current?.credentials||[]).filter(row=>row.kind===kind&&row.direction===views[kind]&&!row.internal);}
  function data(row){return row.direction==="in"?(row.snapshot||{}):row;}
  const partial=row=>(data(row).totals?.missing_responses||0)+(data(row).totals?.unpriced_responses||0)>0;
  const money=(value,rows)=>(rows.some(partial)?"≥ ":"")+dollars(value);
  function isActive(row){return row.direction==="in"&&row.id===(row.kind==="quota"?current.client?.active_id:current.egress?.selection?.active_id);}
  const lending=row=>row.enabled!==false&&(row.kind==="quota"?current?.host?.running===true:current?.egress?.host?.active===true);
  function status(row){const d=data(row);return isActive(row)?row.online===false?"连接不可用":"使用中":d.expired?"已到期":d.enabled===false||(row.direction==="out"&&!lending(row))?"已停用":d.exhausted?"额度已用完":row.direction==="in"?"未使用":"已启用";}
  function fieldValue(row,field,text){
    if(row.direction!=="out"&&field!=="label")return `<span title="${label("由提供方管理")}">${text}</span>`;
    return `<button type="button" class="sharing-cell-value${field==="expires_at"?" sharing-expiry-trigger":""}" data-action="field" data-field="${field}" data-id="${row.id}" aria-controls="sharing-inline-editor" aria-expanded="false" title="${label("点击修改")}">${text}</button>`;
  }
  function weeklyValue(row){
    const value=data(row).weekly_reset;
    if(typeof value!=="boolean")return `<small title="${label("由提供方管理")}">${label("每周刷新")} —</small>`;
    return `<label class="sharing-weekly-check"${row.direction==="in"?` title="${label("由提供方管理")}"`:""}><input type="checkbox" data-action="weekly" data-id="${row.id}"${value?" checked":""}${row.direction==="in"?" disabled":""}>${label("每周刷新")}</label>`;
  }
  function quotaUsage(row){
    const d=data(row),used=d.period_cost_nanos,budget=d.budget_nanos,knownUsed=Number.isFinite(used)&&used>=0,knownBudget=Number.isFinite(budget)&&budget>=0;
    const amount=knownUsed?money(used,[row]):"—",limit=knownBudget?budget===0?tr("不限"):dollars(budget):"—",text=amount+" / "+limit;
    const progress=knownUsed&&knownBudget&&budget>0?`<progress class="sharing-budget-progress" max="${budget}" value="${Math.min(used,budget)}" data-full="${used>=budget}" aria-label="${label("已用 / 额度")}" aria-valuetext="${esc(text)}" title="${esc(text)}"></progress>`:"";
    return `<div class="sharing-budget-cell"><span>${esc(amount)}<small> / ${fieldValue(row,"budget",esc(limit))}</small></span>${progress}</div>`;
  }
  function renderList(kind){
    const rows=shownRows(kind),quota=kind==="quota";
    get(kind+"-credential-count").textContent=number(rows.length);
    get(kind+"-table-head").innerHTML=`<tr>${(quota?["凭据名称","已用 / 额度","累计用量","期限","状态","操作"]:["凭据名称","上传 / 下载流量","连接次数","期限","状态","操作"]).map(value=>`<th>${label(value)}</th>`).join("")}</tr>`;
    get(kind+"-credential-rows").innerHTML=rows.map(row=>{const d=data(row),out=row.direction==="out";
      return `<tr class="${isActive(row)?"current":""}"><td>${fieldValue(row,"label",`<strong title="${esc(row.label)}">${esc(row.label)}</strong>`)}</td><td>${quota?quotaUsage(row):`${esc(bytes(d.totals?.sent_bytes))}<small> / ${esc(bytes(d.totals?.received_bytes))}</small>`}</td><td>${esc(quota?money(d.totals?.cost_nanos,[row]):number(d.totals?.requests))}</td><td><span class="sharing-expiry-cell">${fieldValue(row,"expires_at",esc(d.expires_at===undefined?"—":date(d.expires_at)))}${quota?weeklyValue(row):""}</span></td><td class="sharing-row-status">${out?`<button type="button" class="sharing-cell-value" data-action="toggle" data-id="${row.id}" title="${label(lending(row)?"停用":"启用")}">${label(status(row))}</button>`:label(status(row))}</td><td><div class="sharing-row-actions">${out?`<button type="button" class="sharing-copy-button" data-action="copy" data-id="${row.id}" data-copied="${copiedKey===row.id}" title="${label("复制凭据")}" aria-label="${label("复制凭据")}">${copyCaption("凭据")}</button>${quota?`<button type="button" data-action="reset" data-id="${row.id}">${label("重置")}</button>`:""}`:`<button type="button" data-action="use" data-id="${row.id}" ${isActive(row)?"disabled":""}>${label(isActive(row)?"使用中":"使用")}</button>`}<button type="button" class="sharing-row-delete" data-action="delete" data-id="${row.id}" title="${label("删除凭据")}">${label("删除")}</button></div></td></tr>`;
    }).join("");
    get(kind+"-empty").hidden=rows.length>0;
  }
  function render(value){
    if(!value.credentials_supported||value.protocol<4){closeInline(false);closeDelete(false);current=null;get("sharing-cards").hidden=true;message("当前后台不支持凭据管理，请更新软件。",true);return;}
    current=value;if(editing||deleting)return;get("sharing-cards").hidden=false;
    const hosting=value.host?.running===true,borrowing=value.client?.active===true;
    for(const kind of ["quota","exit"]){const out=views[kind]==="out";get(kind+"-issue").hidden=!out;get(kind+"-borrow").hidden=out;
      get(kind+"-generate").hidden=!out;get(kind+"-activate").hidden=out;
      for(const button of get("sharing-cards").querySelectorAll(`[data-view][data-kind="${kind}"]`))button.setAttribute("aria-pressed",String(button.dataset.view===views[kind]));
      get(kind+"-generate").disabled=busy||(kind==="quota"&&borrowing);get(kind+"-activate").disabled=busy||(kind==="quota"&&hosting);
      get(kind+"-generate").setAttribute("data-copied",String(copiedKey===kind+"-generate"));
    }
    get("quota-local").hidden=!borrowing;get("quota-local").disabled=busy;get("exit-local").hidden=views.exit!=="in";
    for(const button of get("exit-local").querySelectorAll("button")){button.disabled=busy;button.setAttribute("aria-pressed",String(button.dataset.action===value.egress?.selection?.mode));}
    for(const kind of ["quota","exit"])renderList(kind);
    if(busy)for(const kind of ["quota","exit"])for(const input of get(kind+"-credential-rows").querySelectorAll("button,input"))input.disabled=true;
    if(!recoveryShown&&(value.recovery_error||value.recovered)){recoveryShown=true;message(value.recovery_error||"上次共享已结束，已恢复原连接。请重新加载 Codex 配置。",!!value.recovery_error);}
  }
  async function action(button,dataValue,success=""){if(busy)return;clearCopied();controller?.abort();busy=true;button?.setAttribute("aria-busy","true");if(button)button.disabled=true;message("");
    try{const result=await request(dataValue),{link,...state}=result;
      render(state);if(link)await copy(link,dataValue.action==="credential_issue"?dataValue.kind+"-generate":dataValue.id);else if(result.reload_required===true)message(dataValue.action==="credential_delete"?"已删除凭据。请重新加载 Codex 配置或重启 ChatGPT 应用。":"已切换额度来源。请重新加载 Codex 配置或重启 ChatGPT 应用。");else message(success===null?"":success||"已更新，新请求使用当前设置。");return result;
    }catch(error){message(error.message,true);}finally{busy=false;button?.setAttribute("aria-busy","false");if(button)button.disabled=false;if(current)render(current);}}
  async function refresh(){if(!active||document.hidden||busy||editing||deleting||calendar)return;controller?.abort();const task=new AbortController();controller=task;try{const value=await request(null,task.signal);if(active&&!task.signal.aborted)render(value);}catch(error){if(error.name!=="AbortError")message(error.message,true);}finally{if(controller===task)controller=null;}}
  function positionInline(){
    if(!editing)return;
    const box=get("sharing-inline-editor"),anchor=editing.anchor.getBoundingClientRect(),rect=box.getBoundingClientRect(),margin=12;
    const left=Math.max(margin,Math.min(anchor.left,innerWidth-rect.width-margin));
    const top=anchor.bottom+6+rect.height<=innerHeight-margin?anchor.bottom+6:Math.max(margin,anchor.top-rect.height-6);
    box.style.left=left+"px";box.style.top=top+"px";
  }
  function closeInline(renderNow=true){
    closeCalendar(false);
    if(!editing)return;
    const draft=editing;draft.anchor.setAttribute("aria-expanded","false");editing=null;get("sharing-inline-editor").hidden=true;
    get("sharing-inline-message").hidden=true;if(renderNow&&current){render(current);get("sharing-cards").querySelector(`[data-action="field"][data-id="${draft.id}"][data-field="${draft.field}"]`)?.focus();}
  }
  function openInline(row,field,anchor){
    if(busy||!["label","budget","expires_at"].includes(field)||(row.direction!=="out"&&field!=="label")||(field==="budget"&&row.kind!=="quota"))return;
    controller?.abort();closeInline(false);message("");
    const original=field==="label"?row.label:field==="budget"?row.budget_nanos/1e9:row.expires_at;
    editing={id:row.id,kind:row.kind,field,original,anchor};anchor.setAttribute("aria-expanded","true");
    const input=get("sharing-inline-value"),box=get("sharing-inline-editor"),isDate=field==="expires_at";
    input.type=field==="budget"?"number":"text";input.maxLength=80;input.step="any";
    input.min=field==="budget"?String(Math.min(original,1)):"";
    input.max=field==="budget"?"1000000":"";input.autocomplete="off";input.setCustomValidity("");input.disabled=isDate;input.hidden=isDate;input.required=!isDate;
    input.value=isDate?"":original??"";
    get("sharing-inline-label").textContent=tr(field==="label"?"名称":field==="budget"?"额度":"期限")+(field==="budget"?" · "+tr("美元"):"");
    get("sharing-inline-label").setAttribute("for",isDate?"sharing-inline-expiry-date":"sharing-inline-value");
    get("sharing-inline-expiry-container").hidden=!isDate;
    if(isDate){
      setExpiry("sharing-inline-expiry",Number.isFinite(original)?localHour(original*1000):defaultExpiry());
      if(original===null)setExpiry("sharing-inline-expiry",null);
    }
    disableExpiry("sharing-inline-expiry",!isDate);
    get("sharing-inline-save").textContent=tr(isDate?"确定":"保存");
    box.dataset.field=field;box.hidden=false;positionInline();
    (isDate?get("sharing-inline-expiry-date"):input).focus();if(field==="label")input.select();
  }
  get("sharing-inline-value").addEventListener("input",()=>{get("sharing-inline-value").setCustomValidity("");get("sharing-inline-message").hidden=true;});
  get("sharing-inline-cancel").addEventListener("click",()=>{if(!busy)closeInline();});
  async function saveInline(){
    if(!editing||busy)return;
    const draft=editing,input=get("sharing-inline-value");
    try{
      let value;
      if(draft.field==="label")value=credentialName(draft.kind,input.value,draft.id);
      else if(draft.field==="budget")value=Number(input.value)===draft.original?draft.original:budget("sharing-inline-value");
      else value=expiry("sharing-inline-expiry",draft.original);
      if(value===draft.original){closeInline();return;}
      input.disabled=true;disableExpiry("sharing-inline-expiry",true);get("sharing-inline-cancel").disabled=true;
      try{
        const result=await action(get("sharing-inline-save"),{action:"credential_edit",id:draft.id,[draft.field]:value},null);
        if(result&&editing===draft)closeInline();else positionInline();
      }finally{input.disabled=draft.field==="expires_at";disableExpiry("sharing-inline-expiry",draft.field!=="expires_at");get("sharing-inline-cancel").disabled=false;}
    }catch(error){message(error.message,true);positionInline();}
  }
  get("sharing-inline-editor").addEventListener("submit",async event=>{event.preventDefault();await saveInline();});
  document.addEventListener("pointerdown",event=>{if(calendar&&!busy&&!get(calendar.id+"-calendar").contains(event.target)&&!get(calendar.id+"-date").contains(event.target))closeCalendar(false);if(editing&&!busy&&!get("sharing-inline-editor").contains(event.target)&&!event.target.closest("[data-action=field]"))closeInline(false);});
  document.addEventListener("keydown",event=>{if(!busy&&event.key==="Escape"){if(calendar){event.preventDefault();closeCalendar();}else if(editing){event.preventDefault();closeInline();}}});
  const positionEditors=()=>{positionInline();positionCalendar();};
  globalThis.addEventListener("resize",positionEditors);
  document.addEventListener("scroll",positionEditors,true);
  function openDelete(row){
    controller?.abort();closeInline(false);message("");deleting={id:row.id,kind:row.kind};
    get("sharing-delete-name").textContent=[tr(row.kind==="quota"?"额度":"出口"),tr(row.direction==="out"?"借出":"借入"),row.label].join(" · ");
    get("sharing-delete-description").textContent=tr(row.direction==="out"?"删除后该凭据将失效，其用量记录也会移除，无法撤销。":"仅移除本机保存的凭据和用量记录，不影响对方的凭据。");
    const hint=get("sharing-delete-hint");hint.hidden=!isActive(row);hint.textContent=isActive(row)?tr(row.kind==="quota"?"此凭据正在使用，删除后将停止借用并恢复原连接，需要重新加载 Codex 配置或重启 ChatGPT 应用。":"此凭据正在使用，删除后将恢复默认出口。"):"";
    get("sharing-delete-dialog").showModal();get("sharing-delete-cancel").focus();
  }
  function closeDelete(renderNow=true){
    if(!deleting)return;
    const draft=deleting;deleting=null;get("sharing-delete-dialog").close();get("sharing-delete-message").hidden=true;
    if(renderNow&&current){render(current);const target=get("sharing-cards").querySelector(`[data-action="delete"][data-id="${draft.id}"]`)||get(draft.kind+"-credential-rows").querySelector("[data-action=delete]")||get(draft.kind+"-section-title");target.focus();}
  }
  get("sharing-delete-cancel").addEventListener("click",()=>{if(!busy)closeDelete();});
  get("sharing-delete-dialog").addEventListener("cancel",event=>{event.preventDefault();if(!busy)closeDelete();});
  get("sharing-delete-dialog").addEventListener("close",()=>{if(deleting&&!get("sharing-delete-dialog").open)closeDelete();});
  get("sharing-delete-confirm").addEventListener("click",async()=>{
    if(!deleting||busy)return;
    const draft=deleting,dialog=get("sharing-delete-dialog");get("sharing-delete-cancel").disabled=true;dialog.setAttribute("aria-busy","true");
    try{const result=await action(get("sharing-delete-confirm"),{action:"credential_delete",id:draft.id},"已删除凭据。");if(result&&deleting===draft)closeDelete();}
    finally{get("sharing-delete-cancel").disabled=false;dialog.setAttribute("aria-busy","false");}
  });
  async function copy(value,key){try{await navigator.clipboard.writeText(value);copiedKey=key;copyTimer=setTimeout(clearCopied,1800);message("");}catch(_){message("复制失败，请点击右侧“凭据”重试。",true);}}
  for(const kind of ["quota","exit"]){
    get(kind+"-issue").addEventListener("submit",async event=>{event.preventDefault();try{const result=await action(get(kind+"-generate"),{action:"credential_issue",kind,label:credentialName(kind,get(kind+"-label").value),expires_at:expiry(kind+"-expiry"),...(kind==="quota"?{budget:budget("quota-budget"),weekly_reset:get("quota-weekly").checked}:{})},null);if(result)resetIssue(kind);}catch(error){message(error.message,true);}});
    get(kind+"-borrow").addEventListener("submit",async event=>{event.preventDefault();const result=await action(get(kind+"-activate"),{action:"credential_use",kind,label:get(kind+"-borrow-label").value.trim(),link:get(kind+"-borrow-link").value.trim()});if(result)get(kind+"-borrow-link").value="";});
  }
  get("sharing-cards").addEventListener("click",async event=>{const button=event.target.closest("[data-view],[data-action]");if(!button||busy)return;const {kind,view,action:command}=button.dataset;
    if(view){closeInline(false);views[kind]=view;if(current)render(current);return;}
    if(command==="local"||command==="provider"){await action(button,kind==="quota"?{action:"client_disconnect"}:{action:"egress_select",mode:command==="provider"?"provider":"local"});return;}
    const row=(current?.credentials||[]).find(row=>row.id===button.dataset.id);if(!row)return;
    if(command==="weekly")return;
    if(command==="delete"){openDelete(row);return;}
    if(command==="field"){openInline(row,button.dataset.field,button);return;}if(editing)closeInline(false);if(command==="copy"){await action(button,{action:"credential_copy",id:row.id});return;}
    if(command==="reset")await action(button,{action:"credential_reset",id:row.id},"本期额度已重置，累计统计已保留。");
    if(command==="toggle")await action(button,{action:"credential_edit",id:row.id,enabled:!lending(row)});
    if(command==="use")await action(button,{action:"credential_use",kind:row.kind,id:row.id});
  });
  get("sharing-cards").addEventListener("change",async event=>{
    const input=event.target;if(input.dataset.action!=="weekly")return;
    const row=(current?.credentials||[]).find(row=>row.id===input.dataset.id);
    if(busy||!row||row.direction!=="out"||row.kind!=="quota"){if(row)input.checked=data(row).weekly_reset===true;return;}
    const value=input.checked;if(editing)closeInline(false);
    await action(input,{action:"credential_edit",id:row.id,weekly_reset:value},null);
  });
  document.addEventListener("visibilitychange",()=>{if(document.hidden)controller?.abort();else if(active)refresh();});
  function setActive(value){active=value;clearInterval(timer);timer=null;if(!active){controller?.abort();clearCopied();closeInline(false);closeDelete(false);return;}message("");refresh();timer=setInterval(refresh,3000);}
  return {setActive};
})();
if(typeof workspaceView!=="undefined"&&workspaceView==="sharing")globalThis.InspectorSharing.setActive(true);
