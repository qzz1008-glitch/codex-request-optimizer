"use strict";
const $ = (id) => document.getElementById(id);
history.replaceState(null, "", "/");
const state = { status: null, packets: [], sessions: [], selected: null, detail: null, tab: "overview", path: "", text: null, older: false, polling: false, detailVersion: "", detailRequest: 0, listVersion: "", sessionVersion: "", sort: "created", direction: "desc", filters: {kind:"",status:"",thread:""}, canLoadMore: false, listRequest: 0, listController: null, listRevision: null, filterRevision: null };
const nf = new Intl.NumberFormat("zh-CN");
let contextSettings = null;
let tokenWindow = null, tokenRequest = 0, tokenLoading = false, tokenTimer = null, tokenController = null, tokenPendingRetries = 0;
const tokenCache = new Map();
let sessionSummaries = [], sessionMetadataAt = 0, sessionMetadataProbeAt = 0, sessionMetadataLoading = false, sessionMetadataSupported = null;
let sessionLegacyCursor = 0;
const dateFormat = new Intl.DateTimeFormat("zh-CN",{month:"2-digit",day:"2-digit"});
const clockFormat = new Intl.DateTimeFormat("zh-CN",{hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false});
const timestampFormat = new Intl.DateTimeFormat("zh-CN",{year:"numeric",month:"numeric",day:"numeric",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false});
const names = uiI18n.labels({"message:user":"用户消息","message:assistant":"助手消息","message:developer":"开发者指令","message:system":"系统指令",reasoning:"推理（含密文）",function_call_output:"函数工具返回",custom_tool_call_output:"自定义工具返回",function_call:"函数工具调用",custom_tool_call:"自定义工具调用",additional_tools:"增量工具目录",tool_definition:"工具定义",compaction_trigger:"压缩触发项",compaction:"压缩状态",context_compaction:"压缩内容"});
const states = uiI18n.labels({captured:"待响应",streaming:"响应中",complete:"完成",client_disconnected:"客户端断开",relay_error:"转发失败",interrupted:"进程中断"});
function el(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; }
function bytes(n) { return n === undefined || n === null ? "—" : nf.format(n) + " B"; }
function uploadBytes(p) { return Number.isFinite(p.outbound_wire_bytes) && p.outbound_wire_bytes >= 0 ? p.outbound_wire_bytes : null; }
function compact(n) { if (n === undefined || n === null) return "—"; return n >= 1048576 ? (n/1048576).toFixed(2)+" MiB" : n >= 1024 ? (n/1024).toFixed(1)+" KiB" : n+" B"; }
function renderStorageInfo() {
  const storage=state.status?.storage;if(!storage)return;
  const sessions=state.filterRevision!==null?nf.format(state.sessions.length):"—";
  $("storage-info").replaceChildren(
    el("span","",compact(storage.stored)+" / "+compact(storage.max_bytes)),
    el("span","",t("{count} 会话").replace("{count}",sessions)),
    el("span","",t("{count} 请求").replace("{count}",nf.format(storage.count)))
  );
}
function signed(n) { return (n > 0 ? "+" : "") + bytes(n); }
function kind(n) { return names[n] || n; }
function requestKind(p) { return p.is_compaction ? t("上下文压缩") : p.method === "POST" ? t("对话生成") : p.method || t("未知"); }
const requestKinds = uiI18n.labels({generation:"对话生成",compaction:"上下文压缩",other:"其他请求"});
const filterLabels = uiI18n.labels({kind:"类型",status:"状态"});
function statusLabel(value) { return value.startsWith("http:") ? "HTTP "+value.slice(5) : states[value.slice(6)] || t("未知状态"); }
function setFilterOptions(key, options) {
  const select=$("filter-"+key), current=state.filters[key];
  if(current && !options.some(o=>o.value===current)) options.push({value:current,label:select.selectedOptions[0]?.textContent||current});
  const signature=JSON.stringify([current,options]);
  if(select.dataset.optionsVersion===signature || document.activeElement===select)return;
  select.dataset.optionsVersion=signature;
  const entries=[{value:"",label:filterLabels[key]},...options];
  select.replaceChildren(...entries.map(entry=>{const option=el("option","",entry.label);option.value=entry.value;option.title=entry.title||entry.label;return option;}));
  select.value=current;
  select.classList.toggle("filtered",!!current);
  const chosen=options.find(o=>o.value===current);
  select.title=t("筛选")+filterLabels[key]+"："+(chosen?(chosen.title||chosen.label):t("全部"));
}
async function refreshFilters() {
  const revision=state.status?.storage.revision;
  const options=await api("/api/packet-filters");
  setFilterOptions("kind",["generation","compaction",...(options.kinds.includes("other")?["other"]:[])].map(value=>({value,label:requestKinds[value]})));
  setFilterOptions("status",options.statuses.map(value=>({value,label:statusLabel(value)})));
  state.sessions=options.threads;
  state.filterRevision=revision;
  renderStorageInfo();renderSessions();
}
async function refreshSessionSummaries() {
  if(!state.status || sessionMetadataLoading || Date.now()-sessionMetadataAt<5000)return;
  sessionMetadataLoading=true;
  try {
    if(sessionMetadataSupported!==false || Date.now()-sessionMetadataProbeAt>=30000) {
      sessionMetadataProbeAt=Date.now();
      try {
        const result=await api("/api/session-summaries",undefined,AbortSignal.timeout(5000));
        if(result.state==="available" && Array.isArray(result.sessions)) {
          sessionSummaries=result.sessions;sessionMetadataSupported=true;return;
        }
      } catch(_) {}
      sessionMetadataSupported=false;
    }
    // Older running previews can still show recent upload size and request time.
    const previous=new Map(sessionSummaries.map(row=>[row.thread_id,row]));
    const ids=state.sessions.slice(0,300), results=ids.map(id=>({...previous.get(id),thread_id:id}));
    const jobs=Array.from({length:Math.min(8,ids.length)},(_,i)=>(sessionLegacyCursor+i)%ids.length);let index=0;
    sessionLegacyCursor=ids.length?(sessionLegacyCursor+jobs.length)%ids.length:0;
    async function worker() {
      while(index<jobs.length) {
        const at=jobs[index++], id=ids[at], existing=previous.get(id)||{};
        try {
          const query=new URLSearchParams({limit:"1",sort:"created",direction:"desc"});
          query.set(id?"thread":"thread_unknown",id||"1");
          const packets=await api("/api/packets?"+query,undefined,AbortSignal.timeout(5000)), packet=packets[0];
          if(packet)results[at]={...existing,thread_id:id,title:existing.title??null,updated_at:Math.max(existing.updated_at||0,packet.created),latest_packet:{id:packet.id,created:packet.created,wire_bytes:uploadBytes(packet)}};
          else results[at]={...existing,thread_id:id};
        } catch(_) {results[at]={...existing,thread_id:id};}
      }
    }
    await Promise.all(Array.from({length:Math.min(3,ids.length)},()=>worker()));
    sessionSummaries=results;
  } finally {sessionMetadataAt=Date.now();sessionMetadataLoading=false;renderSessions();}
}
function hasTextSelectionWithin(node) {
  const selection=window.getSelection();
  return !!selection&&!selection.isCollapsed&&selection.containsNode(node,true);
}
function renderSessions() {
  if(!state.status||hasTextSelectionWithin($("session-list")))return;
  const summaries=new Map(sessionSummaries.map(row=>[row.thread_id,row]));
  const ids=[...new Set([...sessionSummaries.map(row=>row.thread_id),...state.sessions])], selected=state.filters.thread;
  ids.sort((a,b)=>(summaries.get(b)?.updated_at||0)-(summaries.get(a)?.updated_at||0));
  const selectedId=selected==="unknown"?"":selected.slice(7);
  if(selected && !ids.includes(selectedId))ids.push(selectedId);
  const query=$("session-search").value.trim().toLowerCase();
  const signature=JSON.stringify([ids,selected,sessionSummaries,sessionMetadataSupported,query]);
  if(signature===state.sessionVersion)return;
  state.sessionVersion=signature;
  const fragment=document.createDocumentFragment();
  for(const id of ids) {
    const value=id?"thread:"+id:"unknown", data=summaries.get(id)||{};
    const title=!id?t("未识别会话"):data.title_source==="index_task"&&data.source_title?t("会话索引 · ")+data.source_title:data.title===null||data.title===undefined?t("会话名称暂不可用"):data.title||t("未命名会话");
    if(query && !(title+"\n"+id).toLowerCase().includes(query))continue;
    const item=el("button","session-item"+(value===selected?" selected":""));
    item.type="button";item.dataset.session=value;
    item.title=title+"\n"+(id||t("未识别会话"));
    if(value===selected)item.setAttribute("aria-current","page");
    const heading=el("div","session-heading");heading.append(el("span","session-title",title));
    item.append(heading);
    if(id)item.append(el("code","session-id",id));
    const updated=Number.isFinite(data.updated_at)?new Date(data.updated_at*1000):null;
    const meta=el("div","session-meta"), when=el("span","session-updated",t("更新 ")+(updated?dateFormat.format(updated)+" "+clockFormat.format(updated):"—"));
    if(updated)when.title=timestampFormat.format(updated);
    const size=data.latest_packet?.wire_bytes, packetSize=el("span","session-size",t("包 ")+compact(size));
    packetSize.title=size===undefined||size===null?t("尚未采集最终上传大小"):t("最终上传正文：")+bytes(size);
    const tokens=data.token_window, estimated=tokens?.estimated, used=estimated?tokens?.used_tokens:tokens?.input_tokens;
    const available=tokens?.state==="available"&&Number.isFinite(used)&&used>=0, capacity=tokens?.window_tokens;
    const tokenText=available?(estimated?"≈ ":"")+nf.format(used):"—";
    const usage=el("span","session-token","Token "+tokenText);
    usage.title=available?(estimated?t("压缩后上下文总量估算"):t("最近请求输入 Token"))+(capacity?t(" · 窗口 ")+nf.format(used)+" / "+nf.format(capacity)+(estimated?"":" · "+(used/capacity*100).toFixed(1)+"%"):"")+(tokens.recorded_at?" · "+timestampFormat.format(new Date(tokens.recorded_at*1000)):""):t("尚无可用的输入 Token 记录");
    meta.append(usage,packetSize,when);item.append(meta);
    let pointerStart=null;
    item.addEventListener("pointerdown",event=>{pointerStart={x:event.clientX,y:event.clientY};});
    item.addEventListener("click",event=>{
      const dragged=pointerStart&&Math.hypot(event.clientX-pointerStart.x,event.clientY-pointerStart.y)>3;
      pointerStart=null;
      if(event.detail>0&&(dragged||event.detail>1)&&hasTextSelectionWithin(item))return;
      if(hasTextSelectionWithin($("session-list")))window.getSelection().removeAllRanges();
      selectSession(value).catch(fail);
    });fragment.append(item);
  }
  if(!fragment.childElementCount) {
    const empty=el("p","session-empty",query?t("未找到匹配的会话"):t("暂无会话"));
    empty.setAttribute("role","status");fragment.append(empty);
  }
  $("session-list").replaceChildren(fragment);
  $("session-metadata-note").hidden=sessionMetadataSupported!==false;
}
async function selectSession(value) {
  if(value===state.filters.thread)return;
  cancelTokenRead();tokenWindow=null;
  state.filters.thread=value;
  state.detailRequest++;
  state.selected=null;state.detail=null;state.detailVersion="";state.text=null;
  state.packets=[];state.older=false;state.canLoadMore=false;state.listRevision=null;
  $("packet-detail").hidden=true;$("empty-state").hidden=false;
  $("packet-list").scrollTop=0;
  renderSessions();renderList();
  await loadPackets();
}
function message(text, error=false) { $("notice").textContent = text; $("notice").className = "notice"+(error?" error":""); $("notice").hidden = false; }
function fail(err) { message(err.message || String(err), true); }
async function api(path, data, signal) { const r = await fetch(path, {method:data === undefined ? "GET" : "POST",headers:data === undefined ? {} : {"Content-Type":"application/json"},body:data === undefined ? undefined : JSON.stringify(data),signal}); const value = await r.json(); if (!r.ok) throw Error(value.error || ("HTTP "+r.status)); return value; }
function action(id, fn) { $(id).addEventListener("click", () => Promise.resolve().then(fn).catch(fail)); }
function metric(label, value, caption, cls="") { const n = el("div","metric"); n.append(el("small","",label),el("strong",cls,value),el("span","caption",caption)); return n; }
function tokenMetric() {
  const data=tokenWindow, available=data?.state==="available"&&!data.estimated&&Number.isFinite(data.input_tokens)&&data.input_tokens>=0;
  const used=available?data.input_tokens:null, window=available?data.window_tokens:null;
  const ratio=window?used/window:null;
  const value=available?nf.format(used)+(window?" / "+nf.format(window):" tokens"):"—";
  const caption=available?(ratio===null?t("窗口上限未记录"):(ratio*100).toFixed(1)+"%")+t(" · 输出 ")+nf.format(data.output_tokens):data?.state==="pending"?t("等待请求完成"):data?.state==="unsupported"?t("暂未读取到用量记录"):data?.state==="loading"||!data?t("正在读取 token 记录…"):t("暂无对应的输入 Token 记录");
  const card=metric(t("输入上下文占用"),value,caption,ratio!==null&&ratio>=1?"danger":ratio!==null&&ratio>=.9?"warning":"");
  card.id="token-window";
  card.title=available?t("本次请求输入 Token / 窗口上限；输出单独展示，缓存 Token 已包含在输入中。"):t("请求完成并写入用量记录后显示，仅匹配本次请求。");
  return card;
}
function tokenVersion(packet) { return [packet.state,packet.status,packet.duration_ms].join(":"); }
function cancelTokenRead() {
  clearTimeout(tokenTimer);tokenTimer=null;
  tokenController?.abort();tokenController=null;
  tokenRequest++;tokenLoading=false;tokenPendingRetries=0;
}
function cachedTokenWindow(packet) {
  const cached=tokenCache.get(packet.id);
  if(!cached)return null;
  if(cached.version!==tokenVersion(packet) || cached.expires<Date.now()) {tokenCache.delete(packet.id);return null;}
  tokenCache.delete(packet.id);tokenCache.set(packet.id,cached);
  return cached.data;
}
function cacheTokenWindow(packet,data) {
  if(packet?.state!=="complete" || data.state!=="available")return;
  tokenCache.delete(packet.id);
  tokenCache.set(packet.id,{version:tokenVersion(packet),expires:Date.now()+300000,data});
  while(tokenCache.size>256)tokenCache.delete(tokenCache.keys().next().value);
}
async function loadTokenWindow() {
  const id=state.selected;
  if(!id || document.hidden || tokenLoading || (tokenWindow && !["loading","pending"].includes(tokenWindow.state)))return;
  clearTimeout(tokenTimer);tokenTimer=null;
  const request=++tokenRequest;tokenLoading=true;
  const controller=new AbortController();tokenController=controller;
  const timeout=setTimeout(()=>controller.abort(),6000);
  try {
    const data=await api("/api/packets/"+id+"/token-window",undefined,controller.signal);
    if(request!==tokenRequest || state.selected!==id)return;
    tokenWindow=data;cacheTokenWindow(state.detail,data);
  } catch(error) {
    if(request!==tokenRequest || state.selected!==id)return;
    tokenWindow={state:error.message==="unknown operation"?"unsupported":"unavailable"};
  } finally {
    clearTimeout(timeout);
    if(request===tokenRequest) {
      tokenLoading=false;tokenController=null;
      if(state.selected===id) {
        $("token-window")?.replaceWith(tokenMetric());
        const delay=tokenWindow?.state==="loading"?50:tokenWindow?.state==="pending"?(++tokenPendingRetries<=30?300:1000):null;
        if(delay!==null)tokenTimer=setTimeout(loadTokenWindow,delay);
      }
    }
  }
}
function panel(title) { const n = el("section","panel"); n.append(el("h3","",title)); return n; }
function table(headings, rows) { const wrap = el("div","table-scroll"), t = el("table"), head=el("thead"), tr=el("tr"); for (const h of headings) tr.append(el("th","",h)); head.append(tr); t.append(head); const body=el("tbody"); for (const cells of rows) { const row=el("tr"); for (const cell of cells) {const td=el("td"); if (cell instanceof Node) td.append(cell); else td.textContent = cell; row.append(td);} body.append(row); } t.append(body); wrap.append(t); return wrap; }
function link(text, fn) { const n=el("button","link-button",text); n.addEventListener("click",()=>Promise.resolve().then(fn).catch(fail)); return n; }
function fact(n,label,value) { const r=el("div","fact-row"); r.append(el("span","",label),el("span","",value)); n.append(r); }
function showTab(tab) { state.tab=tab; for (const b of document.querySelectorAll("[data-tab]")) b.classList.toggle("active",b.dataset.tab===tab); for (const t of ["overview","items","tree","diff","headers"]) $("tab-"+t).hidden=t!==tab; }
function badge(p) { return el("span","badge "+(p.status>=400?"bad":p.status>=200?"good":"warn"),p.status?String(p.status):states[p.state]||p.state); }
function renderSort() {
  for (const b of document.querySelectorAll("[data-sort]")) {
    const active=b.dataset.sort===state.sort;
    b.closest("th").setAttribute("aria-sort",active?(state.direction==="asc"?"ascending":"descending"):"none");
    b.querySelector(".sort-arrow").textContent=active?(state.direction==="asc"?" ↑":" ↓"):"";
  }
}
function renderList() {
  if(!state.status)return;
  const filtered=Object.values(state.filters).some(Boolean);
  const sessionCount=state.status.storage.threads?.find(row=>(row.thread||"")===(state.filters.thread==="unknown"?"":state.filters.thread.slice(7)))?.count;
  const total=!filtered?state.status.storage.count:!state.filters.kind && !state.filters.status && state.filters.thread && Number.isFinite(sessionCount)?sessionCount:null;
  $("record-count").textContent=total===null?nf.format(state.packets.length)+(state.canLoadMore?"+":"")+t(" 包"):nf.format(total)+t(" 包");
  const signature=JSON.stringify([state.selected,state.status.budget_bytes,state.older,state.canLoadMore,state.sort,state.direction,state.filters,state.packets.map(p=>[p.id,p.created,p.method,p.is_compaction,p.state,p.status,p.outbound_wire_bytes,p.outbound_content_encoding,p.thread_id,p.overflow_compaction_calls,p.overflow_cache_hit,p.overflow_original_wire_bytes])]);
  if(signature===state.listVersion)return;
  state.listVersion=signature;
  const root=$("packet-rows"), fragment=document.createDocumentFragment(); renderSort();
  $("list-empty").hidden=!!state.packets.length;
  $("list-empty").textContent=Object.values(state.filters).some(Boolean)?t("没有符合筛选条件的请求。"):t("暂无请求记录。");
  for (const p of state.packets) {
    const protection=[];
    if(p.overflow_compaction_calls>0)protection.push(t("压缩 ")+nf.format(p.overflow_compaction_calls)+t(" 次"));
    if(p.overflow_cache_hit===true)protection.push(t("复用摘要"));
    const row=el("tr",[state.selected===p.id?"selected":"",protection.length?"overflow-protected":""].filter(Boolean).join(" ")), date=new Date(p.created*1000);
    const time=el("button","packet-open");
    time.append(el("span","packet-date",dateFormat.format(date)),el("span","packet-clock",clockFormat.format(date)));
    time.title=timestampFormat.format(date);
    time.setAttribute("aria-label",t("查看 ")+time.title+t(" 的请求"));
    const type=requestKind(p);
    const upload=uploadBytes(p);
    const size=el("span","packet-size"+(upload!==null && (upload>1_000_000 || upload>=state.status.budget_bytes)?" danger":""),compact(upload)); size.title=upload===null?t("尚未采集出口大小"):bytes(upload);
    row.dataset.session=p.thread_id||"";row.title=p.thread_id?"session_id: "+p.thread_id:t("会话未识别");
    if(protection.length)row.title+=t("\n已应用超限保护：")+protection.join(" · ")+(Number.isFinite(p.overflow_original_wire_bytes)?t(" · 原始上传 ")+compact(p.overflow_original_wire_bytes):"");
    for (const value of [time,type,size,badge(p)]) {const td=el("td");if(value instanceof Node)td.append(value);else td.textContent=value;row.append(td);}
    row.addEventListener("click",()=>selectPacket(p.id).catch(fail)); fragment.append(row);
  }
  root.replaceChildren(fragment);
}
function loadMorePackets() {
  if(!state.canLoadMore || state.listController || document.hidden)return;
  const list=$("packet-list");
  if(list.scrollHeight-list.scrollTop-list.clientHeight>180)return;
  return loadPackets(true).catch(fail);
}
async function loadPackets(append=false,background=false) {
  if((append || background) && state.listController)return;
  state.listController?.abort();
  const controller=new AbortController();state.listController=controller;
  const requestId=++state.listRequest;
  const list=$("packet-list");
  if(!append && !background)list.scrollTop=0;
  list.setAttribute("aria-busy","true");
  $("list-loading").hidden=!append;
  let loaded=false;
  const revision=state.status?.storage.revision;
  const q=new URLSearchParams({limit:"100",sort:state.sort,direction:state.direction});
  if(state.filters.kind)q.set("kind",state.filters.kind);
  if(state.filters.status)q.set("status",state.filters.status);
  if(state.filters.thread==="unknown")q.set("thread_unknown","1");
  else if(state.filters.thread)q.set("thread",state.filters.thread.slice(7));
  if (append && state.packets.length) q.set("after",state.packets.at(-1).id);
  try {
    const values=await api("/api/packets?"+q,undefined,controller.signal);
    if(requestId!==state.listRequest)return;
    state.listRevision=revision;
    state.canLoadMore=values.length===100;
    if (append) {state.packets.push(...values); state.older=true;} else {state.packets=values;state.older=false;}
    renderList();
    loaded=true;
    if (state.selected) { const p=state.packets.find(p=>p.id===state.selected); if(p) {const ver=[p.state,p.status,p.duration_ms,p.outbound_wire_bytes,p.outbound_content_encoding].join(":");if(ver!==state.detailVersion) await selectPacket(state.selected,true);} }
  } catch(e) {if(e.name!=="AbortError")throw e;}
  finally {
    if(state.listController===controller) {
      state.listController=null;
      list.setAttribute("aria-busy","false");$("list-loading").hidden=true;
      if(loaded)requestAnimationFrame(loadMorePackets);
    }
  }
}
async function refreshStatus(fill=false) {
  state.status=await api("/api/status");renderStatus(fill);
}
function renderStatus(fill=false) {
  const s=state.status;if(!s)return;
  const uiVersion=s.ui_version||s.version||t("未知"), relayVersion=s.version||t("未知");
  $("app-version").textContent=uiVersion===relayVersion?t("版本 ")+uiVersion:t("界面 ")+uiVersion+t(" · 中转 ")+relayVersion;
  $("app-version").title=uiVersion===relayVersion?t("当前界面与中转版本一致。"):t("界面已更新，正在使用原有中转；中转新功能需要单独升级后生效。");
  const preview=!!s.ui_live_reload && !!s.ui_read_only;
  $("ui-refresh").hidden=!s.ui_live_reload;
  $("preview-banner").hidden=!preview;
  document.documentElement.classList.toggle("preview-mode",preview);
  if(preview) {
    const style=getComputedStyle(document.documentElement);
    const width=parseInt(style.getPropertyValue("--desktop-width")), height=parseInt(style.getPropertyValue("--desktop-height"));
    $("preview-banner").textContent=t("桌面窗口预览 · ")+width+" × "+height+t(s.ui_entry_port_editable?" · 保存后自动刷新":" · 保存后自动刷新 · 设置只读");
  }
  $("capture-toggle").disabled=!!s.ui_read_only;
  $("capture-status").textContent=s.capture_enabled?t("正在记录"):t("记录已暂停"); $("capture-status").classList.toggle("paused",!s.capture_enabled);
  $("capture-toggle").textContent=s.capture_enabled?t("暂停记录"):t("继续记录");
  $("empty-connect").hidden=!!s.profile.active;
  renderStorageInfo();
  if(fill) fillSettings();
  renderSessions();renderList();
}
function fillSettings() {
  const s=state.status, p=s.profile;
  const development=s.ui_development;
  $("ui-development-settings").hidden=!development?.available;
  $("ui-development").checked=!!development?.enabled;
  $("ui-development").disabled=!!s.ui_read_only||!development?.available;
  $("ui-development-message").textContent=t(development?.error||"");
  $("ui-development-message").hidden=!development?.error;
  fillEntryPort();
  $("budget").value=s.budget_bytes; $("storage-limit").value=s.max_storage_mb; $("packet-limit").value=s.max_packets;
  $("request-compression").value=s.request_compression; $("request-compression").disabled=!!s.ui_read_only;
  const startupSupported=!!s.start_on_login_supported;
  const retrySupported=!!s.capacity_retry_supported;
  const imageSupported=!!s.image_isolation_supported;
  $("start-on-login").checked=!!s.start_on_login;
  $("start-on-login").disabled=!!s.ui_read_only||!startupSupported;
  $("capacity-retry").checked=!!s.capacity_retry_enabled;
  $("capacity-retry").disabled=!!s.ui_read_only||!retrySupported;
  $("image-isolation").checked=!!s.image_isolation_enabled;
  $("image-isolation").disabled=!!s.ui_read_only||!imageSupported;
  $("image-isolation-hint").textContent=t(imageSupported?"关闭原生 view_image，保留独立解析工具。主会话只接收图片路径和解析文字；保存后需重新加载 Codex。":"当前中转暂不支持图片隔离，请升级中转后启用。");
  $("startup-hint").textContent=t(startupSupported?(s.startup_state_known===false?"无法读取开机启动状态，请重新保存设置。":"登录 Windows 后在后台启动，可从托盘打开窗口。"):"开机启动仅支持已安装的 Windows 版本。");
  $("runtime-settings-save").disabled=!!s.ui_read_only||(!startupSupported&&!retrySupported&&!imageSupported);
  $("runtime-settings-message").hidden=retrySupported;
  $("runtime-settings-message").textContent=retrySupported?"":t("当前中转暂不支持此功能。");
  $("overflow-compaction").checked=!!s.overflow_compaction_enabled;
  $("overflow-compaction").disabled=!!s.ui_read_only || !("overflow_compaction_enabled" in s);
  if(!("overflow_compaction_enabled" in s)) {
    $("overflow-message").textContent=t("当前中转暂不支持此功能。");$("overflow-message").hidden=false;
  }
  $("link-activate").disabled=!!s.ui_read_only || !!p.active || !!p.config_error; $("link-restore").disabled=!!s.ui_read_only || !p.can_restore;
  $("settings-save").disabled=!!s.ui_read_only;
  $("link-status").textContent=providerStatus(s);
}
function fillEntryPort() {
  const s=state.status, port=s.entry_port;
  const supported=!!s.entry_port_supported && !!port;
  const editable=supported && (!s.ui_read_only || s.ui_entry_port_editable);
  $("entry-port").value=port?.configured_port??port?.current_port??s.relay_port??47844;
  $("entry-port").disabled=!editable;$("entry-port-save").disabled=!editable;
  $("entry-port-current").textContent=supported?
    t("当前端口：{port}").replace("{port}",port.current_port)+(port.pending?" · "+t("待生效端口：{port}").replace("{port}",port.configured_port):""):
    t("入口端口暂不可用。");
}
function providerStatus(s) {
  if(s.profile.config_error || s.provider_sync_error)return t("无法读取 Codex 配置。");
  if(s.auto_detect_providers && s.profile.all_providers)return t("已自动接管 {count} 个 provider").replace("{count}",s.provider_count);
  return s.profile.active?t("已配置接入"):t("尚未接入");
}
function contextMessage(text,error=false) { const node=$("context-message");node.textContent=text;node.className="help"+(error?" error":"");node.hidden=!text; }
function syncContextDefaults() {
  for(const key of ["context","compact"]) {
    const disabled=$(key+"-default").checked;
    $(key+"-tokens").disabled=disabled;$(key+"-range").disabled=disabled;
  }
}
function fillContextSettings(data) {
  contextSettings=data;
  const maximum=data.max_context_tokens/1000;
  renderContextModel();
  for(const [key,value,fallback] of [["context",data.context_tokens,data.default_context_tokens||240000],["compact",data.compact_tokens,220000]]) {
    $(key+"-default").checked=value===null;
    $(key+"-tokens").max=String(maximum);$(key+"-range").max=String(Math.max(key==="context"?4:1,Math.floor(maximum)));
    const display=value??(key==="context"?data.default_context_tokens:null);
    $(key+"-tokens").value=display===null?"":String(display/1000);$(key+"-tokens").placeholder=t("模型默认");$(key+"-range").value=String((value??fallback)/1000);
  }
  syncContextDefaults();$("context-save").disabled=!!state.status?.ui_read_only;$("context-reset").disabled=!!state.status?.ui_read_only;
}
function renderContextModel() {
  const data=contextSettings;if(!data)return;
  $("context-model").textContent=(data.model?t("全局模型：")+data.model:t("未指定模型"))+(data.default_context_tokens?t(" · 默认 ")+nf.format(data.default_context_tokens/1000)+"k":"");
  for(const key of ["context","compact"])$(key+"-tokens").placeholder=t("模型默认");
}
async function loadContextSettings() {
  contextMessage("");
  try {fillContextSettings(await api("/api/context-settings"));}
  catch(error) {contextSettings=null;$("context-save").disabled=true;$("context-reset").disabled=true;contextMessage(t("上下文配置读取失败：")+error.message,true);}
}
async function saveContextSettings(reset=false) {
  if(!contextSettings)return;
  const values={};
  for(const key of ["context","compact"]) {
    const input=$(key+"-tokens"),useDefault=reset||$(key+"-default").checked;
    if(!useDefault && !input.reportValidity())return;
    values[key+"_tokens"]=useDefault?null:Math.round(Number(input.value)*1000);
  }
  try {
    const result=await api("/api/context-settings",values);fillContextSettings(result);
    contextMessage(result.changed?t("已保存，重启 ChatGPT App 生效。"):t("设置未变化。"));
  } catch(error) {contextMessage(error.message,true);}
}
let updateStatus=null, updateBusy=false;
function renderUpdates() {
  if(!updateStatus)return;
  const u=updateStatus, busy=updateBusy||["checking","downloading","upgrading"].includes(u.state);
  $("updates-versions").textContent=t("当前版本：{version}").replace("{version}",u.current_version)+(u.latest_version?" · "+t("最新版本：{version}").replace("{version}",u.latest_version):"");
  $("updates-source").textContent=u.repository;$("updates-source").href="https://github.com/"+u.repository;
  $("automatic-updates").checked=!!u.automatic;$("automatic-updates").disabled=updateBusy;
  $("updates-check").disabled=busy;$("updates-install").disabled=busy||u.state!=="available";
  const labels={idle:"可检查 GitHub 上的最新版本。",checking:"正在检查更新…",current:"已是最新版本。",available:"发现新版本，可以升级。",downloading:"正在下载并校验升级包…",upgrading:"正在升级，旧请求继续完成…",complete:"升级已完成。"};
  $("updates-message").textContent=u.state==="error"?u.error:t(labels[u.state]||"可检查 GitHub 上的最新版本。");
  $("updates-message").className="help"+(u.state==="error"?" error":"");
}
async function loadUpdates() {
  try {updateStatus=await api("/api/updates");renderUpdates();}
  catch(error){$("updates-message").textContent=error.message;}
}
async function performUpdate(which) {
  updateBusy=true;renderUpdates();
  try {updateStatus=await api("/api/updates",{action:which});}
  catch(error){updateStatus={...updateStatus,state:"error",error:error.message};}
  finally {updateBusy=false;renderUpdates();}
}
async function openSettings() {
  await Promise.all([refreshStatus(true),loadContextSettings(),loadUpdates()]);
  $("settings-dialog").showModal();
}
async function selectPacket(id,refresh=false) {
  const requestId=++state.detailRequest;
  const m=await api("/api/packets/"+id); if(requestId!==state.detailRequest || (refresh && state.selected!==id)) return;
  const changed=state.selected!==id, previousDetail=state.detail; state.selected=id; state.detail=m; state.detailVersion=[m.state,m.status,m.duration_ms,m.outbound_wire_bytes,m.outbound_content_encoding].join(":");
  $("empty-state").hidden=true; $("packet-detail").hidden=false; renderList();
  if(changed || tokenVersion(m)!==tokenVersion(previousDetail||{})) {cancelTokenRead();tokenWindow=cachedTokenWindow(m);}
  else if(tokenWindow?.state==="unavailable") tokenWindow=null;
  renderMetrics(m);
  loadTokenWindow();
  renderOverview(m); renderInputItems(m); renderHeaders(m);
  if(changed) {state.path=""; state.text=null; $("body-results").hidden=true; $("text-viewer").hidden=true; await loadTree("");if(requestId===state.detailRequest)await loadDiff();}
  else if(state.tab==="diff") await loadDiff();
}
function renderMetrics(m) {
  const upload=uploadBytes(m);
  $("metrics").replaceChildren(metric(t("网关上传正文"),bytes(upload),upload===null?t("尚未采集出口大小"):"Content-Encoding: "+(m.outbound_content_encoding||"identity")),tokenMetric());
}
function bars(title, rows, total) {
  const p=panel(title);
  for(const r of rows) { const row=el("div","bar-row"), label=el("div","bar-label"); label.append(link(r.name,()=>goPath(r.path)),el("span","",bytes(r.bytes)+" · "+(r.bytes/(total||1)*100).toFixed(1)+"%")); const progress=el("progress");progress.max=total||1;progress.value=r.bytes;row.append(label,progress);p.append(row); }
  return p;
}
function inputRows(items) {
  return items.map(i=>{const cell=el("div");cell.append(link(kind(i.kind)+(i.name?" · "+i.name:""),()=>goPath(i.path)),el("span","cell-note",i.label||""));return ["#"+i.index,cell,bytes(i.bytes)];});
}
function renderOverview(m) {
  const root=$("overview");root.replaceChildren();
  if(m.inspection!=="json") {root.append(el("p","muted",t("正文未展开：")+(m.inspection_error||t("正文为空"))+t("。转发仍按原始正文进行。")));return;}
  const note=el("p","muted help",t("字段构成按未压缩 JSON 统计，各字段不能直接换算成独立的压缩上传大小。")); root.append(note);
  if(m.overflow_original_wire_bytes!==undefined && (m.overflow_compaction_calls || m.overflow_cache_hit || m.overflow_cache_reset)) {
    root.append(el("p","muted help",t("超限兜底：原始上传 ")+bytes(m.overflow_original_wire_bytes)+t("；本次压缩 ")+(m.overflow_compaction_calls||0)+t(" 次")+(m.overflow_cache_hit?t("；复用已有摘要"):"")+(m.overflow_cache_reset?t("；旧映射已清空"):"")+t("。下方展示的是原始请求的脱敏副本。")));
  }
  const cols=el("div","two-columns");
  const fields=[...(m.top_fields||[]).map(r=>({...r,path:"/"+r.name.replaceAll("~","~0").replaceAll("/","~1")})),{name:t("字段名、标点及空白"),bytes:m.json_envelope_bytes,path:""}].sort((a,b)=>b.bytes-a.bytes);
  cols.append(bars(t("顶层字段"),fields,m.decoded_bytes));
  const groups=Object.entries(m.input_groups||{}).map(([name,size])=>({name:kind(name),bytes:size,path:"/input"})).sort((a,b)=>b.bytes-a.bytes);
  cols.append(bars(t("input 字段构成"),groups,(m.top_fields.find(r=>r.name==="input")||{}).bytes));root.append(cols);
}
function renderInputItems(m) {
  const root=$("input-items");root.replaceChildren();
  const items=m.inspection==="json"?[...(m.input_items||[])].sort((a,b)=>b.bytes-a.bytes).slice(0,100):[];
  if(!items.length) {root.append(el("p","muted",t("暂无可展示的输入条目。")));return;}
  const biggest=panel(t("输入条目 · 按大小排序"));
  biggest.append(table([t("位置"),t("类型与内容摘要"),t("原始字节")],inputRows(items)),el("p","muted help",t("显示最大的 100 项；完整输入可在“内容浏览”查看。")));root.append(biggest);
}
async function loadTree(path) {
  const id=state.selected; state.path=path; $("tree-query").value=path; const root=$("json-tree");root.replaceChildren(el("span","muted",t("加载中…")));
  try {const n=await api("/api/packets/"+id+"/node?path="+encodeURIComponent(path));if(state.selected!==id || state.path!==path)return;root.replaceChildren(treeNode(n,id,true));}
  catch(e){if(state.selected===id && state.path===path)root.replaceChildren(el("span","muted",e.message));}
}
function treeNode(node,id,opened=false) {
  const container=node.kind==="object"||node.kind==="array", branch=el(container?"details":"div","tree-node");
  if(!container) {const line=el("div","tree-leaf");line.append(link(node.label,()=>loadText(node.path)),el("span","tree-type",node.kind),el("span","tree-size",bytes(node.bytes)),el("span","tree-preview",String(node.preview??"null").slice(0,180)));branch.append(line);return branch;}
  const summary=el("summary"); summary.append(el("span","tree-key",node.label),el("span","tree-type",node.kind+" · "+node.count+t(" 项")),el("span","tree-size",bytes(node.bytes)));
  const children=el("div");branch.append(summary,children);let loaded=false;
  async function page(data,append=false) {if(!append) children.replaceChildren();for(const child of data.children||[])children.append(treeNode(child,id));if(data.next_offset!==null&&data.next_offset!==undefined){const more=link(t("加载更多子节点…"),async()=>{const next=await api("/api/packets/"+id+"/node?"+new URLSearchParams({path:node.path,offset:data.next_offset}));more.remove();await page(next,true);});children.append(more);}}
  branch.addEventListener("toggle",async()=>{if(!branch.open||loaded)return;loaded=true;try{await page(node.children?node:await api("/api/packets/"+id+"/node?path="+encodeURIComponent(node.path)));}catch(e){children.textContent=e.message;}});
  if(opened) branch.open=true; return branch;
}
async function goPath(path) {showTab("tree");await loadTree(path);}
async function loadText(path,append=false) {
  const id=state.selected, offset=append&&state.text?state.text.next_offset:0;
  const data=await api("/api/packets/"+id+"/text?"+new URLSearchParams({path,offset}));if(state.selected!==id)return;
  if(!append) $("text-content").textContent="";$("text-content").append(document.createTextNode(data.text));$("text-path").textContent=path||t("根节点");$("text-viewer").hidden=false;
  state.text={path,...data};$("text-more").hidden=data.next_offset===null;$("text-position").textContent=t("已显示 ")+nf.format(offset+data.text.length)+" / "+nf.format(data.total_chars)+t(" 字符");
}
async function submitTreeQuery() {
  const query=$("tree-query").value.trim();
  $("text-viewer").hidden=true;
  if(!query || query.startsWith("/")) {
    $("body-results").hidden=true;
    await loadTree(query);
  } else await searchBody(query);
}
async function searchBody(query) {const id=state.selected,results=await api("/api/packets/"+id+"/search?q="+encodeURIComponent(query));if(state.selected!==id || $("tree-query").value.trim()!==query)return;const root=$("body-results");root.hidden=false;root.replaceChildren(el("small","muted",t("找到 ")+results.length+t(" 项（最多 60 项）")));for(const hit of results){const n=el("div","search-hit");n.append(link(hit.path||t("根节点"),async()=>{await loadTree(hit.path);await loadText(hit.path);}),el("p","",hit.preview));root.append(n);}}
async function loadDiff() {
  const id=state.selected,data=await api("/api/packets/"+id+"/diff");if(state.selected!==id)return;const root=$("diff-content");root.replaceChildren();
  if(!data){root.append(el("p","muted",t("还没有可比较的上一条同会话记录。会话 ID 未识别时不自动配对。")));return;}
  const p=panel(t("相对上一条同会话请求"));fact(p,t("网关上传正文变化"),signed(data.upload_delta));fact(p,t("未压缩 JSON 变化"),signed(data.decoded_delta));root.append(p);
  const f=panel(t("顶层字段变化"));f.append(table([t("字段"),t("之前"),t("当前"),t("差值")],data.fields.map(r=>[r.name,bytes(r.before),bytes(r.after),signed(r.after-r.before)])));root.append(f);
  for(const [label,items] of [[t("新增或内容改变的条目"),data.added],[t("移除或内容改变前的条目"),data.removed]]) {const n=panel(label+" · "+items.length); n.append(table([t("位置"),t("类型"),t("字节")],items.slice(0,100).map(i=>["#"+i.index,kind(i.kind)+(i.name?" · "+i.name:""),bytes(i.bytes)])));root.append(n);}
  root.append(el("p","muted help",t("按条目内容指纹与出现次数比较；条目改变会表现为移除旧条目并新增新条目，未据此推断裁剪是否安全。")));
}
function renderHeaders(m) {
  const root=$("headers-content");root.replaceChildren();const p=panel(t("请求与响应"));
  if(m.source!=="live")fact(p,t("数据来源"),t("历史重建（大小为估算）"));
  fact(p,t("网关上传正文"),bytes(uploadBytes(m)));
  fact(p,t("上传编码"),uploadBytes(m)===null?"—":m.outbound_content_encoding||"identity");
  fact(p,t("采集入口正文"),bytes(m.wire_bytes)+" · "+(m.content_encoding||"identity"));
  fact(p,t("未压缩 JSON"),bytes(m.decoded_bytes));
  for(const [label,value] of [[t("模型"),m.model||t("未标记")],[t("路径"),m.method+" "+m.path],[t("会话"),m.thread_id||t("未识别")],[t("请求正文 SHA-256"),m.sha256],[t("响应状态"),m.status||t("尚未收到")],[t("响应正文流量"),bytes(m.response_bytes)],[t("首部等待时间"),m.headers_ms===undefined?"—":m.headers_ms+" ms"],[t("采集点"),t("Codex 发送后、现有出口处理前")]])fact(p,label,String(value));root.append(p);
  const h=panel(t("请求头 · 认证值不保存"));h.append(table([t("名称"),t("可保存的值")],Object.entries(m.headers||{})));root.append(h);
  root.append(el("p","muted help",t("未知头字段仅保留名称，避免记录认证信息。原始认证头仍会转发给指定上游。响应只记录状态、耗时与流量，不保存正文。")));
}
async function setInterfaceLanguage(language) {
  if(language===uiI18n.language)return;
  const buttons=document.querySelectorAll("#language-toggle button");
  for(const button of buttons)button.disabled=true;
  try {
    uiI18n.setLanguage(language);
    state.listVersion="";state.sessionVersion="";
    renderStatus();renderContextModel();
    if(state.status) {
      const p=state.status.profile;
      $("link-status").textContent=providerStatus(state.status);
      if(!("overflow_compaction_enabled" in state.status))$("overflow-message").textContent=t("当前中转暂不支持此功能。");
    }
    if(state.detail) {
      renderMetrics(state.detail);renderOverview(state.detail);renderInputItems(state.detail);renderHeaders(state.detail);
      const selected=state.selected, path=state.path, query=$("tree-query").value, searchVisible=!$("body-results").hidden;
      await loadTree(path);
      if(state.selected===selected && state.path===path && $("tree-query").value===path) {
        $("tree-query").value=query;
        if(searchVisible)await searchBody(query);
      }
      if(state.text) {
        $("text-path").textContent=state.text.path||t("根节点");
        $("text-position").textContent=t("已显示 ")+nf.format($("text-content").textContent.length)+" / "+nf.format(state.text.total_chars)+t(" 字符");
      }
      await loadDiff();
    }
    if(state.status)await refreshFilters();
  } finally {for(const button of buttons)button.disabled=false;}
}
action("language-zh",()=>setInterfaceLanguage("zh"));
action("language-en",()=>setInterfaceLanguage("en"));
action("capture-toggle",async()=>{await api("/api/settings",{capture_enabled:!state.status.capture_enabled});await refreshStatus();});
action("settings-open",openSettings);action("empty-connect",openSettings);action("settings-close",()=>$("settings-dialog").close());
action("ui-refresh",()=>{if(state.status?.ui_live_reload)location.reload();});
$("ui-development").addEventListener("change",async()=>{
  const checkbox=$("ui-development"),previous=!!state.status?.ui_development?.enabled;
  checkbox.disabled=true;
  try {
    await api("/api/ui-development",{enabled:checkbox.checked});
    location.reload();
  } catch(error) {checkbox.checked=previous;fail(error);}
  finally {checkbox.disabled=!!state.status?.ui_read_only;}
});
action("entry-port-save",async()=>{
  const input=$("entry-port");if(!input.reportValidity())return;
  const result=await api("/api/entry-port",{port:Number(input.value)});
  state.status.entry_port=result;fillEntryPort();
  const node=$("entry-port-message");node.textContent=t(result.pending?"已保存端口。入口服务重新启动后应用，重启 ChatGPT App 生效。":"端口设置已保存，当前端口无需切换。");node.hidden=false;
});
action("context-save",()=>saveContextSettings());action("context-reset",()=>saveContextSettings(true));
$("updates-check").addEventListener("click",()=>performUpdate("check"));
$("updates-install").addEventListener("click",()=>performUpdate("install"));
$("automatic-updates").addEventListener("change",async()=>{
  updateBusy=true;
  const enabled=$("automatic-updates").checked;
  try {updateStatus=await api("/api/updates",{action:"configure",automatic:enabled});}
  catch(error){updateStatus={...updateStatus,state:"error",error:error.message};}
  finally {updateBusy=false;renderUpdates();}
});
setInterval(()=>{if($("settings-dialog").open && !updateBusy && updateStatus && (["checking","downloading","upgrading"].includes(updateStatus.state)||updateStatus.automatic))loadUpdates();},2000);
$("request-compression").addEventListener("change",async()=>{
  const select=$("request-compression"),previous=state.status.request_compression;
  select.disabled=true;
  try {
    const result=await api("/api/settings",{request_compression:select.value});state.status=result;
    const node=$("compression-message");node.textContent=result.request_compression==="zstd"?t("已开启请求压缩。"):t("已关闭请求压缩。");node.hidden=false;
  } catch(error) {select.value=previous;fail(error);} finally {select.disabled=false;}
});
$("overflow-compaction").addEventListener("change",async()=>{
  const checkbox=$("overflow-compaction"),previous=!!state.status.overflow_compaction_enabled;
  checkbox.disabled=true;
  try {
    const result=await api("/api/settings",{overflow_compaction_enabled:checkbox.checked});state.status=result;
    checkbox.checked=!!result.overflow_compaction_enabled;
    const node=$("overflow-message");node.textContent=checkbox.checked?t("已开启超限兜底。"):t("已关闭超限兜底。");node.hidden=false;
  } catch(error) {checkbox.checked=previous;fail(error);} finally {checkbox.disabled=false;}
});
for(const key of ["context","compact"]) {
  $(key+"-tokens").addEventListener("input",()=>{$(key+"-range").value=$(key+"-tokens").value;contextMessage("");});
  $(key+"-range").addEventListener("input",()=>{$(key+"-tokens").value=$(key+"-range").value;contextMessage("");});
  $(key+"-default").addEventListener("change",()=>{syncContextDefaults();contextMessage("");});
}
action("runtime-settings-save",async()=>{
  const values={};
  if(state.status.start_on_login_supported)values.start_on_login=$("start-on-login").checked;
  if(state.status.capacity_retry_supported)values.capacity_retry_enabled=$("capacity-retry").checked;
  const imageChanged=state.status.image_isolation_supported&&($("image-isolation").checked!==!!state.status.image_isolation_enabled);
  if(state.status.image_isolation_supported)values.image_isolation_enabled=$("image-isolation").checked;
  await api("/api/settings",values);await refreshStatus(true);
  $("runtime-settings-message").textContent=t(imageChanged?"图片工具设置已保存，请重新加载 Codex 后继续使用。":"运行设置已保存。");$("runtime-settings-message").hidden=false;
});
action("settings-save",async()=>{await api("/api/settings",{budget_bytes:Number($("budget").value),max_storage_mb:Number($("storage-limit").value),max_packets:Number($("packet-limit").value)});await refreshStatus(true);message(t("观察设置已保存。"));if(state.selected)await selectPacket(state.selected,true);});
async function linkAction(which) {await api("/api/link",{action:which,...(which==="activate"?{upstream:state.status.upstream}:{})});await refreshStatus(true);message((which==="activate"?t("已接入。"):t("已恢复原配置。"))+t("重启 ChatGPT App 生效。"));}
action("link-activate",()=>linkAction("activate"));action("link-restore",()=>linkAction("restore"));
$("session-search").addEventListener("input",renderSessions);
$("packet-list").addEventListener("scroll",loadMorePackets,{passive:true});
action("tree-query-go",submitTreeQuery);action("text-close",()=>{$("text-viewer").hidden=true;});action("text-more",()=>loadText(state.text.path,true));
for(const b of document.querySelectorAll("[data-tab]")) b.addEventListener("click",()=>showTab(b.dataset.tab));
for(const b of document.querySelectorAll("[data-sort]")) b.addEventListener("click",()=>{
  const key=b.dataset.sort;
  state.direction=state.sort===key?(state.direction==="asc"?"desc":"asc"):"desc";
  state.sort=key;
  renderSort();
  loadPackets().catch(fail);
});
for(const select of document.querySelectorAll("[data-filter]")) select.addEventListener("change",()=>{
  state.filters[select.dataset.filter]=select.value;
  select.classList.toggle("filtered",!!select.value);
  select.title=t("筛选")+filterLabels[select.dataset.filter]+"："+(select.value?(select.selectedOptions[0]?.title||select.value):t("全部"));
  loadPackets().catch(fail);
});
$("tree-query").addEventListener("keydown",e=>{if(e.key==="Enter" && !e.isComposing){e.preventDefault();Promise.resolve().then(submitTreeQuery).catch(fail);}});
async function poll() {
  if(state.polling || document.hidden)return;
  state.polling=true;
  try {
    await refreshStatus();
    const revision=state.status.storage.revision, pending=[];
    if(state.filterRevision!==revision)pending.push(refreshFilters());
    if(!state.older && state.listRevision!==revision)pending.push(loadPackets(false,true));
    await Promise.all(pending);
    await Promise.all([refreshSessionSummaries(),loadTokenWindow()]);
  } catch(e) {fail(e);} finally {state.polling=false;}
}
const pollTimer=setInterval(poll,2000);
document.addEventListener("visibilitychange",()=>{if(!document.hidden){loadTokenWindow();loadMorePackets();}});
(async()=>{state.polling=true;try{await Promise.all([refreshStatus(true),refreshFilters(),loadPackets()]);await refreshSessionSummaries();if(!state.status.profile.active && state.status.storage.count===0)await openSettings();}finally{state.polling=false;}})().catch(fail);
