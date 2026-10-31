"use strict";

globalThis.InspectorSharing=(()=>{
  const get=id=>document.getElementById(id),tr=value=>typeof t==="function"?t(value):value;
  const esc=value=>String(value??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const label=value=>esc(tr(value)),number=value=>Number(value||0).toLocaleString();
  const dollars=n=>"$"+(Number(n||0)/1e9).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:4});
  const bytes=n=>{n=Number(n||0);return n<1000?`${number(n)} B`:n<1e6?`${(n/1000).toFixed(1)} KB`:`${(n/1e6).toFixed(2)} MB`;};
  const localDay=n=>{const d=new Date(n);return new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,10);};
  const expiryDay=n=>{const d=new Date(n*1000);if(!d.getHours()&&!d.getMinutes()&&!d.getSeconds()&&!d.getMilliseconds())d.setDate(d.getDate()-1);return localDay(d.getTime());};
  const date=n=>n===null?tr("无期限"):Number.isFinite(n)?expiryDay(n):"—";
  const minimumExpiry=()=>expiryDay((Date.now()+864e5)/1000);
  const defaultExpiry=()=>{const d=new Date();d.setDate(d.getDate()+7);return localDay(d.getTime());};
  function dayEnd(day){
    if(!/^\d{4}-\d{2}-\d{2}$/.test(day))return NaN;
    const d=new Date(day+"T00:00");if(!Number.isFinite(d.getTime())||localDay(d.getTime())!==day)return NaN;
    d.setDate(d.getDate()+1);d.setHours(0,0,0,0);return d.getTime()/1000;
  }
  let active=false,busy=false,current=null,timer=null,controller=null,recoveryShown=false,editing=null,deleting=null,calendar=null,dragging=null;
  let recentId=null,recentTimer=null,recentScroll=false;
  const views={quota:"out",exit:"out"};
  const message=(value,error=false,anchor="")=>{
    const box=get("sharing-message");box.textContent=tr(value);box.hidden=!value;box.classList.toggle("error",error);
    box.dataset.feedbackAnchor=error?"":anchor;box.dataset.feedbackDuration=anchor&&!error?"2400":"";
    for(const id of ["sharing-inline-message","sharing-calendar-message","sharing-delete-message"]){const local=get(id);local.textContent=error?tr(value):"";local.hidden=!error;}
  };
  const expiryIds=["quota-expiry","exit-expiry","sharing-inline-expiry"];
  const editingExpiry=id=>id==="sharing-inline-expiry"&&editing?.field==="expires_at";
  const resetWeekdays=["周一","周二","周三","周四","周五","周六","周日"];
  const weeklyPickerOpen=()=>globalThis.CSS?.supports?.("selector(select:open)")&&get("sharing-cards").querySelector(".sharing-weekly-select:open");
  const weeklyLabel=value=>value===null||value===""?tr("不刷新"):tr(resetWeekdays[value])+" 00:00";
  function syncWeeklyLabel(input){const display=input.parentElement?.querySelector(".sharing-weekly-value");if(display)display.textContent=weeklyLabel(input.value);}
  function weeklyOptions(value=null){
    const selected=value===null?"":String(value);
    return [["",esc(weeklyLabel(null))],...resetWeekdays.map((_,index)=>[String(index),esc(weeklyLabel(index))])].map(([key,text])=>`<option value="${key}"${key===selected?" selected":""}>${text}</option>`).join("");
  }
  function weeklyDay(row){const d=data(row);return typeof d.weekly_reset!=="boolean"?undefined:d.weekly_reset?(d.weekly_reset_day??0):null;}
  function weeklySettings(value){
    if(value!==""&&!/^[0-6]$/.test(value))throw Error(tr("请选择周一至周日，或选择不刷新。"));
    const day=value===""?null:Number(value);
    if(day!==null&&day!==0&&!current?.weekly_reset_days_supported)throw Error(tr("当前后台不支持选择刷新日期，请更新软件。"));
    return {weekly_reset:day!==null,...(current?.weekly_reset_days_supported?{weekly_reset_day:day}:{})};
  }
  function expiryControl(id,labelId){
    return `<div class="sharing-expiry-control" role="group" aria-labelledby="${labelId}"><input id="${id}" type="hidden"><button id="${id}-date" class="sharing-expiry-date" type="button" aria-haspopup="dialog" aria-expanded="false" aria-controls="${id}-calendar" aria-labelledby="${labelId} ${id}-display"><span id="${id}-display"></span><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" aria-hidden="true"><rect x="2.5" y="3.5" width="11" height="10" rx="1.5"/><path d="M5 2v3m6-3v3M3 7h10"/></svg></button>
      <div id="${id}-calendar" class="sharing-calendar" role="dialog" aria-labelledby="${id}-month" hidden><div class="sharing-calendar-header"><button id="${id}-previous" type="button" data-month="-1" aria-label="${label("上个月")}">‹</button><strong id="${id}-month" aria-live="polite"></strong><button type="button" data-month="1" aria-label="${label("下个月")}">›</button></div><div id="${id}-weekdays" class="sharing-calendar-weekdays" aria-hidden="true"></div><div id="${id}-days" class="sharing-calendar-days" role="group" aria-labelledby="${id}-month"></div><div class="sharing-calendar-footer"><button id="${id}-apply" class="primary" type="button" data-apply="true">${label("确定")}</button><label class="sharing-expiry-unlimited"><input id="${id}-unlimited" type="checkbox">${label("无期限")}</label></div>${id==="sharing-inline-expiry"?'<p id="sharing-calendar-message" class="sharing-edit-message" role="alert" hidden></p>':""}</div></div>`;
  }
  function expiryField(id){
    return `<div class="sharing-expiry"><label id="${id}-label" for="${id}-date">${label("期限")}</label>${expiryControl(id,id+"-label")}</div>`;
  }
  function clearRecent(){
    clearTimeout(recentTimer);recentTimer=null;recentId=null;recentScroll=false;
    for(const row of get("sharing-cards").querySelectorAll(".sharing-recent"))row.classList.remove("sharing-recent");
  }
  function highlightCredential(identifier,state){
    if(!identifier||!state.credentials?.some(row=>row.id===identifier))return;
    clearRecent();recentId=identifier;recentScroll=true;recentTimer=setTimeout(clearRecent,3500);
  }
  function card(kind){
    return `<div class="sharing-card" data-kind="${kind}">
      <form id="${kind}-issue" class="sharing-form"><label>${label("名称")}<input id="${kind}-label" maxlength="80" required autocomplete="off"></label>
        ${expiryField(kind+"-expiry")}
        ${kind==="quota"?`<div class="sharing-budget-row"><label id="quota-budget-label" for="quota-budget">${label("额度")}</label><span class="sharing-amount-field"><input id="quota-budget" type="number" value="1" min="1" max="1000000" step="0.01" required aria-labelledby="quota-budget-label" aria-describedby="quota-budget-unit" title="${label("最低 1 美元。")}"><span id="quota-budget-unit" class="sharing-amount-unit">${label("美元")}</span></span></div><div class="sharing-weekly-row"><label for="quota-weekly">${label("每周刷新")}</label><select id="quota-weekly" class="sharing-weekly-select" title="${label("按提供方本机时间，在所选当天零点刷新。")}">${weeklyOptions()}</select></div>`:""}
      </form>
      <form id="${kind}-borrow" class="sharing-form" hidden><label>${label("名称")}<input id="${kind}-borrow-label" maxlength="80" required autocomplete="off"></label><label>${label("凭据")}<input id="${kind}-borrow-link" type="password" autocomplete="off" spellcheck="false" required placeholder="${kind==="quota"?"codex-share":"codex-exit"}://…"></label></form>
      <div class="sharing-actions"><button class="primary sharing-copy-button" type="submit" form="${kind}-issue" id="${kind}-generate">${label("生成凭据")}</button><button class="primary" type="submit" form="${kind}-borrow" id="${kind}-activate" hidden>${label("保存凭据")}</button></div>
      ${kind==="quota"?`<button id="quota-local" class="sharing-local" type="button" data-action="local" data-kind="quota">${label("使用本机额度")}</button>`:""}
    </div>`;
  }
  for(const kind of ["quota","exit"])get(kind+"-config").innerHTML=card(kind);
  get("sharing-inline-expiry-container").innerHTML=expiryControl("sharing-inline-expiry","sharing-inline-label");
  function disableExpiry(id,disabled=false){
    get(id+"-unlimited").disabled=disabled;
    get(id+"-date").disabled=disabled;
  }
  function setExpiry(id,value,modified){
    const input=get(id),finite=value??input.dataset.finiteValue??defaultExpiry();
    input.dataset.finiteValue=finite;input.value=value??"";input.dataset.unlimited=String(value===null);
    if(modified!==undefined)input.dataset.modified=String(modified);
    get(id+"-date").value=finite;input.dataset.minimumDay=minimumExpiry();
    const original=id==="sharing-inline-expiry"&&editing?.field==="expires_at"?editing.original:undefined;
    const prior=Number.isFinite(original)&&finite===expiryDay(original)?new Date(original*1000):null;
    const clock=prior&&(prior.getHours()||prior.getMinutes()||prior.getSeconds()||prior.getMilliseconds())
      ?String(prior.getHours()).padStart(2,"0")+":"+String(prior.getMinutes()).padStart(2,"0"):"24:00";
    get(id+"-display").textContent=value===null?tr("无期限"):finite+" "+clock;
    get(id+"-unlimited").checked=value===null;disableExpiry(id);
  }
  function resetIssue(kind){get(kind+"-label").value="";setExpiry(kind+"-expiry",defaultExpiry(),false);if(kind==="quota"){get("quota-budget").value="1";get("quota-weekly").value="";}}
  function closeCalendar(focus=true){
    if(!calendar)return;
    const id=calendar.id;calendar=null;get(id+"-unlimited").checked=get(id).dataset.unlimited==="true";get(id+"-calendar").hidden=true;get(id+"-date").setAttribute("aria-expanded","false");
    if(focus){if(editingExpiry(id))closeInline();else get(id+"-date").focus();}
  }
  function positionCalendar(){
    if(!calendar)return;
    const box=get(calendar.id+"-calendar"),anchor=(editingExpiry(calendar.id)?editing.anchor:get(calendar.id+"-date")).getBoundingClientRect(),rect=box.getBoundingClientRect(),margin=12;
    box.style.left=Math.max(margin,Math.min(anchor.left,innerWidth-rect.width-margin))+"px";
    box.style.top=(anchor.bottom+6+rect.height<=innerHeight-margin?anchor.bottom+6:Math.max(margin,anchor.top-rect.height-6))+"px";
  }
  function renderCalendar(focus=false){
    if(!calendar)return;
    const {id,month}=calendar,locale=document.documentElement?.lang||"zh-CN",year=month.getFullYear(),index=month.getMonth(),minimum=get(id).dataset.minimumDay;
    get(id+"-month").textContent=new Intl.DateTimeFormat(locale,{year:"numeric",month:"long"}).format(month);
    get(id+"-weekdays").innerHTML=Array.from({length:7},(_,n)=>`<span>${esc(new Intl.DateTimeFormat(locale,{weekday:"short"}).format(new Date(2023,0,1+n)))}</span>`).join("");
    const today=localDay(Date.now()),selected=calendar.unlimited?null:calendar.day;
    const days=Array.from({length:new Date(year,index,1).getDay()},()=>'<span aria-hidden="true"></span>');
    for(let n=1;n<=new Date(year,index+1,0).getDate();n++){
      const day=localDay(new Date(year,index,n,12).getTime());
      days.push(`<button type="button" data-day="${day}" ${day<minimum?"disabled":""} tabindex="${day===calendar.focused?"0":"-1"}" aria-label="${day}" aria-pressed="${day===selected}" ${day===today?'aria-current="date"':""}>${n}</button>`);
    }
    get(id+"-days").innerHTML=days.join("");get(id+"-previous").disabled=localDay(new Date(year,index,0,12).getTime())<minimum;
    get(id+"-unlimited").checked=calendar.unlimited;get(id+"-apply").disabled=calendar.unlimited?!editingExpiry(id):!selectableExpiry(calendar.day,calendarOriginal(id));
    positionCalendar();if(focus)get(id+"-days").querySelector(`[data-day="${calendar.focused}"]`)?.focus();
  }
  function openCalendar(id){
    if(busy||get(id+"-date").disabled)return;
    if(calendar?.id===id){closeCalendar();return;}
    closeCalendar(false);controller?.abort();
    const day=get(id+"-date").value,focused=day<get(id).dataset.minimumDay?get(id).dataset.minimumDay:day;
    calendar={id,month:new Date(focused+"T12:00"),focused,day,unlimited:get(id).dataset.unlimited==="true",modified:false};get(id+"-date").setAttribute("aria-expanded","true");get(id+"-calendar").hidden=false;renderCalendar(true);
  }
  function calendarOriginal(id){return editingExpiry(id)&&!calendar?.modified&&get(id).dataset.modified!=="true"?editing.original:undefined;}
  function selectableExpiry(day,original){
    try{expiryTime(day,original);return true;}catch(_){return false;}
  }
  for(const id of expiryIds){
    setExpiry(id,defaultExpiry(),false);get(id+"-calendar").hidden=true;
    get(id+"-date").addEventListener("click",()=>openCalendar(id));
    get(id+"-unlimited").addEventListener("change",()=>{
      if(editingExpiry(id)&&calendar?.id===id){calendar.unlimited=get(id+"-unlimited").checked;get("sharing-calendar-message").hidden=true;renderCalendar();return;}
      setExpiry(id,get(id+"-unlimited").checked?null:get(id).dataset.finiteValue||defaultExpiry());get("sharing-inline-message").hidden=true;closeCalendar();
    });
    get(id+"-calendar").addEventListener("click",async event=>{
      const button=event.target.closest("[data-month],[data-day],[data-apply]");if(!button||button.disabled||busy||calendar?.id!==id)return;
      if(button.dataset.month){calendar.month=new Date(calendar.month.getFullYear(),calendar.month.getMonth()+Number(button.dataset.month),1,12);calendar.focused=localDay(calendar.month.getTime());if(calendar.focused<get(id).dataset.minimumDay)calendar.focused=get(id).dataset.minimumDay;renderCalendar();return;}
      if(button.dataset.apply){
        if(!(editingExpiry(id)&&calendar.unlimited)&&!selectableExpiry(calendar.day,calendarOriginal(id)))return;
        setExpiry(id,calendar.unlimited?null:calendar.day,editingExpiry(id)?calendar.modified||get(id).dataset.modified==="true":true);
        get("sharing-inline-message").hidden=true;get("sharing-calendar-message").hidden=true;
        if(editingExpiry(id))await saveInline();else closeCalendar();return;
      }
      if(button.dataset.day<get(id).dataset.minimumDay)return;
      calendar.day=button.dataset.day;calendar.focused=calendar.day;calendar.unlimited=false;calendar.modified=true;get("sharing-calendar-message").hidden=true;renderCalendar(true);
    });
    get(id+"-calendar").addEventListener("keydown",event=>{
      if(!calendar||calendar.id!==id||busy)return;
      if(event.key==="Escape"){event.preventDefault();event.stopPropagation();closeCalendar();return;}
      if(event.target?.closest?.("[data-apply]"))return;
      const offsets={ArrowLeft:-1,ArrowRight:1,ArrowUp:-7,ArrowDown:7},day=new Date(calendar.focused+"T12:00");
      if(event.key in offsets)day.setDate(day.getDate()+offsets[event.key]);
      else if(event.key==="Home"||event.key==="End")day.setDate(day.getDate()-day.getDay()+(event.key==="End"?6:0));
      else if(event.key==="PageUp"||event.key==="PageDown"){const n=day.getDate();day.setDate(1);day.setMonth(day.getMonth()+(event.key==="PageUp"?-1:1));day.setDate(Math.min(n,new Date(day.getFullYear(),day.getMonth()+1,0).getDate()));}
      else return;
      event.preventDefault();let value=localDay(day.getTime());if(value<get(id).dataset.minimumDay)value=get(id).dataset.minimumDay;
      calendar.focused=value;calendar.month=new Date(value+"T12:00");renderCalendar(true);
    });
  }
  // Preserve an existing timestamp until the user explicitly chooses a date.
  function expiryTime(value,original){if(Number.isFinite(original)&&value===expiryDay(original))return original;const n=dayEnd(value);if(!Number.isFinite(n)||n<Date.now()/1000+86400)throw new Error(tr("期限至少为一天，也可选择无期限。"));return n;}
  function expiry(id,original){const input=get(id);return input.dataset.unlimited==="true"?null:expiryTime(get(id+"-date").value,input.dataset.modified==="true"?undefined:original);}
  function budget(id,minimum=1){const value=String(get(id).value),n=Number(value);if(!value.trim()||!Number.isFinite(n)||n<minimum||n>1000000)throw new Error(tr(minimum===0?"已用额度应为 0 到 1,000,000 美元。":"额度应为 1 到 1,000,000 美元。"));return value;}
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
    const result=await response.json();if(!response.ok){if(data?.action==="credential_delete"&&result.error==="invalid sharing action")throw new Error(tr("当前后台不支持删除凭据，请更新软件后重试。"));if(data?.action==="credential_reorder"&&result.error==="invalid sharing action")throw new Error(tr("当前后台不支持凭据排序，请更新软件后重试。"));throw new Error(tr(result.error||"共享操作失败。"));}return result;
  }
  function shownRows(kind){return (current?.credentials||[]).filter(row=>row.kind===kind&&row.direction===views[kind]&&!row.internal);}
  function data(row){return row.direction==="in"?(row.snapshot||{}):row;}
  const partial=row=>(data(row).totals?.missing_responses||0)+(data(row).totals?.unpriced_responses||0)>0;
  const money=(value,rows)=>(rows.some(partial)?"≥ ":"")+dollars(value);
  function isActive(row){return row.direction==="in"&&row.id===(row.kind==="quota"?current.client?.active_id:current.egress?.selection?.active_id);}
  const lending=row=>row.enabled!==false&&(row.kind==="quota"?current?.host?.running===true:current?.egress?.host?.active===true);
  function status(row){return (row.direction==="out"?lending(row):isActive(row))?"启用中":"停用中";}
  function fieldValue(row,field,text){
    if(row.direction!=="out"&&field!=="label")return `<span title="${label("由提供方管理")}">${text}</span>`;
    const handle=field==="label"?`<button type="button" class="sharing-drag-handle" draggable="true" data-drag-id="${row.id}" title="${label("拖动排序")}" aria-label="${label("拖动排序")} · ${esc(row.label)}" aria-keyshortcuts="Alt+ArrowUp Alt+ArrowDown"><svg viewBox="0 0 12 18" aria-hidden="true"><circle cx="3" cy="4" r="1"/><circle cx="9" cy="4" r="1"/><circle cx="3" cy="9" r="1"/><circle cx="9" cy="9" r="1"/><circle cx="3" cy="14" r="1"/><circle cx="9" cy="14" r="1"/></svg></button>`:"";
    return handle+`<button type="button" class="sharing-cell-value${field==="expires_at"?" sharing-expiry-trigger":""}" data-action="field" data-field="${field}" data-id="${row.id}" aria-controls="${field==="expires_at"?"sharing-inline-expiry-calendar":"sharing-inline-editor"}" aria-expanded="false" title="${label("点击修改")}">${text}</button>`;
  }
  function weeklyValue(row){
    const value=weeklyDay(row);
    if(value===undefined)return `<small title="${label("由提供方管理")}">—</small>`;
    return `<span class="sharing-weekly-control"><span class="sharing-weekly-value" aria-hidden="true">${esc(weeklyLabel(value))}</span><select class="sharing-weekly-select" data-action="weekly" data-id="${row.id}" aria-label="${label("每周刷新")}" title="${label(row.direction==="in"?"由提供方管理":"按提供方本机时间，在所选当天零点刷新。")}"${row.direction==="in"?" disabled":""}>${weeklyOptions(value)}</select></span>`;
  }
  function quotaUsage(row){
    const d=data(row),used=d.period_cost_nanos,budget=d.budget_nanos,knownUsed=Number.isFinite(used)&&used>=0,knownBudget=Number.isFinite(budget)&&budget>=0;
    const amount=knownUsed?money(used,[row]):"—",limit=knownBudget?budget===0?tr("不限"):dollars(budget):"—",text=amount+" / "+limit;
    const progress=knownUsed&&knownBudget&&budget>0?`<progress class="sharing-budget-progress" max="${budget}" value="${Math.min(used,budget)}" data-full="${used>=budget}" aria-label="${label("已用 / 额度")}" aria-valuetext="${esc(text)}" title="${esc(text)}"></progress>`:"";
    return `<div class="sharing-budget-cell"><span><span class="sharing-value sharing-used">${knownUsed?fieldValue(row,"used",esc(amount)):"—"}</span><span class="sharing-value-separator"> / </span><span class="sharing-value sharing-limit">${fieldValue(row,"budget",esc(limit))}</span></span>${progress}</div>`;
  }
  function renderList(kind){
    const rows=shownRows(kind),quota=kind==="quota";
    get(kind+"-credential-count").textContent=number(rows.length);
    get(kind+"-table-head").innerHTML=`<tr>${(quota?["凭据名称","额度使用","累计用量","期限","每周刷新","状态","操作"]:["凭据名称","上传 / 下载流量","连接次数","期限","状态","操作"]).map(value=>`<th${["凭据名称","期限","每周刷新","状态","操作"].includes(value)?` class="sharing-column-compact${value==="状态"?" sharing-column-status":""}"`:""}>${label(value)}</th>`).join("")}</tr>`;
    get(kind+"-credential-rows").innerHTML=rows.map(row=>{const d=data(row),out=row.direction==="out";
      return `<tr class="${[isActive(row)?"current":"",recentId===row.id?"sharing-recent":""].filter(Boolean).join(" ")}" data-credential-id="${esc(row.id)}"><td>${fieldValue(row,"label",`<strong title="${esc(row.label)}">${esc(row.label)}</strong>`)}</td><td>${quota?quotaUsage(row):`<span class="sharing-value sharing-upload" title="${esc(bytes(d.totals?.sent_bytes))}">${esc(bytes(d.totals?.sent_bytes))}</span><span class="sharing-value-separator"> / </span><span class="sharing-value sharing-download" title="${esc(bytes(d.totals?.received_bytes))}">${esc(bytes(d.totals?.received_bytes))}</span>`}</td><td>${esc(quota?money(d.totals?.cost_nanos,[row]):number(d.totals?.requests))}</td><td><span class="sharing-expiry-cell">${fieldValue(row,"expires_at",esc(d.expires_at===undefined?"—":date(d.expires_at)))}</span></td>${quota?`<td class="sharing-weekly-cell">${weeklyValue(row)}</td>`:""}<td class="sharing-row-status" data-state="${(out?lending(row):isActive(row))?"enabled":"disabled"}">${out?`<button type="button" class="sharing-cell-value" data-action="toggle" data-id="${row.id}" data-state="${d.expired||d.exhausted?"warn":lending(row)?"enabled":"disabled"}" title="${label(lending(row)?"停用":"启用")}">${label(status(row))}</button>`:label(status(row))}</td><td><div class="sharing-row-actions">${out?`<button type="button" id="sharing-copy-${row.id}" class="sharing-copy-button" data-action="copy" data-id="${row.id}" title="${label("复制凭据")}" aria-label="${label("复制凭据")}">${label("凭据")}</button>`:`<button type="button" data-action="use" data-id="${row.id}" ${quota&&isActive(row)?"disabled":""}>${label(quota?(isActive(row)?"使用中":"使用"):(isActive(row)?"停用":"启用"))}</button>`}<button type="button" class="sharing-row-delete" data-action="delete" data-id="${row.id}" title="${label("删除凭据")}">${label("删除")}</button></div></td></tr>`;
    }).join("");
    get(kind+"-empty").hidden=rows.length>0;
  }
  function render(value){
    if(!value.credentials_supported||value.protocol<4){closeInline(false);closeDelete(false);current=null;get("sharing-cards").hidden=true;message("当前后台不支持凭据管理，请更新软件。",true);return;}
    current=value;if(editing||deleting||dragging||weeklyPickerOpen())return;get("sharing-cards").hidden=false;
    const hosting=value.host?.running===true,borrowing=value.client?.active===true;
    for(const kind of ["quota","exit"]){const out=views[kind]==="out";get(kind+"-issue").hidden=!out;get(kind+"-borrow").hidden=out;
      get(kind+"-generate").hidden=!out;get(kind+"-activate").hidden=out;
      for(const button of get("sharing-cards").querySelectorAll(`[data-view][data-kind="${kind}"]`))button.setAttribute("aria-pressed",String(button.dataset.view===views[kind]));
      get(kind+"-generate").disabled=busy||(kind==="quota"&&borrowing);get(kind+"-activate").disabled=busy||(kind==="quota"&&hosting);
    }
    get("quota-local").hidden=!borrowing;get("quota-local").disabled=busy;
    for(const kind of ["quota","exit"])renderList(kind);
    if(recentScroll){const row=get("sharing-cards").querySelector(`[data-credential-id="${recentId}"]`);if(row){row.scrollIntoView({block:"nearest",inline:"nearest"});recentScroll=false;}}
    if(busy)for(const kind of ["quota","exit"])for(const input of get(kind+"-credential-rows").querySelectorAll("button,input,select"))input.disabled=true;
    if(!recoveryShown&&(value.recovery_error||value.recovered)){recoveryShown=true;message(value.recovery_error||"上次共享已结束，已恢复原连接。请重新加载 Codex 配置。",!!value.recovery_error);}
  }
  async function action(button,dataValue,success=""){if(busy)return;controller?.abort();busy=true;button?.setAttribute("aria-busy","true");if(button)button.disabled=true;message("");
    try{const result=await request(dataValue),{link,...state}=result;
      const identifier=dataValue.action==="credential_issue"?result.issued_id:dataValue.action==="credential_use"&&dataValue.link?(dataValue.kind==="quota"?result.client?.active_id:result.egress?.selection?.active_id):null;
      highlightCredential(identifier,state);
      render(state);if(link)await copy(link,dataValue.action==="credential_issue"?dataValue.kind+"-generate":"sharing-copy-"+dataValue.id);else if(success!==null&&result.reload_required===true)message(dataValue.action==="credential_delete"?"已删除凭据。请重新加载 Codex 配置或重启 ChatGPT 应用。":"已切换额度来源。请重新加载 Codex 配置或重启 ChatGPT 应用。");else message(success===null?"":success||"已更新，新请求使用当前设置。");return result;
    }catch(error){message(error.message,true);}finally{busy=false;button?.setAttribute("aria-busy","false");if(button)button.disabled=false;if(current)render(current);}}
  async function refresh(){if(!active||document.hidden||busy||editing||deleting||calendar||dragging||weeklyPickerOpen())return;controller?.abort();const task=new AbortController();controller=task;try{const value=await request(null,task.signal);if(active&&!task.signal.aborted)render(value);}catch(error){if(error.name!=="AbortError")message(error.message,true);}finally{if(controller===task)controller=null;}}
  function finishDrag(restore=true){
    if(!dragging)return;
    const draft=dragging;dragging=null;clearTimeout(draft.hideTimer);
    if(draft.frame!==null)cancelAnimationFrame(draft.frame);
    draft.source.classList.remove("sharing-dragging","sharing-drag-preview");get("sharing-cards").classList.remove("sharing-reordering");
    if(restore&&current)render(current);
  }
  function pauseDragScroll(){if(dragging&&dragging.frame!==null){cancelAnimationFrame(dragging.frame);dragging.frame=null;}}
  function placeDrag(y){
    const draft=dragging;if(!draft)return;
    const rows=Array.from(draft.body.rows).filter(row=>row!==draft.source);
    const before=rows.find(row=>{const box=row.getBoundingClientRect();return y<box.top+box.height/2;})||null;
    draft.target=(before||rows.at(-1))?.querySelector("[data-drag-id]")?.dataset.dragId;draft.after=!before;
    if(draft.source.nextElementSibling===before)return;
    const positions=new Map(rows.map(row=>[row,row.getBoundingClientRect().top]));
    for(const row of rows)row.getAnimations?.().forEach(animation=>animation.cancel());
    draft.body.insertBefore(draft.source,before);
    const reduce=typeof matchMedia==="function"&&matchMedia("(prefers-reduced-motion: reduce)").matches;
    if(!reduce)for(const row of rows){const delta=positions.get(row)-row.getBoundingClientRect().top;if(delta)row.animate?.([{transform:`translateY(${delta}px)`},{transform:"translateY(0)"}],{duration:150,easing:"cubic-bezier(.2,.8,.2,1)"});}
  }
  function scrollDrag(){
    const draft=dragging;if(!draft)return;draft.frame=null;
    const box=draft.scroll.getBoundingClientRect(),y=draft.y;
    if(y<box.top||y>box.bottom)return;
    const speed=y<box.top+28?-Math.ceil((box.top+28-y)/3):y>box.bottom-28?Math.ceil((y-box.bottom+28)/3):0;
    if(!speed)return;
    const previous=draft.scroll.scrollTop;draft.scroll.scrollBy(0,speed);
    if(draft.scroll.scrollTop!==previous){placeDrag(y);draft.frame=requestAnimationFrame(scrollDrag);}
  }
  async function moveCredential(identifier,target,after){
    if(busy)return;
    const source=(current?.credentials||[]).find(row=>row.id===identifier),destination=(current?.credentials||[]).find(row=>row.id===target);
    if(!source||!destination||source.internal||destination.internal||source.kind!==destination.kind||source.direction!==destination.direction||source.direction!==views[source.kind]||identifier===target)return;
    const previous=current,rows=shownRows(source.kind),order=rows.filter(row=>row.id!==identifier);
    order.splice(order.findIndex(row=>row.id===target)+(after?1:0),0,source);
    if(rows.every((row,index)=>row.id===order[index].id))return;
    let index=0;closeInline(false);render({...previous,credentials:previous.credentials.map(row=>row.kind===source.kind&&row.direction===source.direction&&!row.internal?order[index++]:row)});
    const body=get(source.kind+"-credential-rows");body.setAttribute("aria-busy","true");
    try{const result=await action(null,{action:"credential_reorder",id:identifier,target_id:target,after},null);if(!result)render(previous);return result;}
    finally{body.setAttribute("aria-busy","false");body.querySelector(`[data-drag-id="${identifier}"]`)?.focus();}
  }
  get("sharing-cards").addEventListener("dragstart",event=>{
    const handle=event.target.closest("[data-drag-id]"),row=(current?.credentials||[]).find(row=>row.id===handle?.dataset.dragId);
    if(busy||deleting||!row||row.internal||shownRows(row.kind).length<2){event.preventDefault();return;}
    controller?.abort();closeInline(false);finishDrag();
    const source=handle.closest("tr"),body=source.parentElement,box=source.getBoundingClientRect();
    const draft={id:row.id,kind:row.kind,direction:row.direction,source,body,scroll:source.closest(".sharing-table-scroll"),target:null,after:false,y:event.clientY,frame:null};dragging=draft;
    event.dataTransfer.effectAllowed="move";event.dataTransfer.setData("application/x-inspector-credential",row.id);
    source.classList.add("sharing-drag-preview");
    event.dataTransfer.setDragImage(source,Math.max(0,Math.min(box.width,event.clientX-box.left)),Math.max(0,Math.min(box.height,event.clientY-box.top)));
    get("sharing-cards").classList.add("sharing-reordering");
    // Capture the complete row before leaving its live placeholder invisible.
    draft.hideTimer=setTimeout(()=>{if(dragging===draft){source.classList.remove("sharing-drag-preview");source.classList.add("sharing-dragging");}},0);
  });
  get("sharing-cards").addEventListener("dragover",event=>{
    if(!dragging)return;
    if(!dragging.scroll.contains(event.target)){pauseDragScroll();return;}
    event.preventDefault();event.dataTransfer.dropEffect="move";dragging.y=event.clientY;placeDrag(event.clientY);
    if(dragging.frame===null)scrollDrag();
  });
  get("sharing-cards").addEventListener("drop",async event=>{
    const draft=dragging;if(!draft)return;
    if(!draft.scroll.contains(event.target)){finishDrag();return;}
    event.preventDefault();placeDrag(event.clientY);const {id,target,after}=draft;finishDrag(false);
    if(target)await moveCredential(id,target,after);
  });
  get("sharing-cards").addEventListener("dragend",()=>finishDrag());
  get("sharing-cards").addEventListener("dragleave",event=>{if(dragging&&!dragging.scroll.contains(event.relatedTarget))pauseDragScroll();});
  get("sharing-cards").addEventListener("keydown",async event=>{
    if(dragging)return;
    const handle=event.target.closest("[data-drag-id]");if(!handle||!event.altKey||!["ArrowUp","ArrowDown"].includes(event.key))return;
    event.preventDefault();const row=(current?.credentials||[]).find(row=>row.id===handle.dataset.dragId);if(!row)return;const rows=shownRows(row.kind),index=rows.findIndex(item=>item.id===row.id),down=event.key==="ArrowDown",target=rows[index+(down?1:-1)];if(target)await moveCredential(row.id,target.id,down);
  });
  function positionInline(){
    if(!editing)return;
    if(editing.field==="expires_at"){positionCalendar();return;}
    const box=get("sharing-inline-editor"),anchor=editing.anchor.getBoundingClientRect(),rect=box.getBoundingClientRect(),margin=12;
    const left=Math.max(margin,Math.min(anchor.left,innerWidth-rect.width-margin));
    const top=anchor.bottom+6+rect.height<=innerHeight-margin?anchor.bottom+6:Math.max(margin,anchor.top-rect.height-6);
    box.style.left=left+"px";box.style.top=top+"px";
  }
  function closeInline(renderNow=true){
    closeCalendar(false);
    if(!editing)return;
    const draft=editing;draft.anchor.setAttribute("aria-expanded","false");editing=null;get("sharing-inline-editor").hidden=true;
    get("sharing-inline-message").hidden=true;get("sharing-calendar-message").hidden=true;if(renderNow&&current){render(current);get("sharing-cards").querySelector(`[data-action="field"][data-id="${draft.id}"][data-field="${draft.field}"]`)?.focus();}
  }
  function openInline(row,field,anchor){
    if(busy||!["label","budget","used","expires_at"].includes(field)||(row.direction!=="out"&&field!=="label")||(["budget","used"].includes(field)&&row.kind!=="quota"))return;
    if(editing?.id===row.id&&editing.field===field){closeInline();return;}
    if(field==="used"&&!current?.used_quota_edit_supported){message("当前后台不支持修改已用额度，请更新软件。",true);return;}
    controller?.abort();closeInline(false);message("");
    const original=field==="label"?row.label:field==="budget"?row.budget_nanos/1e9:field==="used"?row.period_cost_nanos/1e9:row.expires_at;
    editing={id:row.id,kind:row.kind,field,original,anchor};anchor.setAttribute("aria-expanded","true");
    const input=get("sharing-inline-value"),box=get("sharing-inline-editor"),isDate=field==="expires_at",isAmount=field==="budget"||field==="used";
    input.type=isAmount?"number":"text";input.maxLength=80;input.step="any";
    input.min=field==="budget"?String(Math.min(original,1)):field==="used"?"0":"";
    input.max=isAmount?"1000000":"";input.autocomplete="off";input.setCustomValidity("");input.disabled=isDate;input.hidden=isDate;input.required=!isDate;
    input.value=isDate?"":original??"";
    get("sharing-inline-label").textContent=tr(field==="label"?"名称":field==="budget"?"额度":field==="used"?"已用额度":"期限")+(isAmount?" · "+tr("美元"):"");
    get("sharing-inline-label").setAttribute("for",isDate?"sharing-inline-expiry-date":"sharing-inline-value");
    get("sharing-inline-expiry-container").hidden=!isDate;
    if(isDate){
      setExpiry("sharing-inline-expiry",Number.isFinite(original)?expiryDay(original):defaultExpiry(),false);
      if(original===null)setExpiry("sharing-inline-expiry",null);
    }
    disableExpiry("sharing-inline-expiry",!isDate);
    get("sharing-inline-save").textContent=tr(isDate?"确定":"保存");
    box.dataset.field=field;box.hidden=false;positionInline();
    if(isDate)openCalendar("sharing-inline-expiry");
    else{input.focus();if(field==="label")input.select();}
  }
  get("sharing-inline-value").addEventListener("input",()=>{get("sharing-inline-value").setCustomValidity("");get("sharing-inline-message").hidden=true;});
  get("sharing-inline-cancel").addEventListener("click",()=>{if(!busy)closeInline();});
  async function saveInline(){
    if(!editing||busy)return;
    const draft=editing,input=get("sharing-inline-value");
    try{
      let value;
      if(draft.field==="label")value=credentialName(draft.kind,input.value,draft.id);
      else if(draft.field==="budget"||draft.field==="used")value=String(input.value).trim()&&Number(input.value)===draft.original?draft.original:budget("sharing-inline-value",draft.field==="used"?0:1);
      else value=expiry("sharing-inline-expiry",draft.original);
      if(value===draft.original){closeInline();return;}
      input.disabled=true;disableExpiry("sharing-inline-expiry",true);get("sharing-inline-cancel").disabled=true;
      try{
        const result=await action(get(draft.field==="expires_at"?"sharing-inline-expiry-apply":"sharing-inline-save"),{action:"credential_edit",id:draft.id,[draft.field]:value},null);
        if(result&&editing===draft)closeInline();else positionInline();
      }finally{input.disabled=draft.field==="expires_at";disableExpiry("sharing-inline-expiry",draft.field!=="expires_at");get("sharing-inline-cancel").disabled=false;}
    }catch(error){message(error.message,true);positionInline();}
  }
  get("sharing-inline-editor").addEventListener("submit",async event=>{event.preventDefault();if(editing?.field!=="expires_at")await saveInline();});
  document.addEventListener("pointerdown",event=>{if(calendar&&!busy&&!get(calendar.id+"-calendar").contains(event.target)&&!get(calendar.id+"-date").contains(event.target))closeCalendar(false);if(editing&&!busy&&!get("sharing-inline-editor").contains(event.target)&&!event.target.closest("[data-action=field]"))closeInline(false);});
  document.addEventListener("keydown",event=>{if(!busy&&event.key==="Escape"){if(calendar){event.preventDefault();closeCalendar();}else if(editing){event.preventDefault();closeInline();}}});
  const positionEditors=()=>{positionInline();positionCalendar();};
  globalThis.addEventListener("resize",positionEditors);
  document.addEventListener("scroll",positionEditors,true);
  function openDelete(row){
    controller?.abort();closeInline(false);message("");deleting={id:row.id,kind:row.kind};
    get("sharing-delete-name").textContent=[tr(row.kind==="quota"?"额度":"出口"),tr(row.direction==="out"?"借出":"借入"),row.label].join(" · ");
    get("sharing-delete-description").textContent=tr(row.direction==="out"?"删除后该凭据将失效，其用量记录也会移除，无法撤销。":"仅移除本机保存的凭据和用量记录，不影响对方的凭据。");
    const hint=get("sharing-delete-hint");hint.hidden=!isActive(row);hint.textContent=isActive(row)?tr(row.kind==="quota"?"此凭据正在使用，删除后将停止借用并恢复原连接，需要重新加载 Codex 配置或重启 ChatGPT 应用。":"此凭据正在使用，删除后将使用本机出口。"):"";
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
  async function copy(value,anchor){try{await navigator.clipboard.writeText(value);message("已复制，仅供一台机器使用",false,anchor);}catch(_){message("复制失败，请点击右侧“凭据”重试。",true);}}
  for(const kind of ["quota","exit"]){
    get(kind+"-issue").addEventListener("submit",async event=>{event.preventDefault();try{const result=await action(get(kind+"-generate"),{action:"credential_issue",kind,label:credentialName(kind,get(kind+"-label").value),expires_at:expiry(kind+"-expiry"),...(kind==="quota"?{budget:budget("quota-budget"),...weeklySettings(get("quota-weekly").value)}:{})},null);if(result)resetIssue(kind);}catch(error){message(error.message,true);}});
    get(kind+"-borrow").addEventListener("submit",async event=>{event.preventDefault();const result=await action(get(kind+"-activate"),{action:"credential_use",kind,label:get(kind+"-borrow-label").value.trim(),link:get(kind+"-borrow-link").value.trim()});if(result)get(kind+"-borrow-link").value="";});
  }
  get("sharing-cards").addEventListener("click",async event=>{const button=event.target.closest("[data-view],[data-action]");if(!button||busy)return;const {kind,view,action:command}=button.dataset;
    if(view){closeInline(false);views[kind]=view;if(current)render(current);return;}
    if(command==="local"&&kind==="quota"){await action(button,{action:"client_disconnect"});return;}
    const row=(current?.credentials||[]).find(row=>row.id===button.dataset.id);if(!row)return;
    if(command==="weekly")return;
    if(command==="delete"){openDelete(row);return;}
    if(command==="field"){openInline(row,button.dataset.field,button);return;}if(editing)closeInline(false);if(command==="copy"){await action(button,{action:"credential_copy",id:row.id});return;}
    if(command==="toggle")await action(button,{action:"credential_edit",id:row.id,enabled:!lending(row)},null);
    if(command==="use")await action(button,row.kind==="exit"&&isActive(row)?{action:"egress_select",mode:"local"}:{action:"credential_use",kind:row.kind,id:row.id});
  });
  get("sharing-cards").addEventListener("change",async event=>{
    const input=event.target;if(input.dataset.action!=="weekly")return;
    const row=(current?.credentials||[]).find(row=>row.id===input.dataset.id);
    if(busy||!row||row.direction!=="out"||row.kind!=="quota"){if(row)input.value=String(weeklyDay(row)??"");return;}
    try{const changes=weeklySettings(input.value);syncWeeklyLabel(input);if(editing)closeInline(false);await action(input,{action:"credential_edit",id:row.id,...changes},null);}
    catch(error){input.value=String(weeklyDay(row)??"");syncWeeklyLabel(input);message(error.message,true);}
  });
  document.addEventListener("visibilitychange",()=>{if(document.hidden)controller?.abort();else if(active)refresh();});
  function setActive(value){active=value;clearInterval(timer);timer=null;if(!active){controller?.abort();finishDrag();clearRecent();closeInline(false);closeDelete(false);return;}message("");refresh();timer=setInterval(refresh,3000);}
  return {setActive};
})();
if(typeof workspaceView!=="undefined"&&workspaceView==="sharing")globalThis.InspectorSharing.setActive(true);
