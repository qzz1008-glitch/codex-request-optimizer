"use strict";
const $ = (id) => document.getElementById(id);
history.replaceState(null, "", "/");
const state = { status: null, packets: [], sessions: [], selected: null, detail: null, tab: "overview", path: "", text: null, older: false, polling: false, detailVersion: "", detailRequest: 0, listVersion: "", sessionVersion: "", sort: "created", direction: "desc", filters: {kind:"",status:"",thread:""}, canLoadMore: false, listRequest: 0, listController: null, listRevision: null, filterRevision: null };
const nf = new Intl.NumberFormat("zh-CN");
let requestBodyCache=null, requestBodyDownloadUrl="", requestBodySequence=0;
let contextSettings = null;
let settingsEditing = false, settingsSaving = false, settingsOpening = false, settingsRevision = 0;
let linkSaving = "", linkRevision = 0;
let settingsBaseline = null, settingsLanguage = null;
let compressionTarget = null, compressionSaving = false, compressionRevision = 0;
let tokenWindow = null, tokenRequest = 0, tokenLoading = false, tokenTimer = null, tokenController = null, tokenPendingRetries = 0;
const tokenCache = new Map();
const listTokenCache = new Map(), listTokenRequests = new Map(), listTokenCells = new Map(), visibleTokenRows = new Set();
let tokenSortCache = null;
const listTokenObserver = new IntersectionObserver(entries=>{
  for(const entry of entries) {
    const id=entry.target.dataset.packetId;
    if(entry.isIntersecting && entry.target.isConnected)visibleTokenRows.add(id);else visibleTokenRows.delete(id);
  }
  loadListTokens();
},{root:$("packet-list"),rootMargin:"100px"});
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
function originalBytes(p) { return Number.isFinite(p.decoded_bytes) && p.decoded_bytes >= 0 ? p.decoded_bytes : null; }
function compact(n) { if (n === undefined || n === null) return "—"; return n >= 1048576 ? (n/1048576).toFixed(2)+" MiB" : n >= 1024 ? (n/1024).toFixed(1)+" KiB" : n+" B"; }
function renderStorageInfo() {
  const storage=state.status?.storage;if(!storage)return;
  const sessions=state.filterRevision!==null?nf.format(state.sessions.length):"—";
  $("storage-info").replaceChildren(
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
  if(!select)return;
  if(current && !options.some(o=>o.value===current)) options.push({value:current,label:select.selectedOptions[0]?.textContent||current});
  const signature=JSON.stringify([current,options]);
  if(select.dataset.optionsVersion===signature || document.activeElement===select)return;
  select.dataset.optionsVersion=signature;
  const entries=[{value:"",label:key==="kind"?t("全部类型"):t("全部")},...options];
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
    const tokens=data.token_window, estimated=tokens?.estimated, used=estimated?tokens?.used_tokens:tokens?.input_tokens;
    const available=tokens?.state==="available"&&Number.isFinite(used)&&used>=0, capacity=tokens?.window_tokens;
    const tokenText=available?(estimated?"≈ ":"")+nf.format(used):"—";
    const usage=el("span","session-token","Token "+tokenText);
    usage.title=available?(estimated?t("压缩后上下文总量估算"):t("最近请求输入 Token"))+(capacity?t(" · 窗口 ")+nf.format(used)+" / "+nf.format(capacity)+(estimated?"":" · "+(used/capacity*100).toFixed(1)+"%"):"")+(tokens.recorded_at?" · "+timestampFormat.format(new Date(tokens.recorded_at*1000)):""):t("尚无可用的输入 Token 记录");
    meta.append(usage,when);item.append(meta);
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
  requestBodyCache=null;requestBodySequence++;clearJsonDownload();
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
  if(packet?.state!=="complete" || data.state!=="available" || data.window_pending)return;
  tokenCache.delete(packet.id);
  tokenCache.set(packet.id,{version:tokenVersion(packet),expires:Date.now()+300000,data});
  while(tokenCache.size>256)tokenCache.delete(tokenCache.keys().next().value);
}
async function loadTokenWindow() {
  const id=state.selected;
  if(!id || document.hidden || tokenLoading || (tokenWindow && !tokenWindow.window_pending && !["loading","pending"].includes(tokenWindow.state)))return;
  clearTimeout(tokenTimer);tokenTimer=null;
  const request=++tokenRequest;tokenLoading=true;
  const controller=new AbortController();tokenController=controller;
  const timeout=setTimeout(()=>controller.abort(),6000);
  try {
    const data=await api("/api/packets/"+id+"/token-window",undefined,controller.signal);
    if(request!==tokenRequest || state.selected!==id)return;
    tokenWindow=data;cacheTokenWindow(state.detail,data);cacheListTokens(state.detail,data);
  } catch(error) {
    if(request!==tokenRequest || state.selected!==id)return;
    tokenWindow={state:error.message==="unknown operation"?"unsupported":"unavailable"};
  } finally {
    clearTimeout(timeout);
    if(request===tokenRequest) {
      tokenLoading=false;tokenController=null;
      if(state.selected===id) {
        const delay=tokenWindow?.state==="loading"?50:(tokenWindow?.state==="pending"||tokenWindow?.window_pending)?(++tokenPendingRetries<=30?300:1000):null;
        if(delay!==null)tokenTimer=setTimeout(loadTokenWindow,delay);
      }
    }
  }
}
function panel(title) { const n = el("section","panel"); n.append(el("h3","",title)); return n; }
function table(headings, rows) { const wrap = el("div","table-scroll"), t = el("table"), head=el("thead"), tr=el("tr"); for (const h of headings) tr.append(el("th","",h)); head.append(tr); t.append(head); const body=el("tbody"); for (const cells of rows) { const row=el("tr"); for (const cell of cells) {const td=el("td"); if (cell instanceof Node) td.append(cell); else td.textContent = cell; row.append(td);} body.append(row); } t.append(body); wrap.append(t); return wrap; }
function link(text, fn) { const n=el("button","link-button",text); n.addEventListener("click",()=>Promise.resolve().then(fn).catch(fail)); return n; }
function fact(n,label,value) { const r=el("div","fact-row"); r.append(el("span","",label),el("span","",value)); n.append(r); }
function showTab(tab) { state.tab=tab; for (const b of document.querySelectorAll("[data-tab]")) b.classList.toggle("active",b.dataset.tab===tab); for (const t of ["overview","compression","tree","diff"]) $("tab-"+t).hidden=t!==tab; }
function packetStatus(p) {
  if(p.state==="relay_error" || p.status>=400)return "failed";
  if(["client_disconnected","interrupted"].includes(p.state))return "interrupted";
  if(p.state==="streaming")return "streaming";
  if(p.state==="complete")return "complete";
  return "waiting";
}
function listTokens(p) {
  const sorted=tokenSortCache?.entries.get(p.id);
  if(sorted && sorted.version===tokenVersion(p) && sorted.expires>Date.now())return sorted.data;
  const cached=cachedTokenWindow(p);
  if(cached)return cached;
  const entry=listTokenCache.get(p.id);
  if(entry && entry.version===tokenVersion(p) && entry.expires>Date.now())return entry.data;
  if(p.method!=="POST" || p.is_compaction || p.status!==200 || !["streaming","complete"].includes(p.state))return {state:p.state==="captured"?"pending":"unavailable"};
  return null;
}
function renderListTokens(p) {
  const cell=listTokenCells.get(p.id);if(!cell)return;
  const data=listTokens(p), available=data?.state==="available" && !data.estimated && Number.isInteger(data.input_tokens) && data.input_tokens>=0;
  cell.textContent=available?nf.format(data.input_tokens):"—";
  cell.title=available?t("本次请求的真实输入 Token"):!data || ["loading","pending"].includes(data.state)?t("等待输入 Token 记录"):t("暂无对应的输入 Token 记录");
}
function cacheListTokens(p,data) {
  if(!p)return;
  listTokenCache.delete(p.id);
  const lifetime=data.state==="available"?300000:["loading","pending"].includes(data.state)?1500:30000;
  listTokenCache.set(p.id,{version:tokenVersion(p),expires:Date.now()+lifetime,data});
  const sorted=tokenSortCache?.entries.get(p.id);
  if(sorted && sorted.version===tokenVersion(p)) {
    if(sortInputTokens(sorted.data)!==sortInputTokens(data)) {tokenSortCache.dirty=true;queueMicrotask(refreshTokenSort);}
    sorted.data=data;sorted.expires=Date.now()+lifetime;
  }
  while(listTokenCache.size>512)listTokenCache.delete(listTokenCache.keys().next().value);
  const current=state.packets.find(packet=>packet.id===p.id);
  if(current)renderListTokens(current);
}
function loadListTokens() {
  if(document.hidden)return;
  for(const id of visibleTokenRows) {
    const p=state.packets.find(packet=>packet.id===id);if(!p)continue;
    renderListTokens(p);
    if(listTokens(p) || listTokenRequests.has(id))continue;
    if(listTokenRequests.size>=3)break;
    const controller=new AbortController(), timeout=setTimeout(()=>controller.abort(),6000);
    listTokenRequests.set(id,controller);
    api("/api/packets/"+id+"/token-window",undefined,controller.signal)
      .then(data=>{cacheTokenWindow(p,data);cacheListTokens(p,data);})
      .catch(()=>cacheListTokens(p,{state:"unavailable"}))
      .finally(()=>{clearTimeout(timeout);listTokenRequests.delete(id);loadListTokens();});
  }
}
function renderSort() {
  for (const b of document.querySelectorAll("[data-sort]")) {
    const active=b.dataset.sort===state.sort;
    b.closest("th").setAttribute("aria-sort",active?(state.direction==="asc"?"ascending":"descending"):"none");
    b.querySelector(".sort-arrow").textContent=active?(state.direction==="asc"?"↑":"↓"):"";
  }
}
function sortInputTokens(data) {
  return data?.state==="available" && !data.estimated && Number.isInteger(data.input_tokens) && data.input_tokens>=0?data.input_tokens:null;
}
function sortedTokenPackets(cache,direction) {
  const counts=new Map(cache.records.map(p=>[p.id,sortInputTokens(listTokens(p))]));
  return cache.records.slice().sort((a,b)=>{
    const x=counts.get(a.id), y=counts.get(b.id);
    if(x===null && y!==null)return 1;
    if(y===null && x!==null)return -1;
    return (x!==null && y!==null?(x-y)*(direction==="asc"?1:-1):0) || b.created-a.created || b.id.localeCompare(a.id);
  });
}
function refreshTokenSort() {
  const cache=tokenSortCache;
  if(state.sort!=="input_tokens" || !cache?.dirty || state.listController)return;
  cache.dirty=false;
  state.packets=sortedTokenPackets(cache,state.direction).slice(0,state.packets.length);
  state.canLoadMore=state.packets.length<cache.records.length;
  renderList();
}
async function tokenSortedPackets(query,revision,signal) {
  const filters=new URLSearchParams(query);
  for(const key of ["limit","sort","direction","after"])filters.delete(key);
  const key=filters.toString(), previous=tokenSortCache?.key===key?tokenSortCache:null;
  let cache=previous;
  if(!cache || (!query.has("after") && (cache.revision!==revision || cache.expires<=Date.now()))) {
    let records=[];
    if(previous?.revision===revision)records=previous.records;
    else {
      const scan=new URLSearchParams(filters);scan.set("limit","300");scan.set("sort","created");scan.set("direction","desc");
      for(;;) {
        const page=await api("/api/packets?"+scan,undefined,signal);
        signal.throwIfAborted();records.push(...page);
        if(page.length<300)break;
        scan.set("after",page.at(-1).id);
      }
    }
    const entries=new Map();let cursor=0;
    const read=async()=>{
      while(cursor<records.length) {
        signal.throwIfAborted();const p=records[cursor++];let data=listTokens(p);
        if(!data || ["loading","pending"].includes(data.state) && p.status===200 && ["streaming","complete"].includes(p.state)) {
          const controller=new AbortController(), cancel=()=>controller.abort(), timeout=setTimeout(cancel,6000);
          signal.addEventListener("abort",cancel,{once:true});
          try {
            const deadline=Date.now()+3000;
            do {
              data=await api("/api/packets/"+p.id+"/token-window",undefined,controller.signal);
              if(data.state==="loading")await new Promise(resolve=>setTimeout(resolve,75));
            } while(data.state==="loading" && Date.now()<deadline);
            cacheTokenWindow(p,data);cacheListTokens(p,data);
          } catch(error) {signal.throwIfAborted();data={state:"unavailable"};}
          finally {clearTimeout(timeout);signal.removeEventListener("abort",cancel);}
        }
        const lifetime=["loading","pending"].includes(data?.state)?1500:sortInputTokens(data)===null?30000:300000;
        entries.set(p.id,{version:tokenVersion(p),data,expires:Date.now()+lifetime});
      }
    };
    await Promise.all(Array.from({length:Math.min(3,records.length)},read));signal.throwIfAborted();
    cache={key,revision,records,entries,dirty:false,expires:Date.now()+300000};
    for(const entry of entries.values())cache.expires=Math.min(cache.expires,entry.expires);
    tokenSortCache=cache;
  }
  const sorted=sortedTokenPackets(cache,query.get("direction"));
  cache.dirty=false;
  const start=query.has("after")?sorted.findIndex(p=>p.id===query.get("after"))+1:0;
  return sorted.slice(start,start+Number(query.get("limit")));
}
function renderList() {
  if(!state.status)return;
  const filtered=Object.values(state.filters).some(Boolean);
  const sessionCount=state.status.storage.threads?.find(row=>(row.thread||"")===(state.filters.thread==="unknown"?"":state.filters.thread.slice(7)))?.count;
  const total=!filtered?state.status.storage.count:!state.filters.kind && !state.filters.status && state.filters.thread && Number.isFinite(sessionCount)?sessionCount:null;
  $("record-count").textContent=total===null?nf.format(state.packets.length)+(state.canLoadMore?"+":"")+t(" 包"):nf.format(total)+t(" 包");
  const signature=JSON.stringify([state.selected,state.status.budget_bytes,state.older,state.canLoadMore,state.sort,state.direction,state.filters,state.packets.map(p=>[p.id,p.created,p.method,p.is_compaction,p.state,p.status,p.duration_ms,p.decoded_bytes,p.outbound_wire_bytes,p.outbound_content_encoding,p.thread_id,p.overflow_compaction_calls,p.overflow_cache_hit,p.overflow_original_wire_bytes])]);
  if(signature===state.listVersion)return;
  state.listVersion=signature;
  const root=$("packet-rows"), fragment=document.createDocumentFragment(); renderSort();
  listTokenObserver.disconnect();visibleTokenRows.clear();listTokenCells.clear();
  $("list-empty").hidden=!!state.packets.length;
  $("list-empty").textContent=Object.values(state.filters).some(Boolean)?t("没有符合筛选条件的请求。"):t("暂无请求记录。");
  for (const p of state.packets) {
    const protection=[];
    if(p.overflow_compaction_calls>0)protection.push(t("压缩 ")+nf.format(p.overflow_compaction_calls)+t(" 次"));
    if(p.overflow_cache_hit===true)protection.push(t("复用摘要"));
    const row=el("tr",["request-"+packetStatus(p),state.selected===p.id?"selected":"",protection.length?"overflow-protected":""].filter(Boolean).join(" ")), date=new Date(p.created*1000);
    const time=el("button","packet-open");
    time.append(el("span","packet-date",dateFormat.format(date)),el("span","packet-clock",clockFormat.format(date)));
    time.title=timestampFormat.format(date);
    time.setAttribute("aria-label",t("查看 ")+time.title+t(" 的请求"));
    const type=requestKind(p);
    const original=originalBytes(p), upload=uploadBytes(p);
    const before=el("span","packet-size",compact(original));
    before.title=original===null?t("原始大小未记录"):t("压缩前：")+bytes(original);
    const after=el("span","packet-size",compact(upload));
    after.title=upload===null?t("尚未采集最终上传大小"):t("最终上传正文：")+bytes(upload)+" · "+(p.outbound_content_encoding||"identity");
    row.dataset.packetId=p.id;row.dataset.session=p.thread_id||"";row.title=(states[p.state]||p.state)+(p.status?" · HTTP "+p.status:"")+"\n"+(p.thread_id?"session_id: "+p.thread_id:t("会话未识别"));
    if(protection.length)row.title+=t("\n已应用超限保护：")+protection.join(" · ")+(Number.isFinite(p.overflow_original_wire_bytes)?t(" · 原始上传 ")+compact(p.overflow_original_wire_bytes):"");
    for (const value of [time,type,before,after]) {const td=el("td");if(value instanceof Node)td.append(value);else td.textContent=value;row.append(td);}
    const tokens=el("td","packet-tokens");listTokenCells.set(p.id,tokens);renderListTokens(p);row.append(tokens);
    row.addEventListener("click",()=>selectPacket(p.id).catch(fail)); fragment.append(row);
  }
  root.replaceChildren(fragment);
  for(const row of root.children)listTokenObserver.observe(row);
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
  $("list-loading").hidden=!append && state.sort!=="input_tokens";
  $("list-loading").textContent=t(state.sort==="input_tokens"?"正在读取输入 Token…":"正在加载更多记录…");
  let loaded=false;
  const revision=state.status?.storage.revision;
  const q=new URLSearchParams({limit:"100",sort:state.sort,direction:state.direction});
  if(state.filters.kind)q.set("kind",state.filters.kind);
  if(state.filters.status)q.set("status",state.filters.status);
  if(state.filters.thread==="unknown")q.set("thread_unknown","1");
  else if(state.filters.thread)q.set("thread",state.filters.thread.slice(7));
  if (append && state.packets.length) q.set("after",state.packets.at(-1).id);
  try {
    const values=state.sort==="input_tokens"?await tokenSortedPackets(q,revision,controller.signal):await api("/api/packets?"+q,undefined,controller.signal);
    if(requestId!==state.listRequest)return;
    state.listRevision=revision;
    state.canLoadMore=values.length===100;
    if (append) {state.packets.push(...values); state.older=true;} else {state.packets=values;state.older=false;}
    renderList();
    loaded=true;
    if (!state.selected && state.packets.length) await selectPacket(state.packets[0].id,false,requestId);
    else if (state.selected) { const p=state.packets.find(p=>p.id===state.selected); if(p) {const ver=[p.state,p.status,p.duration_ms,p.outbound_wire_bytes,p.outbound_content_encoding].join(":");if(ver!==state.detailVersion) await selectPacket(state.selected,true);} }
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
  const revision=settingsRevision, connectionRevision=linkRevision, modeRevision=compressionRevision, status=await api("/api/status");
  if(settingsSaving||compressionSaving||revision!==settingsRevision||connectionRevision!==linkRevision||modeRevision!==compressionRevision)return;
  state.status=status;renderStatus(fill);
}
function renderConnectionActions() {
  const s=state.status;if(!s)return;
  const p=s.profile,disabled=!!(linkSaving||settingsSaving||compressionSaving||s.ui_read_only);
  $("header-connect").hidden=!!p.active;
  for(const id of ["header-connect","link-activate"]) {
    const button=$(id);
    button.disabled=disabled||!!p.active||!!p.config_error;
    button.textContent=t(linkSaving==="activate"?"正在接入…":"接入 Codex");
    button.setAttribute("aria-busy",String(linkSaving==="activate"));
  }
  $("link-restore").disabled=disabled||!p.can_restore;
  renderCompressionMode();
}
function renderStatus(fill=false) {
  const s=state.status;if(!s)return;
  const preview=!!s.ui_live_reload && !!s.ui_read_only;
  $("ui-refresh").hidden=!s.ui_live_reload;
  $("preview-banner").hidden=!preview;
  document.documentElement.classList.toggle("preview-mode",preview);
  if(preview) {
    const style=getComputedStyle(document.documentElement);
    const width=parseInt(style.getPropertyValue("--desktop-width")), height=parseInt(style.getPropertyValue("--desktop-height"));
    $("preview-banner").textContent=t("桌面窗口预览 · ")+width+" × "+height+t(s.ui_entry_port_editable?" · 保存后自动刷新":" · 保存后自动刷新 · 设置只读");
  }
  const connection=$("codex-status"), unknown=!!(s.profile.config_error || s.provider_sync_error), connected=!!s.profile.active;
  const connectionText=unknown?t("接入状态未知"):connected?t("已接入 Codex"):t("未接入 Codex");
  if(connection.textContent!==connectionText)connection.textContent=connectionText;
  connection.classList.toggle("disconnected",unknown || !connected);
  connection.title=providerStatus(s);
  renderConnectionActions();
  $("empty-connect").hidden=!!s.profile.active;
  renderStorageInfo();
  if(fill) fillSettings();else {fillEntryPort();renderCompressionMode();renderLanguageSelection();}
  renderSessions();renderList();
}
function fillSettings() {
  const s=state.status,p=s.profile;
  fillEntryPort();
  if(!settingsEditing) {
    $("storage-limit").value=s.max_storage_mb;$("packet-limit").value=s.max_packets;
    $("start-on-login").checked=!!s.start_on_login;
    $("capacity-retry").checked=!!s.capacity_retry_enabled;
    $("image-isolation").checked=!!s.image_isolation_enabled;
  }
  for(const id of ["storage-limit","packet-limit"])$(id).disabled=settingsSaving||!!s.ui_read_only;
  $("start-on-login").disabled=settingsSaving||!!s.ui_read_only||!s.start_on_login_supported;
  $("capacity-retry").disabled=settingsSaving||!!s.ui_read_only||!s.capacity_retry_supported;
  $("image-isolation").disabled=settingsSaving||!!s.ui_read_only||!s.image_isolation_supported;
  $("image-isolation-hint").textContent=t(s.image_isolation_supported?"勾选后自动安装缺少的独立图片工具，保留已有工具，并关闭原生 view_image。主请求只接收图片路径和解析文字；需重新加载 Codex。":"当前中转暂不支持此功能。");
  $("startup-hint").textContent=t(s.start_on_login_supported?(s.startup_state_known===false?"无法读取开机启动状态，请重新保存设置。":"登录 Windows 后在后台启动，可从托盘打开窗口。"):"开机启动仅支持已安装的 Windows 版本。");
  $("runtime-settings-message").hidden=!!s.capacity_retry_supported;
  $("runtime-settings-message").textContent=s.capacity_retry_supported?"":t("当前中转暂不支持此功能。");
  renderConnectionActions();
  renderCompressionMode();renderLanguageSelection();fillImageDirectory();fillPacketProtection();syncContextDefaults();
  updateSettingsSaveState(false);
}
function renderLanguageSelection() {
  for(const button of document.querySelectorAll("#language-toggle button")) {
    button.setAttribute("aria-pressed",String(button.dataset.language===(settingsEditing?settingsLanguage:uiI18n.language)));
    button.disabled=settingsSaving;
  }
}
function renderCompressionMode() {
  const s=state.status;if(!s)return;
  const mode=s.request_compression,connected=!!s.profile?.active;
  const supported=["off","zstd"].includes(s.request_compression);
  for(const button of document.querySelectorAll("#request-compression button")) {
    button.setAttribute("aria-pressed",String(button.dataset.compression===mode));
    button.disabled=compressionSaving||settingsSaving||!!linkSaving||!!s.ui_read_only||!supported||!connected;
  }
  $("request-compression").setAttribute("aria-busy",String(compressionSaving));
  $("compression-hint").textContent=!connected?t("接入 Codex 后可切换传输模式。"):supported?t(mode==="zstd"?"无损压缩，减小上传体积，保留完整内容。":"不额外压缩，原样转发并分析请求。"):t("当前中转暂不支持此功能。");
  $("request-compression").title=$("compression-hint").textContent;
  $("compression-confirm-dialog").setAttribute("aria-busy",String(compressionSaving));
  for(const id of ["compression-confirm-cancel","compression-confirm-apply"])$(id).disabled=compressionSaving;
  $("compression-confirm-apply").textContent=t(compressionSaving?"正在切换…":"确认切换");
  if(!connected&&!compressionSaving&&$("compression-confirm-dialog").open)cancelCompressionChange();
}
function requestCompressionChange(mode) {
  const s=state.status,dialog=$("compression-confirm-dialog");
  if(!s||!s.profile?.active||compressionSaving||settingsSaving||settingsOpening||settingsEditing||linkSaving||s.ui_read_only||dialog.open)return;
  if(!["off","zstd"].includes(mode)||!["off","zstd"].includes(s.request_compression)||mode===s.request_compression)return;
  compressionTarget=mode;
  $("compression-confirm-description").textContent=t(mode==="zstd"?
    "切换为「zstd 压缩」？将无损压缩请求正文，减小上传体积。":
    "切换为「仅分析」？将关闭 zstd 传输压缩，继续记录和分析请求。");
  $("compression-confirm-message").hidden=true;
  dialog.showModal();
}
function cancelCompressionChange() {
  if(compressionSaving)return;
  $("compression-confirm-dialog").close();compressionTarget=null;
}
async function confirmCompressionChange() {
  const s=state.status,mode=compressionTarget;
  if(!s||!s.profile?.active||compressionSaving||settingsSaving||linkSaving||s.ui_read_only||!["off","zstd"].includes(mode)||!["off","zstd"].includes(s.request_compression))return;
  if(mode===s.request_compression){cancelCompressionChange();return;}
  compressionSaving=true;compressionRevision++;renderConnectionActions();
  $("compression-confirm-message").hidden=true;
  try {
    const result=await api("/api/settings",{request_compression:mode});
    if(result.request_compression!==mode)throw Error(t("传输模式未保存，请重试。"));
    state.status={...state.status,...result};
    $("compression-confirm-dialog").close();compressionTarget=null;
    renderStatus();
    message(t(mode==="zstd"?"已切换为 zstd 压缩。":"已切换为仅分析。"));
  } catch(error) {
    $("compression-confirm-message").textContent=error.message;
    $("compression-confirm-message").hidden=false;
  } finally {compressionSaving=false;compressionRevision++;renderConnectionActions();}
}
function imageDefaultDirectory() {
  const s=state.status,dataDir=s.data_dir||"",separator=dataDir.includes("\\")?"\\":"/";
  return s.image_save_dir_default||(dataDir?dataDir.replace(/[\\/]+$/,"")+separator+"image-attachments":"");
}
function fillImageDirectory() {
  const s=state.status,input=$("image-save-dir"),supported=!!s.image_save_dir_supported;
  const defaultPath=imageDefaultDirectory();
  if(!settingsEditing)input.value=s.image_save_dir||defaultPath;
  input.placeholder=defaultPath;
  for(const id of ["image-save-dir","image-save-dir-reset"])$(id).disabled=settingsSaving||!!s.ui_read_only||!supported;
  $("image-save-dir-hint").textContent=supported?
    t("默认路径：{path}。修改后用于新保存的图片，原目录文件保留。",{path:defaultPath}):
    t("当前中转暂不支持自定义图片目录，请升级中转后设置。");
}
function fillEntryPort() {
  const s=state.status,port=s.entry_port,supported=!!s.entry_port_supported&&!!port;
  if(!settingsEditing)$("entry-port").value=port?.configured_port??port?.current_port??s.relay_port??47844;
  $("entry-port").disabled=settingsSaving||!supported||!!s.ui_read_only&&!s.ui_entry_port_editable;
  renderEntryPortStatus();
}
function renderEntryPortStatus() {
  const s=state.status;if(!s)return;
  const port=s.entry_port,node=$("link-status"),supported=!!s.entry_port_supported&&!!port;
  let label="未接管",kind="inactive";
  if(!supported)label="入口端口暂不可用。";
  else if(port.error){label=port.error;kind="error";}
  else if(s.profile?.config_error||s.provider_sync_error){label="无法读取 Codex 配置。";kind="error";}
  else if(settingsEditing&&settingsBaseline&&$("entry-port").value!==settingsBaseline.entry_port){label="未保存";kind="unsaved";}
  else if(port.pending){label="重启 ChatGPT App 生效";kind="pending";}
  else if(s.profile?.active){label="已接管";kind="active";}
  node.textContent=t(label);node.dataset.state=kind;
  node.title=supported?t("当前端口：{port}").replace("{port}",port.current_port)+(port.pending?" · "+t("待生效端口：{port}").replace("{port}",port.configured_port)+"\n"+t("入口服务重新启动后应用新端口，重启 ChatGPT App 生效。"):""):"";
}
function providerStatus(s) {
  if(s.profile.config_error || s.provider_sync_error)return t("无法读取 Codex 配置。");
  if(s.auto_detect_providers && s.profile.all_providers)return t("已自动接管 {count} 个 provider").replace("{count}",s.provider_count);
  return s.profile.active?t("已配置接入"):t("尚未接入");
}
function contextMessage(text,error=false) { const node=$("context-message");node.textContent=text;node.className="help"+(error?" error":"");node.hidden=!text; }
function syncContextDefaults() {
  for(const key of ["context","compact"]) {
    const unavailable=settingsSaving||!contextSettings||!!state.status?.ui_read_only;
    $(key+"-default").disabled=unavailable;
    $(key+"-tokens").disabled=unavailable||$(key+"-default").checked;
    $(key+"-range").disabled=$(key+"-tokens").disabled;
  }
  $("context-reset").disabled=settingsSaving||!contextSettings||!!state.status?.ui_read_only;
}
function fillContextSettings(data) {
  contextSettings=data;
  const maximum=data.max_context_tokens/1000;
  renderContextModel();
  for(const [key,value,fallback] of [["context",data.context_tokens,data.default_context_tokens||240000],["compact",data.compact_tokens,220000]]) {
    if(!settingsEditing)$(key+"-default").checked=value===null;
    $(key+"-tokens").max=String(maximum);$(key+"-range").max=String(Math.max(key==="context"?4:1,Math.floor(maximum)));
    const display=value??(key==="context"?data.default_context_tokens:null);
    if(!settingsEditing){$(key+"-tokens").value=display===null?"":String(display/1000);$(key+"-range").value=String((value??fallback)/1000);}
    $(key+"-tokens").placeholder=t("模型默认");
  }
  syncContextDefaults();
}
function renderContextModel() {
  const data=contextSettings;if(!data)return;
  $("context-model").textContent=(data.model?t("全局模型：")+data.model:t("未指定模型"))+(data.default_context_tokens?t(" · 默认 ")+nf.format(data.default_context_tokens/1000)+"k":"");
  for(const key of ["context","compact"])$(key+"-tokens").placeholder=t("模型默认");
}
async function loadContextSettings() {
  contextMessage("");
  try {fillContextSettings(await api("/api/context-settings"));}
  catch(error) {contextSettings=null;syncContextDefaults();contextMessage(t("上下文配置读取失败：")+error.message,true);}
}
let updateStatus=null, updateBusy=false, updateAction="", updateFeedbackVisible=false;
function readSettingsDraft() {
  const draft={
    entry_port:$("entry-port").value,
    packet_size_protection_enabled:$("packet-size-protection").checked,
    budget:$("budget").value,
    start_on_login:$("start-on-login").checked,
    capacity_retry_enabled:$("capacity-retry").checked,
    image_isolation_enabled:$("image-isolation").checked,
    image_save_dir:$("image-save-dir").value,
    max_storage_mb:$("storage-limit").value,
    max_packets:$("packet-limit").value,
    language:settingsLanguage??uiI18n.language
  };
  for(const key of ["context","compact"])draft[key+"_tokens"]=$(key+"-default").checked?null:$(key+"-tokens").value;
  return draft;
}
function changedSettings(draft=readSettingsDraft()) {
  return settingsBaseline?Object.keys(draft).filter(key=>draft[key]!==settingsBaseline[key]):[];
}
function setSettingsMessage(text,error=false) {
  const node=$("settings-message");node.textContent=text;node.className="help "+(error?"error":"muted");
}
function updateSettingsSaveState(clearMessage=true) {
  const dirty=settingsEditing&&changedSettings().length>0;
  $("settings-save").disabled=settingsSaving||!!linkSaving||!dirty;
  $("settings-cancel").disabled=settingsSaving;$("settings-close").disabled=settingsSaving;
  if(clearMessage&&settingsEditing&&!settingsSaving)setSettingsMessage(dirty?t("有未保存的修改"):"");
  if(state.status)renderEntryPortStatus();
}
async function saveSettings() {
  if(!settingsEditing||settingsSaving||linkSaving||!settingsBaseline)return;
  const draft=readSettingsDraft(),changed=new Set(changedSettings(draft)),s=state.status;
  if(!changed.size)return;
  for(const input of $("settings-dialog").querySelectorAll(".settings-content input:not(:disabled)")) {
    if(!input.reportValidity())return;
  }
  const editable=!s.ui_read_only,protectionSupported="packet_size_protection_enabled" in s;
  const entries=[
    ["packet_size_protection_enabled","packet_size_protection_enabled",draft.packet_size_protection_enabled,protectionSupported],
    ["budget","budget_bytes",Math.round(Number(draft.budget)*1000),protectionSupported],
    ["start_on_login","start_on_login",draft.start_on_login,s.start_on_login_supported],
    ["capacity_retry_enabled","capacity_retry_enabled",draft.capacity_retry_enabled,s.capacity_retry_supported],
    ["image_isolation_enabled","image_isolation_enabled",draft.image_isolation_enabled,s.image_isolation_supported],
    ["image_save_dir","image_save_dir",draft.image_save_dir===imageDefaultDirectory()?"":draft.image_save_dir,s.image_save_dir_supported],
    ["max_storage_mb","max_storage_mb",Number(draft.max_storage_mb),true],
    ["max_packets","max_packets",Number(draft.max_packets),true]
  ].filter(([field,,,supported])=>editable&&supported&&changed.has(field));
  const contextChanged=editable&&contextSettings&&["context_tokens","compact_tokens"].some(key=>changed.has(key));
  const portChanged=s.entry_port_supported&&s.entry_port&&(!s.ui_read_only||s.ui_entry_port_editable)&&changed.has("entry_port");
  const controls=new Map([...$("settings-dialog").querySelectorAll("input,button")].map(node=>[node,node.disabled]));
  let saved=0,error=null,restartRequired=false;
  settingsSaving=true;settingsRevision++;
  for(const node of controls.keys())node.disabled=true;
  $("settings-dialog").setAttribute("aria-busy","true");setSettingsMessage(t("正在保存设置…"));
  try {
    if(entries.length) {
      const values=Object.fromEntries(entries.map(([,key,value])=>[key,value]));
      const result=await api("/api/settings",values);
      state.status={...state.status,...result};
      for(const [field] of entries)settingsBaseline[field]=draft[field];
      restartRequired=entries.some(([field])=>field==="image_isolation_enabled");saved++;
    }
    if(contextChanged) {
      const values=Object.fromEntries(["context_tokens","compact_tokens"].map(key=>[key,draft[key]===null?null:Math.round(Number(draft[key])*1000)]));
      contextSettings=await api("/api/context-settings",values);
      settingsBaseline.context_tokens=draft.context_tokens;settingsBaseline.compact_tokens=draft.compact_tokens;
      restartRequired=restartRequired||contextSettings.changed;saved++;
    }
    if(portChanged) {
      state.status.entry_port=await api("/api/entry-port",{port:Number(draft.entry_port)});
      settingsBaseline.entry_port=draft.entry_port;restartRequired=restartRequired||state.status.entry_port.pending;saved++;
    }
    if(changed.has("language")) {
      try {await setInterfaceLanguage(draft.language);}
      finally {if(uiI18n.language===draft.language)settingsBaseline.language=draft.language;}
      saved++;
    }
    if(changedSettings().length)throw Error(t("有未保存的修改"));
  } catch(failure) {error=failure;}
  finally {
    settingsSaving=false;settingsRevision++;
    for(const [node,disabled] of controls)if(node.isConnected)node.disabled=disabled;
    $("settings-dialog").setAttribute("aria-busy","false");
    fillSettings();renderContextModel();renderUpdates();renderStatus();updateSettingsSaveState(false);
    if(error)setSettingsMessage(t(saved?"部分设置已保存，剩余修改未保存：":"保存失败：")+error.message,true);
    else setSettingsMessage(t(restartRequired||state.status.entry_port?.pending?"设置已保存，重启 ChatGPT App 生效。":"设置已保存。"));
  }
}
function renderUpdates() {
  const u=updateStatus, busy=settingsSaving||updateBusy||["checking","downloading","upgrading"].includes(u?.state);
  if(u?.current_version)$("updates-versions").textContent=t("当前版本：{version}").replace("{version}",u.current_version)+(u.latest_version?" · "+t("最新版本：{version}").replace("{version}",u.latest_version):"");
  $("updates-check").disabled=busy;$("updates-install").disabled=busy||u?.state!=="available";
  const labels={idle:"可检查 GitHub 上的最新版本。",checking:"正在检查更新…",current:"已是最新版本。",available:"发现新版本，可以升级。",downloading:"正在下载并校验升级包…",upgrading:"正在升级，旧请求继续完成…",complete:"升级已完成。"};
  const displayedState=updateBusy?(updateAction==="check"?"checking":"downloading"):u?.state;
  const message=$("updates-message");
  message.hidden=!updateFeedbackVisible;
  message.textContent=displayedState==="error"?u.error:t(labels[displayedState]||labels.idle);
  message.classList.toggle("error",displayedState==="error");
}
async function loadUpdates() {
  try {updateStatus=await api("/api/updates");renderUpdates();}
  catch(error){updateStatus={...updateStatus,state:"error",error:error.message};renderUpdates();}
}
async function performUpdate(which) {
  if(settingsSaving||updateBusy)return;
  updateBusy=true;updateAction=which;updateFeedbackVisible=true;renderUpdates();
  try {updateStatus=await api("/api/updates",{action:which});}
  catch(error){updateStatus={...updateStatus,state:"error",error:error.message};}
  finally {updateBusy=false;updateAction="";renderUpdates();}
}
async function openSettings() {
  if(settingsOpening||compressionSaving||$("compression-confirm-dialog").open||$("settings-dialog").open)return;
  settingsOpening=true;
  try {
    await Promise.all([refreshStatus(true),loadContextSettings(),loadUpdates()]);
    settingsLanguage=uiI18n.language;
    settingsBaseline=readSettingsDraft();settingsEditing=true;
    renderLanguageSelection();renderCompressionMode();updateSettingsSaveState();
    $("settings-dialog").showModal();
  } finally {settingsOpening=false;}
}
async function selectPacket(id,refresh=false,expectedListRequest=null) {
  const requestId=++state.detailRequest;
  const m=await api("/api/packets/"+id); if(requestId!==state.detailRequest || (refresh && state.selected!==id) || (expectedListRequest!==null && expectedListRequest!==state.listRequest)) return;
  const changed=state.selected!==id, previousDetail=state.detail; state.selected=id; state.detail=m; state.detailVersion=[m.state,m.status,m.duration_ms,m.outbound_wire_bytes,m.outbound_content_encoding].join(":");
  $("empty-state").hidden=true; $("packet-detail").hidden=false; renderList();
  if(changed || tokenVersion(m)!==tokenVersion(previousDetail||{})) {cancelTokenRead();tokenWindow=cachedTokenWindow(m);}
  else if(tokenWindow?.state==="unavailable") tokenWindow=null;
  loadTokenWindow();
  renderOverview(m); renderCompression(m); renderHeaders(m);
  if(changed)$("diff-content").replaceChildren(el("p","muted",t("加载中…")));
  if(changed) {state.path=""; state.text=null; $("body-results").hidden=true; await loadTree("");if(requestId===state.detailRequest)await loadDiff();}
  else if(state.tab==="diff") await loadDiff();
}
function renderCompression(m) {
  const root=$("compression-content");root.replaceChildren();
  const sizes=panel(t("正文大小")), original=originalBytes(m), upload=uploadBytes(m);
  const before=m.overflow_original_wire_bytes??upload;
  fact(sizes,t("压缩前的请求正文"),bytes(original));
  fact(sizes,t("传输压缩后（摘要前）"),bytes(before));
  fact(sizes,t("实际上传正文"),bytes(upload));
  fact(sizes,t("最终上传编码"),upload===null?"—":m.outbound_content_encoding||"identity");
  if(original>0 && upload!==null)fact(sizes,t("总体减小"),(100*(1-upload/original)).toFixed(1)+"%");
  root.append(sizes);
  const steps=panel(t("压缩与触发记录"));
  fact(steps,t("Codex 原生 compact 请求"),m.is_compaction?t("是"):t("否"));
  fact(steps,t("中转生成摘要"),nf.format(m.overflow_compaction_calls||0)+t(" 次"));
  fact(steps,t("中转复用摘要"),m.overflow_cache_hit?t("是"):t("否"));
  if(m.overflow_covered_items)fact(steps,t("摘要覆盖历史条目"),nf.format(m.overflow_covered_items));
  if(m.overflow_compaction_ms!==undefined)fact(steps,t("摘要处理耗时"),nf.format(m.overflow_compaction_ms)+" ms");
  root.append(steps);
}
function bars(title, rows, total) {
  const p=panel(title);
  for(const r of rows) { const row=el("div","bar-row"), label=el("div","bar-label"); label.append(link(r.name,()=>goPath(r.path)),el("span","",bytes(r.bytes)+" · "+(r.bytes/(total||1)*100).toFixed(1)+"%")); const progress=el("progress");progress.max=total||1;progress.value=r.bytes;row.append(label,progress);p.append(row); }
  return p;
}
function renderOverview(m) {
  const root=$("overview");root.replaceChildren();
  if(m.inspection!=="json") {root.append(el("p","muted",t("正文未展开：")+(m.inspection_error||t("正文为空"))+t("。转发仍按原始正文进行。")));return;}
  const cols=el("div","two-columns");
  const fields=[...(m.top_fields||[]).map(r=>({...r,path:"/"+r.name.replaceAll("~","~0").replaceAll("/","~1")})),{name:t("字段名、标点及空白"),bytes:m.json_envelope_bytes,path:""}].sort((a,b)=>b.bytes-a.bytes);
  cols.append(bars(t("顶层字段"),fields,m.decoded_bytes));
  const groups=Object.entries(m.input_groups||{}).map(([name,size])=>({name:kind(name),bytes:size,path:"/input"})).sort((a,b)=>b.bytes-a.bytes);
  cols.append(bars(t("input 字段构成"),groups,(m.top_fields.find(r=>r.name==="input")||{}).bytes));root.append(cols);
}
function clearJsonDownload() {
  if(requestBodyDownloadUrl)URL.revokeObjectURL(requestBodyDownloadUrl);
  requestBodyDownloadUrl="";
  const download=$("json-download");download.hidden=true;download.removeAttribute("href");
}
async function loadTree(path) {
  const id=state.selected, sequence=++requestBodySequence;
  state.path=path;$("tree-query").value=path;$("body-results").hidden=true;
  const root=$("json-tree");root.textContent=t("加载中…");clearJsonDownload();
  try {
    if(requestBodyCache?.id!==id) {
      requestBodyCache={id,promise:api("/api/packets/"+id+"/export").then(data=>{
        if(!data.view||!Object.prototype.hasOwnProperty.call(data.view,"body"))throw new Error(t("此请求没有可解析的 JSON 正文"));
        return data.view.body;
      })};
    }
    let value=await requestBodyCache.promise;
    if(state.selected!==id||sequence!==requestBodySequence)return;
    if(path) {
      if(!path.startsWith("/"))throw new Error(t("JSON 路径不存在。"));
      for(const segment of path.slice(1).split("/")) {
        const key=segment.replace(/~1/g,"/").replace(/~0/g,"~");
        if(value===null||typeof value!=="object"||!Object.prototype.hasOwnProperty.call(value,key))throw new Error(t("JSON 路径不存在。"));
        value=value[key];
      }
    }
    const text=JSON.stringify(value,null,2);
    root.textContent=text;root.scrollTop=0;root.scrollLeft=0;
    requestBodyDownloadUrl=URL.createObjectURL(new Blob([text+"\n"],{type:"application/json;charset=utf-8"}));
    const download=$("json-download");download.href=requestBodyDownloadUrl;download.download="codex-request-"+id+".json";download.hidden=false;
  } catch(error) {
    if(state.selected===id&&sequence===requestBodySequence) {requestBodyCache=null;root.textContent=error.message;}
  }
}
async function goPath(path) {showTab("tree");await loadTree(path);}
async function submitTreeQuery() {
  const query=$("tree-query").value.trim();
  if(!query || query.startsWith("/")) {
    $("body-results").hidden=true;
    await loadTree(query);
  } else await searchBody(query);
}
async function searchBody(query) {const id=state.selected,results=await api("/api/packets/"+id+"/search?q="+encodeURIComponent(query));if(state.selected!==id || $("tree-query").value.trim()!==query)return;const root=$("body-results");root.hidden=false;root.replaceChildren(el("small","muted",t("找到 ")+results.length+t(" 项（最多 60 项）")));for(const hit of results){const n=el("div","search-hit");n.append(link(hit.path||t("根节点"),()=>loadTree(hit.path)),el("p","",hit.preview));root.append(n);}}
function packetDiffRows(data) {
  const change=(before,after)=>before===null?"added":after===null?"removed":after>before?"grow":after<before?"shrink":"changed";
  const slot=item=>JSON.stringify([item.index,item.kind,item.name||""]);
  const removed=data.removed||[],oldBySlot=new Map(removed.map(item=>[slot(item),item])),used=new Set(),items=[];
  const itemRow=(beforeItem,afterItem)=>{
    const item=afterItem||beforeItem,before=beforeItem?.bytes??null,after=afterItem?.bytes??null;
    return {field:"input",scope:"item",index:item.index,kind:item.kind,name:item.name||"",before,after,
      delta:(after??0)-(before??0),change:change(before,after),path:afterItem?(afterItem.path||"/input/"+afterItem.index):null};
  };
  for(const item of data.added||[]) {
    const previous=oldBySlot.get(slot(item));
    if(previous&&!used.has(previous)) {used.add(previous);items.push(itemRow(previous,item));}
    else items.push(itemRow(null,item));
  }
  for(const item of removed)if(!used.has(item))items.push(itemRow(item,null));
  items.sort((a,b)=>a.index-b.index||(a.change==="removed"?-1:1));
  const fields=new Map();
  for(const field of data.fields||[]) {
    const before=field.before===0?null:field.before,after=field.after===0?null:field.after;
    fields.set(field.name,{field:field.name,scope:"field",before,after,delta:(after??0)-(before??0),
      change:change(before,after),path:after===null?null:"/"+field.name.replaceAll("~","~0").replaceAll("/","~1")});
  }
  const names=new Set(fields.keys());if(items.length)names.add("input");
  const rows=[];
  for(const name of [...names].sort()) {
    if(fields.has(name))rows.push(fields.get(name));
    if(name==="input")rows.push(...items);
  }
  return rows;
}
async function loadDiff() {
  const id=state.selected,data=await api("/api/packets/"+id+"/diff");if(state.selected!==id)return;const root=$("diff-content");root.replaceChildren();
  if(!data){root.append(el("p","muted",t("还没有可比较的上一条同会话记录。会话 ID 未识别时不自动配对。")));return;}
  const p=panel(t("相对上一条同会话请求"));fact(p,t("网关上传正文变化"),signed(data.upload_delta));fact(p,t("未压缩 JSON 变化"),signed(data.decoded_delta));root.append(p);
  const changes=panel(t("变化明细")),rows=packetDiffRows(data);
  const labels={added:t("新增"),removed:t("移除"),grow:t("增大"),shrink:t("缩小"),changed:t("内容改变")};
  if(rows.length)changes.append(table([t("所属字段"),t("条目"),t("变化"),t("之前"),t("当前"),t("差值")],rows.map(r=>{
    const entry=el("div");
    if(r.scope==="field")entry.append(el("span","muted",t("字段合计")));
    else {
      entry.append(r.path?link("#"+r.index,()=>goPath(r.path)):el("span","","#"+r.index));
      entry.append(el("span","cell-note",kind(r.kind)+(r.name?" · "+r.name:"")));
    }
    return [r.field,entry,labels[r.change],bytes(r.before),bytes(r.after),signed(r.delta)];
  })));
  else changes.append(el("p","muted",t("没有发生变化。")));
  root.append(changes);
}
function renderHeaders(m) {
  const root=$("headers-content");root.replaceChildren();const p=panel(t("请求与响应"));
  if(m.source!=="live")fact(p,t("数据来源"),t("历史重建（大小为估算）"));
  fact(p,t("模型"),m.model||t("未标记"));
  fact(p,t("响应状态"),String(m.status||t("尚未收到")));
  fact(p,t("响应头耗时"),Number.isFinite(m.headers_ms)&&m.headers_ms>=0?m.headers_ms+" ms":"—");
  fact(p,t("响应流量"),bytes(m.response_bytes));
  const upload=uploadBytes(m);
  if(Number.isFinite(m.wire_bytes)&&m.wire_bytes>=0&&upload!==null&&m.wire_bytes!==upload) {
    fact(p,t("采集入口正文"),bytes(m.wire_bytes)+" · "+(m.content_encoding||"identity"));
  }
  root.append(p);
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
      if(!("packet_size_protection_enabled" in state.status))$("packet-size-message").textContent=t("当前中转暂不支持此功能。");
    }
    if(state.detail) {
      renderOverview(state.detail);renderHeaders(state.detail);
      const selected=state.selected, path=state.path, query=$("tree-query").value, searchVisible=!$("body-results").hidden;
      await loadTree(path);
      if(state.selected===selected && state.path===path && $("tree-query").value===path) {
        $("tree-query").value=query;
        if(searchVisible)await searchBody(query);
      }
      await loadDiff();
    }
    if(state.status)await refreshFilters();
  } finally {for(const button of buttons)button.disabled=settingsSaving;renderLanguageSelection();}
}
for(const button of document.querySelectorAll("#language-toggle button"))action(button.id,()=>{
  if(!settingsEditing||settingsSaving)return;
  settingsLanguage=button.dataset.language;renderLanguageSelection();updateSettingsSaveState();
});
action("settings-open",openSettings);action("empty-connect",openSettings);
function discardSettingsDraft() {
  settingsEditing=false;settingsBaseline=null;settingsLanguage=null;
  if(state.status)fillSettings();if(contextSettings)fillContextSettings(contextSettings);renderUpdates();
  setSettingsMessage("");
}
function closeSettings() {
  if(settingsSaving)return;
  $("settings-dialog").close();discardSettingsDraft();
}
action("settings-close",closeSettings);action("settings-cancel",closeSettings);
$("settings-dialog").addEventListener("cancel",event=>{event.preventDefault();closeSettings();});
$("settings-dialog").addEventListener("close",()=>{
  if(!$("settings-dialog").open)discardSettingsDraft();
});
$("settings-dialog").querySelector(".settings-content").addEventListener("input",()=>updateSettingsSaveState());
$("settings-dialog").querySelector(".settings-content").addEventListener("change",()=>updateSettingsSaveState());
action("settings-save",saveSettings);
action("ui-refresh",()=>{if(state.status?.ui_live_reload)location.reload();});
action("context-reset",()=>{
  if(settingsSaving||!contextSettings||state.status.ui_read_only)return;
  for(const key of ["context","compact"])$(key+"-default").checked=true;
  syncContextDefaults();contextMessage("");updateSettingsSaveState();
});
$("updates-check").addEventListener("click",()=>performUpdate("check"));
$("updates-install").addEventListener("click",()=>performUpdate("install"));
setInterval(()=>{if($("settings-dialog").open&&!settingsSaving&&!updateBusy&&["checking","downloading","upgrading"].includes(updateStatus?.state))loadUpdates();},2000);
for(const button of document.querySelectorAll("#request-compression button"))action(button.id,()=>requestCompressionChange(button.dataset.compression));
action("compression-confirm-cancel",cancelCompressionChange);action("compression-confirm-apply",confirmCompressionChange);
$("compression-confirm-dialog").addEventListener("cancel",event=>{event.preventDefault();cancelCompressionChange();});
$("compression-confirm-dialog").addEventListener("close",()=>{if(!$("compression-confirm-dialog").open)compressionTarget=null;});
function fillPacketProtection() {
  const s=state.status,supported="packet_size_protection_enabled" in s;
  if(!settingsEditing){$("packet-size-protection").checked=!!s.packet_size_protection_enabled;$("budget").value=s.budget_bytes/1000;}
  $("budget").min=(s.packet_size_min_bytes||1024)/1000;$("budget").max=(s.packet_size_max_bytes||134217728)/1000;
  for(const id of ["packet-size-protection","budget"])$(id).disabled=settingsSaving||!!s.ui_read_only||!supported;
  $("packet-size-message").hidden=supported;
  if(!supported)$("packet-size-message").textContent=t("当前中转暂不支持此功能。");
}
for(const key of ["context","compact"]) {
  $(key+"-tokens").addEventListener("input",()=>{$(key+"-range").value=$(key+"-tokens").value;contextMessage("");});
  $(key+"-range").addEventListener("input",()=>{$(key+"-tokens").value=$(key+"-range").value;contextMessage("");});
  $(key+"-default").addEventListener("change",()=>{syncContextDefaults();contextMessage("");});
}
action("image-save-dir-reset",()=>{
  if(settingsSaving||state.status.ui_read_only||!state.status.image_save_dir_supported)return;
  $("image-save-dir").value=imageDefaultDirectory();updateSettingsSaveState();
});
async function linkAction(which) {
  const s=state.status;
  if(linkSaving||settingsSaving||compressionSaving||!s||s.ui_read_only)return;
  if(which==="activate"?(s.profile.active||s.profile.config_error):!s.profile.can_restore)return;
  linkSaving=which;linkRevision++;renderConnectionActions();updateSettingsSaveState(false);
  try {
    await api("/api/link",{action:which,...(which==="activate"?{upstream:s.upstream}:{})});
    linkRevision++;await refreshStatus(true);
    if(which==="activate"&&(!state.status.profile.active||state.status.profile.config_error||state.status.provider_sync_error))
      throw Error(t("接入未完成，请检查 Codex 配置后重试。"));
    message((which==="activate"?t("已接入。"):t("已恢复原配置。"))+t("重启 ChatGPT App 生效。"));
  } finally {linkSaving="";linkRevision++;renderConnectionActions();updateSettingsSaveState(false);}
}
action("header-connect",()=>linkAction("activate"));action("link-activate",()=>linkAction("activate"));action("link-restore",()=>linkAction("restore"));
$("session-search").addEventListener("input",renderSessions);
$("packet-list").addEventListener("scroll",loadMorePackets,{passive:true});
action("tree-query-go",submitTreeQuery);
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
    if(!state.older && (state.listRevision!==revision || state.sort==="input_tokens" && (tokenSortCache?.dirty || tokenSortCache?.expires<=Date.now())))pending.push(loadPackets(false,true));
    await Promise.all(pending);
    await Promise.all([refreshSessionSummaries(),loadTokenWindow()]);loadListTokens();
  } catch(e) {fail(e);} finally {state.polling=false;}
}
const pollTimer=setInterval(poll,2000);
document.addEventListener("visibilitychange",()=>{if(!document.hidden){loadTokenWindow();loadMorePackets();loadListTokens();}});
(async()=>{state.polling=true;try{await Promise.all([refreshStatus(true),refreshFilters(),loadPackets()]);await refreshSessionSummaries();}finally{state.polling=false;}})().catch(fail);
