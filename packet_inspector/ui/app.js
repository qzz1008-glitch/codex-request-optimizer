"use strict";
const $ = (id) => document.getElementById(id);
function renderWindowState(value) {
  const enabled=!!value?.enabled,maximized=!!value?.maximized;
  document.documentElement.classList.toggle("desktop-titlebar",enabled);
  document.documentElement.classList.toggle("desktop-maximized",maximized);
  $("window-controls").hidden=!enabled;
  const button=$("window-maximize"),label=t(maximized?"还原":"最大化");
  button.title=label;button.setAttribute("aria-label",label);
}
async function initializeWindowControls() {
  const api=window.pywebview?.api;
  if(typeof api?.window_state!=="function")return;
  try {renderWindowState(await api.window_state());} catch(_) {}
}
window.addEventListener("pywebviewready",initializeWindowControls);
window.addEventListener("desktop-window-state",event=>renderWindowState(event.detail));
for(const [id,method] of [["window-minimize","minimize_window"],["window-maximize","toggle_maximize"],["window-close","close_window"]]) {
  $(id).addEventListener("click",async()=>{
    try {const value=await window.pywebview.api[method]();if(value)renderWindowState(value);} catch(_) {}
  });
}
document.querySelector(".app-header").addEventListener("dblclick",async event=>{
  if(!document.documentElement.classList.contains("desktop-titlebar")||event.target.closest("button,input,select,a,label"))return;
  try {renderWindowState(await window.pywebview.api.toggle_maximize());} catch(_) {}
});
initializeWindowControls();
history.replaceState(null, "", "/");
const state = { status: null, packets: [], sessions: [], selected: null, detail: null, tab: "headers", path: "", text: null, older: false, polling: false, detailVersion: "", detailRequest: 0, listVersion: "", sessionVersion: "", sort: "created", direction: "desc", filters: {kind:"",status:"",thread:""}, canLoadMore: false, listRequest: 0, listController: null, listRevision: null, filterRevision: null };
const nf = new Intl.NumberFormat("zh-CN");
let workspaceView="records",tokenTrendView=null;
let requestBodyCache=null, requestBodyDownloadUrl="", requestBodySequence=0;
let treeQueryTimer=null,requestBodyView=null,requestBodyHighlight=null;
let contextSettings = null;
const contextCustomValues = new Map();
let settingsEditing = false, settingsSaving = false, settingsOpening = false, settingsRevision = 0;
let linkSaving = "", linkRevision = 0;
let settingsBaseline = null, settingsLanguage = null;
let interfaceLanguageRevision = 0;
let settingsSaveProgress = {total:0,steps:[]};
const settingsRestartChanges = new Map();
const directoryOpening = new Set();
// Offline benchmarks, three randomized passes per level. Columns: level,
// size reduction below / at least 1,500,000 B (%), single-compression median (ms).
// Conversation: 120 retained requests, 61 exact originals and 59 sanitized copies.
// Images: seven generated PNG/JPEG/WebP fixtures; includes base64 JSON overhead.
const zstdLevelEstimates = [
  [1,48.3494,46.0992,2.827],
  [2,50.6355,48.0554,3.482],
  [3,51.8433,49.2755,4.441],
  [4,52.0046,49.4677,5.665],
  [5,52.5504,50.0394,8.728],
  [6,52.8725,50.3377,10.219],
  [7,52.973,50.4553,12.185],
  [8,53.1289,50.5485,13.88],
  [9,53.1362,50.5589,16.801],
  [10,53.1779,50.6168,23.731],
  [11,53.1976,50.6434,28.797],
  [12,53.1976,50.6434,28.328],
  [13,53.281,50.7022,58.257],
  [14,53.2976,50.727,62.246],
  [15,53.3038,50.7386,63.367],
  [16,53.7208,51.1575,140.005],
  [17,53.6927,50.9378,154.583],
  [18,53.3808,50.4464,223.843],
  [19,53.4301,50.5424,310.901],
  [20,53.4301,50.5424,308.92],
  [21,53.4394,50.5626,321.027],
  [22,53.4337,50.5681,334.609]
];
const imageZstdLevelEstimates = [
  [1,70.5345,25.0648,0.77],
  [2,71.169,25.0651,1.118],
  [3,71.5565,25.0535,1.182],
  [4,71.6367,25.0535,1.799],
  [5,71.8535,25.0543,1.783],
  [6,71.9274,25.0544,1.871],
  [7,71.9459,25.0543,2.016],
  [8,71.9191,25.0543,2.284],
  [9,71.9204,25.0551,2.438],
  [10,71.9274,25.0551,3.214],
  [11,71.9365,25.0551,3.162],
  [12,71.9362,25.0551,3.167],
  [13,71.9266,25.1097,7.071],
  [14,71.9378,25.1091,8.894],
  [15,71.9483,25.1081,12.744],
  [16,72.0734,25.1071,12.137],
  [17,72.0685,25.0963,13.561],
  [18,72.0886,25.068,23.31],
  [19,72.1214,25.0689,60.683],
  [20,72.1214,25.0689,58.33],
  [21,72.1235,25.0689,84.948],
  [22,72.1248,25.0689,150.028]
];
let compressionTarget = null, compressionSaving = false, compressionRevision = 0;
let connectionRestartNotified = false;
let exitRequest = null, exitSaving = false, exitFinished = false, exitRevision = 0;
let connectSource = null, connectSaving = false, connectChecking = false, startupConnectChecked = false;
let desktopOpenRevision = null;
let tokenWindow = null, tokenRequest = 0, tokenLoading = false, tokenTimer = null, tokenController = null, tokenPendingRetries = 0;
const tokenCache = new Map();
const listTokenCache = new Map(), listTokenRequests = new Map(), listTokenCells = new Map(), visibleTokenRows = new Set();
let tokenSortCache = null, tokenSortTimer = null;
const listTokenObserver = new IntersectionObserver(entries=>{
  for(const entry of entries) {
    const id=entry.target.dataset.packetId;
    if(entry.isIntersecting && entry.target.isConnected)visibleTokenRows.add(id);else visibleTokenRows.delete(id);
  }
  loadListTokens();
},{root:$("packet-list"),rootMargin:"100px"});
let sessionSummaries = [], sessionMetadataAt = 0, sessionMetadataProbeAt = 0, sessionMetadataLoading = false, sessionMetadataSupported = null;
const sessionConnectionObserver=new ResizeObserver(()=>renderSessionConnections());
const OTHER_SESSION_GROUP="group:other";
const auxiliaryLabels={image_analysis:"图片解析",next_message_suggestion:"消息建议",title_generation:"自动标题",session_index:"会话索引",subagent:"子任务"};
const requestPurposeCache=new Map();
function auxiliarySessionKind(row) {return Object.hasOwn(auxiliaryLabels,row?.session_kind)?row.session_kind:row?.title_source==="index_task"?"session_index":row?.title_source==="subagent"?"subagent":null;}
function sessionGroups() {
  const rows=new Map(sessionSummaries.map(row=>[row.thread_id,row]));
  for(const id of state.sessions)if(!rows.has(id))rows.set(id,{thread_id:id});
  const imageParents=new Set([...rows.values()].filter(row=>auxiliarySessionKind(row)==="image_analysis"&&
    typeof row.parent_thread_id==="string"&&row.parent_thread_id&&row.parent_thread_id!==row.thread_id).map(row=>row.parent_thread_id));
  for(const id of imageParents)if(!rows.has(id))rows.set(id,{thread_id:id,packet_count:0});
  const groups=new Map();
  for(const member of rows.values()) {
    if(member.packet_count===0&&!state.sessions.includes(member.thread_id))continue;
    let current=member,owner=null;const visited=new Set();
    for(let depth=0;current&&depth<32;depth++) {
      if(visited.has(current.thread_id))break;
      visited.add(current.thread_id);
      const kind=auxiliarySessionKind(current);
      if(kind==="subagent"||!kind&&(imageParents.has(current.thread_id)||typeof current.title==="string"&&current.title.trim())) {owner=current;break;}
      current=rows.get(current.parent_thread_id);
    }
    const id=owner?.thread_id||OTHER_SESSION_GROUP;
    if(!groups.has(id)) {
      const title=typeof owner?.title==="string"&&owner.title.trim()?owner.title:owner?
        auxiliarySessionKind(owner)==="subagent"?t("子任务"):owner.thread_id:t("其他");
      groups.set(id,{...(owner||{}),thread_id:id,title,members:[],packet_count:0,updated_at:owner?.updated_at||0});
    }
    const group=groups.get(id);group.members.push(member);
    group.packet_count+=Number.isFinite(member.packet_count)?member.packet_count:
      state.status?.storage?.threads?.find(row=>(row.thread||"")===member.thread_id)?.count||0;
    group.updated_at=Math.max(group.updated_at,member.updated_at||0);
    if(member.latest_packet&&(!group.latest_packet||member.latest_packet.created>group.latest_packet.created))group.latest_packet=member.latest_packet;
  }
  return [...groups.values()];
}
function otherTaskSessions() {return sessionGroups().find(group=>group.thread_id===OTHER_SESSION_GROUP)?.members||[];}
function selectedSessionGroup(groups=sessionGroups()) {
  const value=state.filters.thread;
  if(!value)return;
  const id=value===OTHER_SESSION_GROUP?value:value==="unknown"?"":value.slice(7);
  return groups.find(group=>group.thread_id===id||group.members.some(row=>row.thread_id===id));
}
function purposeFromMetadata(meta) {
  if(meta.image_isolation_worker===true)return "image_analysis";
  const kinds=[meta.request_purpose,meta.request_kind,meta.session_kind];
  return kinds.find(kind=>kind!=="subagent"&&Object.hasOwn(auxiliaryLabels,kind))??(kinds.includes("subagent")?"subagent":null);
}
function cacheRequestPurpose(meta) {
  const purpose=purposeFromMetadata(meta);requestPurposeCache.set(meta.id,purpose);
  while(requestPurposeCache.size>512)requestPurposeCache.delete(requestPurposeCache.keys().next().value);
  return purpose;
}
async function enrichSessionPurposes(rows) {
  // Older relays expose the latest user label only in packet metadata. Never fetch images or bodies here.
  const jobs=rows.filter(row=>!row.title&&!auxiliarySessionKind(row)&&row.latest_packet?.id&&!requestPurposeCache.has(row.latest_packet.id)).slice(0,8);
  let cursor=0;
  async function read() {
    while(cursor<jobs.length) {
      const row=jobs[cursor++];
      try {const meta=await api("/api/packets/"+row.latest_packet.id,undefined,AbortSignal.timeout(5000));cacheRequestPurpose(meta);}
      catch(_) {}
    }
  }
  await Promise.all(Array.from({length:Math.min(3,jobs.length)},read));
  return rows.map(row=>{
    const purpose=!row.title&&requestPurposeCache.get(row.latest_packet?.id);
    return purpose?{...row,session_kind:purpose}:row;
  });
}
let sessionLegacyCursor = 0;
const dateFormat = new Intl.DateTimeFormat("zh-CN",{month:"2-digit",day:"2-digit"});
const clockFormat = new Intl.DateTimeFormat("zh-CN",{hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false});
const timestampFormat = new Intl.DateTimeFormat("zh-CN",{year:"numeric",month:"numeric",day:"numeric",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false});
const names = uiI18n.labels({"message:user":"用户消息","message:assistant":"助手消息","message:developer":"开发者指令","message:system":"系统指令",reasoning:"推理（含密文）",function_call_output:"函数工具返回",custom_tool_call_output:"自定义工具返回",function_call:"函数工具调用",custom_tool_call:"自定义工具调用",additional_tools:"增量工具目录",tool_definition:"工具定义",compaction_trigger:"压缩触发项",compaction:"压缩状态",context_compaction:"压缩内容"});
const states = uiI18n.labels({captured:"待响应",streaming:"响应中",complete:"完成",client_disconnected:"客户端断开",relay_error:"转发失败",interrupted:"进程中断"});
function el(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; }
function setText(node,text) {if(node.textContent!==String(text))node.textContent=text;}
function renderNodeKey(node) {
  if(node.nodeType!==Node.ELEMENT_NODE)return null;
  if(node.id)return "id:"+node.id;
  if(node.dataset.packetId)return "packet:"+node.dataset.packetId;
  if(node.dataset.session!==undefined)return "session:"+node.dataset.session;
  return node.dataset.renderKey||null;
}
function reconcileNodes(root,content) {
  const keyed=new Map(Array.from(root.childNodes,node=>[renderNodeKey(node),node]).filter(([key])=>key!==null));
  let cursor=root.firstChild;
  for(const next of Array.from(content.childNodes)) {
    const key=renderNodeKey(next);
    let current=key!==null?keyed.get(key):cursor&&renderNodeKey(cursor)===null?cursor:null;
    if(current?.nodeType!==next.nodeType || current?.nodeName!==next.nodeName)current=null;
    if(!current) {root.insertBefore(next,cursor);current=next;}
    else {
      if(current!==cursor)root.insertBefore(current,cursor);
      if(current.nodeType===Node.ELEMENT_NODE) {
        for(const attribute of Array.from(current.attributes))if(!next.hasAttribute(attribute.name))current.removeAttribute(attribute.name);
        for(const attribute of next.attributes)if(current.getAttribute(attribute.name)!==attribute.value)current.setAttribute(attribute.name,attribute.value);
        current.onclick=next.onclick;current.onpointerdown=next.onpointerdown;
        reconcileNodes(current,next);
      } else if(current.nodeValue!==next.nodeValue)current.nodeValue=next.nodeValue;
    }
    cursor=current.nextSibling;
  }
  while(cursor) {const next=cursor.nextSibling;cursor.remove();cursor=next;}
}
function reconcileContent(root,...nodes) {
  const content=document.createDocumentFragment();content.append(...nodes);
  const selection=window.getSelection(),range=selection?.rangeCount&&!selection.isCollapsed?selection.getRangeAt(0):null;
  const saved=range&&selection.containsNode(root,true)?{
    start:range.startContainer,startOffset:range.startOffset,end:range.endContainer,endOffset:range.endOffset,
    anchor:selection.anchorNode,anchorOffset:selection.anchorOffset,focus:selection.focusNode,focusOffset:selection.focusOffset,
    text:selection.toString()
  }:null;
  reconcileNodes(root,content);
  // Moving an existing row can reset browser ranges. Restore the same endpoints
  // only while those nodes remain attached and still select exactly the same text.
  if(!saved||!saved.start.isConnected||!saved.end.isConnected)return;
  try {
    const after=document.createRange();after.setStart(saved.start,saved.startOffset);after.setEnd(saved.end,saved.endOffset);
    if(after.toString()!==saved.text)return;
    if(selection.anchorNode!==saved.anchor||selection.anchorOffset!==saved.anchorOffset||selection.focusNode!==saved.focus||selection.focusOffset!==saved.focusOffset)
      selection.setBaseAndExtent(saved.anchor,saved.anchorOffset,saved.focus,saved.focusOffset);
  } catch(_) {}
}
function bindSelectableRow(row,activate) {
  row.onpointerdown=event=>{event.currentTarget.selectionStartPoint={x:event.clientX,y:event.clientY};};
  row.onclick=event=>{
    const current=event.currentTarget,start=current.selectionStartPoint;
    current.selectionStartPoint=null;
    const dragged=start&&Math.hypot(event.clientX-start.x,event.clientY-start.y)>3;
    if(event.detail>0&&(dragged||event.detail>1)&&hasTextSelectionWithin(current))return;
    Promise.resolve().then(()=>activate(current)).catch(fail);
  };
}
function bytes(n) { return n === undefined || n === null ? "—" : nf.format(n) + " B"; }
function uploadBytes(p) { return Number.isFinite(p.outbound_wire_bytes) && p.outbound_wire_bytes >= 0 ? p.outbound_wire_bytes : null; }
function compressedBytes(p) { return [p.overflow_original_wire_bytes,p.usage_override_request_bytes,p.outbound_wire_bytes].find(value=>Number.isSafeInteger(value)&&value>=0)??null; }
let compressedSortSupported=true,compressedSortCache=null;
const previousPacketReads=new Map();
async function sessionPacketPage(query,signal) {
  if(!query.has("thread_group"))return api("/api/packets?"+query,undefined,signal);
  const ids=query.getAll("thread"), records=[];
  const base=new URLSearchParams(query);base.delete("thread_group");base.delete("thread");
  let cursor=0;
  async function read() {
    while(cursor<ids.length) {
      signal.throwIfAborted();
      const scan=new URLSearchParams(base),id=ids[cursor++];
      if(id)scan.set("thread",id);else scan.set("thread_unknown","1");
      records.push(...await api("/api/packets?"+scan,undefined,signal));
    }
  }
  await Promise.all(Array.from({length:Math.min(3,ids.length)},read));signal.throwIfAborted();
  const sort=query.get("sort"),direction=query.get("direction")==="asc"?1:-1;
  const value=packet=>sort==="decoded_bytes"?(originalBytes(packet)??-1):sort==="upload_bytes"?(uploadBytes(packet)??-1):sort==="compressed_bytes"?(compressedBytes(packet)??-1):packet.created;
  records.sort((a,b)=>(value(a)-value(b)||a.created-b.created||a.id.localeCompare(b.id))*direction);
  return records.slice(0,Number(query.get("limit")||100));
}
async function compressedSortedPackets(query,revision,signal) {
  if(compressedSortSupported) {
    try {return await sessionPacketPage(query,signal);}
    catch(error) {signal.throwIfAborted();if(error.message!=="invalid packet sort")throw error;compressedSortSupported=false;}
  }
  const filters=new URLSearchParams(query);
  for(const key of ["limit","sort","direction","after"])filters.delete(key);
  const key=filters.toString();let cache=compressedSortCache;
  if(!cache||cache.key!==key||(!query.has("after")&&cache.revision!==revision)) {
    const records=[],scan=new URLSearchParams(filters);scan.set("limit","300");scan.set("sort","created");scan.set("direction","desc");
    for(;;) {
      const page=await sessionPacketPage(scan,signal);signal.throwIfAborted();records.push(...page);
      if(page.length<300)break;
      scan.set("after",page.at(-1).id);
    }
    cache={key,revision,records};compressedSortCache=cache;
  }
  const direction=query.get("direction")==="asc"?1:-1;
  const sorted=cache.records.slice().sort((a,b)=>((compressedBytes(a)??-1)-(compressedBytes(b)??-1))*direction||b.created-a.created||b.id.localeCompare(a.id));
  const start=query.has("after")?sorted.findIndex(packet=>packet.id===query.get("after"))+1:0;
  return sorted.slice(start,start+Number(query.get("limit")));
}
async function compressedDelta(data,current) {
  if(Object.prototype.hasOwnProperty.call(data,"compressed_delta"))return Number.isSafeInteger(data.compressed_delta)?data.compressed_delta:null;
  if(!data.previous_id)return null;
  let previous=state.packets.find(packet=>packet.id===data.previous_id);
  if(!previous) {
    if(!previousPacketReads.has(data.previous_id)) {
      previousPacketReads.set(data.previous_id,api("/api/packets/"+data.previous_id).catch(()=>{previousPacketReads.delete(data.previous_id);return null;}));
      while(previousPacketReads.size>32)previousPacketReads.delete(previousPacketReads.keys().next().value);
    }
    previous=await previousPacketReads.get(data.previous_id);
  }
  const before=previous?compressedBytes(previous):null,after=current?compressedBytes(current):null;
  return before!==null&&after!==null?after-before:null;
}
function originalBytes(p) { return Number.isFinite(p.decoded_bytes) && p.decoded_bytes >= 0 ? p.decoded_bytes : null; }
function compact(n) { if (n === undefined || n === null) return "—"; return n >= 1000000 ? (n/1000000).toFixed(2)+" MB" : n >= 1000 ? (n/1000).toFixed(1)+" KB" : n+" B"; }
function renderStorageInfo() {
  const storage=state.status?.storage;if(!storage)return;
  reconcileContent($("storage-info"),
    el("span","",compact(storage.stored)+"/"+compact(storage.max_bytes)),
    el("span","",t("{count} 请求").replace("{count}",nf.format(storage.count)))
  );
}
function signed(n) { return (n > 0 ? "+" : "") + bytes(n); }
function kind(n) { return names[n] || n; }
function hasModelTokenUsage(p) {
  return p.method==="POST" && !p.is_compaction && p.request_kind!=="tool_search" &&
    (!p.path || /\/responses\/*(?:\?.*)?$/.test(p.path));
}
function requestKind(p) {
  if(p.request_kind==="image_generation" || /\/images\/generations\/*(?:\?.*)?$/.test(p.path||""))return t("图片生成");
  if(p.request_kind==="image_edit" || /\/images\/edits\/*(?:\?.*)?$/.test(p.path||""))return t("图片编辑");
  if(p.request_kind==="tool_search" || /\/alpha\/search\/*(?:\?.*)?$/.test(p.path||""))return t("工具搜索");
  if(p.is_compaction)return t("上下文压缩");
  const purpose=purposeFromMetadata(p)||requestPurposeCache.get(p.id)||auxiliarySessionKind(sessionSummaries.find(row=>row.thread_id===p.thread_id));
  return purpose&&purpose!=="subagent"?t(auxiliaryLabels[purpose]):p.method==="POST"?t("对话生成"):String(p.method||t("未知")).slice(0,10);
}
const requestKinds = uiI18n.labels({generation:"对话生成",compaction:"上下文压缩",image_generation:"图片生成",image_edit:"图片编辑",other:"其他请求"});
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
  setFilterOptions("kind",["generation","compaction",...["image_generation","image_edit","other"].filter(value=>options.kinds.includes(value))].map(value=>({value,label:requestKinds[value]})));
  setFilterOptions("status",options.statuses.map(value=>({value,label:statusLabel(value)})));
  state.sessions=options.threads;
  state.filterRevision=revision;
  renderStorageInfo();renderSessions();
}
async function refreshSessionSummaries() {
  if(!state.status || sessionMetadataLoading || Date.now()-sessionMetadataAt<5000)return;
  const membership=()=>JSON.stringify(sessionGroups().map(group=>[group.thread_id,group.members.map(row=>row.thread_id).sort()]));
  const previousMembers=membership();
  sessionMetadataLoading=true;
  try {
    if(sessionMetadataSupported!==false || Date.now()-sessionMetadataProbeAt>=30000) {
      sessionMetadataProbeAt=Date.now();
      try {
        const result=await api("/api/session-summaries",undefined,AbortSignal.timeout(5000));
        if(result.state==="available" && Array.isArray(result.sessions)) {
          sessionSummaries=await enrichSessionPurposes(result.sessions);sessionMetadataSupported=true;return;
        }
      } catch(_) {}
      sessionMetadataSupported=false;
    }
    // Older running backends can still show recent upload size and request time.
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
    sessionSummaries=await enrichSessionPurposes(results);
  } finally {
    sessionMetadataAt=Date.now();sessionMetadataLoading=false;
    const groups=sessionGroups();
    const group=selectedSessionGroup(groups)||groups.find(group=>group.members.some(row=>row.thread_id===state.detail?.thread_id))||
      groups.slice().sort((a,b)=>(b.updated_at||0)-(a.updated_at||0))[0];
    const selection=group?(group.thread_id===OTHER_SESSION_GROUP?OTHER_SESSION_GROUP:"thread:"+group.thread_id):"";
    if(selection!==state.filters.thread)await selectSession(selection);
    else {
      renderSessions();
      if(state.filters.thread&&previousMembers!==membership())state.listRevision=null;
    }
    renderList();if(state.detail)renderHeaders(state.detail);
  }
}
function hasTextSelectionWithin(node) {
  const selection=window.getSelection();
  return !!selection&&!selection.isCollapsed&&selection.containsNode(node,true);
}
function sessionConnections(groups,selected) {
  const rows=new Map(groups.filter(group=>group.thread_id!==OTHER_SESSION_GROUP).map(group=>[group.thread_id,group]));
  const focus=[...rows.values()].find(group=>selected==="thread:"+group.thread_id||group.members.some(row=>selected==="thread:"+row.thread_id));
  if(!focus)return [];
  // A normal fork also carries a parent ID; only confirmed subagents form sidebar links.
  const parents=new Map([...rows.values()].filter(group=>auxiliarySessionKind(group)==="subagent"&&group.parent_thread_id!==group.thread_id&&rows.has(group.parent_thread_id)).map(group=>[group.thread_id,group.parent_thread_id]));
  const valid=id=>{
    const visited=new Set();
    while(parents.has(id)) {if(visited.has(id))return false;visited.add(id);id=parents.get(id);}
    return true;
  };
  return [...parents].filter(([child,parent])=>(child===focus.thread_id||parent===focus.thread_id)&&valid(child)).map(([child,parent])=>({parent,child}));
}
function renderSessionConnections() {
  const list=$("session-list"),items=list.querySelector(".session-items");
  if(!items)return;
  items.querySelector(".session-connections")?.remove();
  const nodes=new Map(Array.from(items.querySelectorAll(".session-item"),item=>{
    item.classList.remove("session-linked");return [item.dataset.session,item];
  }));
  const connections=sessionConnections(sessionGroups(),state.filters.thread).filter(link=>nodes.has("thread:"+link.parent)&&nodes.has("thread:"+link.child));
  if(!connections.length)return;
  const bounds=items.getBoundingClientRect(),svg=document.createElementNS("http://www.w3.org/2000/svg","svg");
  svg.setAttribute("class","session-connections");svg.setAttribute("aria-hidden","true");
  svg.setAttribute("width","14");svg.setAttribute("height",String(bounds.height));
  const add=(tag,attributes)=>{
    const node=document.createElementNS(svg.namespaceURI,tag);
    for(const [name,value] of Object.entries(attributes))node.setAttribute(name,String(value));
    svg.append(node);return node;
  };
  const positions=new Map(),parents=[...new Set(connections.map(link=>link.parent))];
  // The incoming parent uses the outer rail; this session's children use the inner rail.
  parents.sort((a,b)=>Number("thread:"+a===state.filters.thread)-Number("thread:"+b===state.filters.thread));
  for(const id of new Set(connections.flatMap(link=>[link.parent,link.child]))) {
    const item=nodes.get("thread:"+id),heading=item.querySelector(".session-heading").getBoundingClientRect();
    positions.set(id,Math.round((heading.top-bounds.top+Math.min(heading.height,20)/2)*2)/2);
    item.classList.add("session-linked");
  }
  for(const {parent,child} of connections) {
    const rail=2+parents.indexOf(parent)*4;
    add("path",{class:"session-connection-path",d:`M 9 ${positions.get(parent)} H ${rail} V ${positions.get(child)} H 9`});
  }
  for(const [id,y] of positions) {
    const selected="thread:"+id===state.filters.thread,attributes={class:"session-connection-node"+(selected?" selected":"")};
    if(parents.includes(id))add("rect",{...attributes,x:7,y:y-2,width:4,height:4,rx:.7});
    else add("circle",{...attributes,cx:9,cy:y,r:2});
  }
  items.append(svg);
}
function renderSessions() {
  const list=$("session-list");
  if(!state.status)return;
  const groups=sessionGroups(),summaries=new Map(groups.map(group=>[group.thread_id,group])),connections=sessionConnections(groups,state.filters.thread);
  const ids=groups.map(group=>group.thread_id),selected=state.filters.thread;
  ids.sort((a,b)=>(summaries.get(b)?.updated_at||0)-(summaries.get(a)?.updated_at||0));
  const query=$("session-search").value.trim().toLowerCase();
  const signature=JSON.stringify([ids,selected,groups,sessionMetadataSupported,query]);
  if(signature===state.sessionVersion)return;
  state.sessionVersion=signature;
  const scrollTop=list.scrollTop, bounds=list.getBoundingClientRect();
  const items=Array.from(list.querySelectorAll(".session-item"));
  const visible=item=>{const rect=item.getBoundingClientRect();return rect.bottom>bounds.top&&rect.top<bounds.bottom;};
  // Keep a visible selected session in place when metadata changes the ordering.
  const selectedItem=items.find(item=>item.dataset.session===selected);
  const anchor=selectedItem&&visible(selectedItem)?selectedItem:items.find(visible);
  const anchorId=anchor?.dataset.session, anchorOffset=anchor?anchor.getBoundingClientRect().top-bounds.top:0;
  const focusedId=list.contains(document.activeElement)?document.activeElement.closest(".session-item")?.dataset.session:null;
  const fragment=el("div","session-items");
  for(const id of ids) {
    const otherGroup=id===OTHER_SESSION_GROUP;
    const value=otherGroup?OTHER_SESSION_GROUP:id?"thread:"+id:"unknown", data=summaries.get(id)||{};
    const title=data.title;
    if(query && !(title+"\n"+data.members.map(row=>row.thread_id+" "+(row.title||"")+" "+(auxiliaryLabels[auxiliarySessionKind(row)]?t(auxiliaryLabels[auxiliarySessionKind(row)]):"")).join("\n")).toLowerCase().includes(query))continue;
    const item=el("button","session-item"+(otherGroup?" other-session-group":"")+(value===selected?" selected":""));
    item.type="button";item.dataset.session=value;
    item.title=otherGroup?title:title+"\n"+(id||t("未识别会话"));
    for(const link of connections) {
      if(link.child===id)item.title+="\n"+t("父会话名称")+"："+summaries.get(link.parent).title;
      if(link.parent===id)item.title+="\n"+t("子任务")+"："+summaries.get(link.child).title;
    }
    item.setAttribute("aria-label",item.title);
    if(value===selected)item.setAttribute("aria-current","page");
    const heading=el("div","session-heading");heading.append(el("span","session-title",title));
    item.append(heading);
    const updated=Number.isFinite(data.updated_at)?new Date(data.updated_at*1000):null;
    const meta=el("div","session-meta"), when=el("span","session-updated",t("更新 ")+(updated?dateFormat.format(updated)+" "+clockFormat.format(updated):"—"));
    if(updated)when.title=timestampFormat.format(updated);
    if(!otherGroup) {
      if(id)item.append(el("code","session-id",id));
      const tokens=data.token_window, estimated=tokens?.estimated, used=tokens?.source==="codex_estimate"?tokens?.input_tokens:estimated?tokens?.used_tokens:tokens?.input_tokens;
      const available=tokens?.state==="available"&&Number.isFinite(used)&&used>=0;
      const tokenText=available?(estimated?"≈ ":"")+nf.format(used):"—";
      const usage=el("span","session-token","Token "+tokenText);
      usage.title=available?(estimated?t(tokens?.source==="codex_estimate"?"超限摘要前输入 Token（估算）":"压缩后上下文总量估算"):t("最近请求输入 Token"))+(tokens.recorded_at?" · "+timestampFormat.format(new Date(tokens.recorded_at*1000)):""):t("尚无可用的输入 Token 记录");
      meta.append(usage);
    }
    meta.append(when);item.append(meta);
    bindSelectableRow(item,()=>{
      if(hasTextSelectionWithin($("session-list")))window.getSelection().removeAllRanges();
      return selectSession(value);
    });fragment.append(item);
  }
  if(!fragment.childElementCount) {
    const empty=el("p","session-empty",query?t("未找到匹配的会话"):t("暂无会话"));
    empty.setAttribute("role","status");fragment.append(empty);
  }
  sessionConnectionObserver.disconnect();
  reconcileContent(list,fragment);
  renderSessionConnections();
  const sessionItems=list.querySelector(".session-items");
  if(sessionItems)sessionConnectionObserver.observe(sessionItems);
  const updatedItems=Array.from(list.querySelectorAll(".session-item"));
  const nextAnchor=updatedItems.find(item=>item.dataset.session===anchorId),nextFocus=updatedItems.find(item=>item.dataset.session===focusedId);
  $("session-metadata-note").hidden=sessionMetadataSupported!==false;
  if(nextFocus&&nextFocus!==document.activeElement)nextFocus.focus({preventScroll:true});
  list.scrollTop=scrollTop;
  if(nextAnchor)list.scrollTop+=nextAnchor.getBoundingClientRect().top-list.getBoundingClientRect().top-anchorOffset;
}
async function selectSession(value) {
  if(value===state.filters.thread)return;
  cancelTokenRead();tokenWindow=null;
  state.filters.thread=value;
  state.listController?.abort();state.listRequest++;
  state.detailRequest++;
  state.selected=null;state.detail=null;state.detailVersion="";state.text=null;
  requestBodyCache=null;requestBodyView=null;requestBodySequence++;clearJsonFocus();clearJsonDownload();
  state.packets=[];state.older=false;state.canLoadMore=false;state.listRevision=null;
  $("packet-detail").hidden=true;$("empty-state").hidden=false;
  $("packet-list").scrollTop=0;
  renderSessions();renderList();
  await loadPackets();
}
function message(text, error=false) { uiI18n.text($("notice-text"),text); $("notice").className = "notice"+(error?" error":""); $("notice").hidden = false; }
function fail(err) { message(err.message || String(err), true); }
async function api(path, data, signal) { const r = await fetch(path, {method:data === undefined ? "GET" : "POST",headers:data === undefined ? {} : {"Content-Type":"application/json"},body:data === undefined ? undefined : JSON.stringify(data),signal}); const value = await r.json(); if (!r.ok) throw Object.assign(Error(value.error || ("HTTP "+r.status)),{status:r.status}); return value; }
function action(id, fn) { $(id).addEventListener("click", () => Promise.resolve().then(fn).catch(fail)); }
action("notice-dismiss",()=>{$("notice").hidden=true;});
uiI18n.floatingFeedback();
function tokenVersion(packet) { return [packet.state,packet.status,packet.duration_ms,packet.method,packet.path,packet.is_compaction,packet.request_kind,packet.overflow_compaction_calls,packet.overflow_cache_hit,packet.request_token_estimate?.state,packet.request_token_estimate?.input_tokens].join(":"); }
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
  if(workspaceView!=="records")return;
  const id=state.selected;
  if(!id || document.hidden || tokenLoading || (tokenWindow && !["loading","pending"].includes(tokenWindow.state)))return;
  clearTimeout(tokenTimer);tokenTimer=null;
  const request=++tokenRequest;tokenLoading=true;
  const controller=new AbortController();tokenController=controller;
  const timeout=setTimeout(()=>controller.abort(),6000);
  try {
    const data=hasModelTokenUsage(state.detail)?await api("/api/packets/"+id+"/token-window",undefined,controller.signal):{state:"unavailable"};
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
        const delay=tokenWindow?.state==="loading"?50:tokenWindow?.state==="pending"?(++tokenPendingRetries<=30?300:1000):null;
        if(delay!==null)tokenTimer=setTimeout(loadTokenWindow,delay);
      }
    }
  }
}
function panel(title) { const n = el("section","panel"); n.dataset.renderKey="panel:"+title;n.append(el("h3","",title)); return n; }
function table(headings, rows) { const wrap = el("div","table-scroll"), t = el("table"), head=el("thead"), tr=el("tr"); for (const h of headings) tr.append(el("th","",h)); head.append(tr); t.append(head); const body=el("tbody"); for (const cells of rows) { const row=el("tr"); for (const cell of cells) {const td=el("td"); if (cell instanceof Node) td.append(cell); else td.textContent = cell; row.append(td);} body.append(row); } t.append(body); wrap.append(t); return wrap; }
function link(text, fn) { const n=el("button","link-button",text); n.onclick=()=>Promise.resolve().then(fn).catch(fail); return n; }
function fact(n,label,value) { const r=el("div","fact-row"); r.dataset.renderKey="fact:"+label;r.append(el("span","",label),el("span","",value)); n.append(r); return r; }
function showTab(tab) {
  const basic=!!state.detail?.details_evicted;
  if(basic&&tab==="tree")tab="headers";
  state.tab=tab;
  for(const b of document.querySelectorAll("[data-tab]")) {
    b.hidden=basic&&b.dataset.tab==="tree";
    b.classList.toggle("active",b.dataset.tab===tab);
  }
  for(const t of ["headers","overview","tree","diff"])$("tab-"+t).hidden=t!==tab;
}
function packetStatus(p) {
  if(p.state==="relay_error" || p.status>=400)return "failed";
  if(["client_disconnected","interrupted"].includes(p.state))return "interrupted";
  if(p.state==="streaming")return "streaming";
  if(p.state==="complete")return "complete";
  return "waiting";
}
function capacityRetryCount(p) {
  return Number.isInteger(p.capacity_retry_count)&&p.capacity_retry_count>0?p.capacity_retry_count:0;
}
function packetProtection(p) {
  return {
    predictive:p.usage_override_applied===true,
    overflow:p.overflow_compaction_calls>0 || p.overflow_cache_hit===true ||
      typeof p.relay_error==="string" && p.relay_error.startsWith("overflow_"),
    retry:p.native_retry_advised===true||capacityRetryCount(p)>0
  };
}
function overflowApplied(p) {return p.overflow_compaction_calls>0||p.overflow_cache_hit===true;}
function requestTokenUsage(p,data=null) {
  if(overflowApplied(p)) {
    const estimate=p.request_token_estimate??(data?.source==="codex_estimate"?data:null);
    if(!estimate)return data&&data.state!=="available"?data:null;
    return estimate.source==="codex_estimate"&&estimate.estimated===true&&estimate.state==="available"&&Number.isSafeInteger(estimate.input_tokens)&&estimate.input_tokens>=0?
      estimate:{state:"unavailable",source:"codex_estimate",estimated:true};
  }
  const real=p.upstream_usage?.input_tokens;
  if(Number.isSafeInteger(real)&&real>=0)return {state:"available",input_tokens:real};
  return p.usage_override_applied?{state:"unavailable"}:data;
}
function listTokens(p) {
  if(!hasModelTokenUsage(p))return {state:"unavailable"};
  const direct=requestTokenUsage(p);if(direct)return direct;
  const sorted=tokenSortCache?.entries.get(p.id);
  if(sorted && sorted.version===tokenVersion(p) && (sorted.expires>Date.now() || p.state==="complete" && sortInputTokens(sorted.data)!==null))return requestTokenUsage(p,sorted.data);
  const cached=cachedTokenWindow(p);
  if(cached)return requestTokenUsage(p,cached);
  const entry=listTokenCache.get(p.id);
  if(entry && entry.version===tokenVersion(p) && entry.expires>Date.now())return requestTokenUsage(p,entry.data);
  if(p.method!=="POST" || p.is_compaction || p.status!==200 || !["streaming","complete"].includes(p.state))return {state:p.state==="captured"?"pending":"unavailable"};
  return null;
}
function renderListTokens(p) {
  const cell=listTokenCells.get(p.id);if(!cell)return;
  const data=listTokens(p), value=sortInputTokens(data), available=value!==null;
  setText(cell,available?(data.estimated?"≈ ":"")+nf.format(value):"—");
  cell.title=available?t(data.estimated?"超限摘要前输入 Token（估算）":"本次请求的真实输入 Token"):!data || ["loading","pending"].includes(data.state)?t("等待输入 Token 记录"):t("暂无对应的输入 Token 记录");
}
function tokenDataLifetime(data) {
  return data?.state==="available"?300000:["loading","pending"].includes(data?.state)?1500:30000;
}
function cacheListTokens(p,data) {
  if(!p)return;
  listTokenCache.delete(p.id);
  const lifetime=tokenDataLifetime(data);
  listTokenCache.set(p.id,{version:tokenVersion(p),expires:Date.now()+lifetime,data});
  const sorted=tokenSortCache?.entries.get(p.id);
  if(sorted && sorted.version===tokenVersion(p)) {
    if(sortInputTokens(sorted.data)!==sortInputTokens(data)) {
      tokenSortCache.dirty=true;tokenSortCache.orders.clear();
      if(tokenSortTimer===null)tokenSortTimer=setTimeout(()=>{tokenSortTimer=null;refreshTokenSort();},80);
    }
    sorted.data=data;sorted.expires=Date.now()+lifetime;
  }
  while(listTokenCache.size>4096)listTokenCache.delete(listTokenCache.keys().next().value);
  const current=state.packets.find(packet=>packet.id===p.id);
  if(current)renderListTokens(current);
}
function readListTokens(p,signal) {
  signal?.throwIfAborted();
  if(!hasModelTokenUsage(p))return Promise.resolve({state:"unavailable"});
  const version=tokenVersion(p);let request=listTokenRequests.get(p.id);
  if(!request || request.version!==version || request.controller.signal.aborted) {
    request?.controller.abort();
    const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),6000);
    request={version,controller,users:0,settled:false,promise:null};
    const current=request;listTokenRequests.set(p.id,current);
    current.promise=api("/api/packets/"+p.id+"/token-window",undefined,controller.signal)
      .then(data=>{if(listTokenRequests.get(p.id)===current){cacheTokenWindow(p,data);cacheListTokens(p,data);}return data;})
      .catch(error=>{if(controller.signal.aborted)throw error;const data={state:"unavailable"};if(listTokenRequests.get(p.id)===current)cacheListTokens(p,data);return data;})
      .finally(()=>{current.settled=true;clearTimeout(timeout);if(listTokenRequests.get(p.id)===current)listTokenRequests.delete(p.id);queueMicrotask(loadListTokens);});
  }
  const current=request;current.users++;
  return new Promise((resolve,reject)=>{
    let done=false;
    const finish=(callback,value)=>{
      if(done)return;done=true;signal?.removeEventListener("abort",cancel);
      current.users--;if(!current.users && !current.settled)current.controller.abort();
      callback(value);
    };
    const cancel=()=>finish(reject,signal.reason);
    signal?.addEventListener("abort",cancel,{once:true});
    current.promise.then(data=>finish(resolve,data),error=>finish(reject,error));
  });
}
function loadListTokens() {
  if(document.hidden||workspaceView!=="records")return;
  for(const id of visibleTokenRows) {
    const p=state.packets.find(packet=>packet.id===id);if(!p)continue;
    renderListTokens(p);
    if(listTokens(p) || listTokenRequests.has(id))continue;
    if(listTokenRequests.size>=3)break;
    readListTokens(p).catch(()=>cacheListTokens(p,{state:"unavailable"}));
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
  return data?.state==="available" && (!data.estimated||data.source==="codex_estimate") && Number.isSafeInteger(data.input_tokens) && data.input_tokens>=0?data.input_tokens:null;
}
function sortedTokenPackets(cache,direction) {
  if(cache.orders.has(direction))return cache.orders.get(direction);
  const counts=new Map(cache.records.map(p=>[p.id,sortInputTokens(listTokens(p))]));
  const sorted=cache.records.slice().sort((a,b)=>{
    const x=counts.get(a.id), y=counts.get(b.id);
    if(x===null && y!==null)return 1;
    if(y===null && x!==null)return -1;
    return (x!==null && y!==null?(x-y)*(direction==="asc"?1:-1):0) || b.created-a.created || b.id.localeCompare(a.id);
  });
  cache.orders.set(direction,sorted);return sorted;
}
function refreshTokenSort() {
  const cache=tokenSortCache;
  if(state.sort!=="input_tokens" || !cache?.dirty || state.listController)return;
  cache.dirty=false;
  state.packets=sortedTokenPackets(cache,state.direction).slice(0,state.packets.length);
  state.canLoadMore=state.packets.length<cache.records.length;
  renderList();
}
function renderTokenSortLoading() {
  if(state.sort!=="input_tokens")return;
  $("list-loading").hidden=!state.listController && !tokenSortCache?.loading;
  $("list-loading").textContent=t("正在读取输入 Token…");
}
function tokenSortExpiry(cache) {
  cache.expires=Infinity;
  if(!cache.loading)for(const entry of cache.entries.values())cache.expires=Math.min(cache.expires,entry.expires);
}
function hydrateTokenSort(cache) {
  if(cache!==tokenSortCache || state.sort!=="input_tokens" || cache.loading && !cache.controller.signal.aborted)return;
  const records=cache.records.filter(p=>cache.entries.get(p.id).expires<=Date.now());
  if(!records.length){tokenSortExpiry(cache);return;}
  const controller=new AbortController(),signal=controller.signal;
  cache.controller=controller;cache.loading=true;tokenSortExpiry(cache);renderTokenSortLoading();
  let cursor=0;
  const read=async()=>{
    while(cursor<records.length) {
      signal.throwIfAborted();const p=records[cursor++];
      if(cache.entries.get(p.id).expires>Date.now())continue;
      let data;
      try {
        const deadline=Date.now()+3000;
        do {
          data=await readListTokens(p,signal);signal.throwIfAborted();
          if(data.state==="loading")await new Promise(resolve=>setTimeout(resolve,75));
        } while(data.state==="loading" && Date.now()<deadline);
      } catch(error) {signal.throwIfAborted();data={state:"unavailable"};cacheListTokens(p,data);}
    }
  };
  Promise.all(Array.from({length:Math.min(3,records.length)},read))
    .catch(error=>{if(!signal.aborted)fail(error);})
    .finally(()=>{
      if(cache!==tokenSortCache || cache.controller!==controller)return;
      cache.loading=false;cache.controller=null;tokenSortExpiry(cache);refreshTokenSort();renderTokenSortLoading();
    });
}
async function tokenSortedPackets(query,revision,signal) {
  const filters=new URLSearchParams(query);
  for(const key of ["limit","sort","direction","after"])filters.delete(key);
  const key=filters.toString(), previous=tokenSortCache?.key===key?tokenSortCache:null;
  let cache=previous;
  if(!cache || (!query.has("after") && cache.revision!==revision)) {
    const records=[],scan=new URLSearchParams(filters);scan.set("limit","300");scan.set("sort","created");scan.set("direction","desc");
    for(;;) {
      const page=await sessionPacketPage(scan,signal);
      signal.throwIfAborted();records.push(...page);
      if(page.length<300)break;
      scan.set("after",page.at(-1).id);
    }
    const entries=new Map();
    for(const p of records) {
      const version=tokenVersion(p),data=listTokens(p);
      const reusable=[previous?.entries.get(p.id),listTokenCache.get(p.id),tokenCache.get(p.id)].find(entry=>entry?.version===version && entry.data===data);
      const native=hasModelTokenUsage(p) && p.status===200 && ["streaming","complete"].includes(p.state) && (!p.usage_override_applied||overflowApplied(p)) && requestTokenUsage(p)===null;
      entries.set(p.id,{version,data,expires:!native?Infinity:reusable?.expires??(data?Date.now()+tokenDataLifetime(data):0)});
    }
    tokenSortCache?.controller?.abort();
    cache={key,revision,records,entries,dirty:false,loading:false,controller:null,orders:new Map(),expires:Infinity};
    tokenSortCache=cache;
  }
  signal.throwIfAborted();hydrateTokenSort(cache);
  const sorted=sortedTokenPackets(cache,query.get("direction"));
  cache.dirty=false;
  const start=query.has("after")?sorted.findIndex(p=>p.id===query.get("after"))+1:0;
  return sorted.slice(start,start+Number(query.get("limit")));
}
function renderListEmpty() {
  const empty=$("list-empty");
  empty.hidden=!state.status||!!state.listController||state.listRevision===null||state.packets.length>0;
  empty.textContent=Object.values(state.filters).some(Boolean)?t("没有符合筛选条件的请求。"):t("暂无请求记录。");
}
function renderList() {
  if(!state.status)return;
  renderListEmpty();
  const filtered=Object.values(state.filters).some(Boolean);
  const sessionCount=selectedSessionGroup()?.packet_count;
  const total=!filtered?state.status.storage.count:!state.filters.kind && !state.filters.status && state.filters.thread && Number.isFinite(sessionCount)?sessionCount:null;
  setText($("record-count"),total===null?nf.format(state.packets.length)+(state.canLoadMore?"+":"")+t(" 条"):nf.format(total)+t(" 条"));
  const signature=JSON.stringify([state.selected,state.status.budget_bytes,state.older,state.canLoadMore,state.sort,state.direction,state.filters,state.packets.map(p=>[p.id,p.created,p.method,p.is_compaction,requestKind(p),p.state,p.status,p.duration_ms,p.decoded_bytes,p.outbound_wire_bytes,p.outbound_content_encoding,p.thread_id,p.overflow_compaction_calls,p.overflow_cache_hit,p.overflow_original_wire_bytes,p.usage_override_applied,p.capacity_retry_count,p.native_retry_advised,p.native_retry_delay_ms,p.relay_error,p.upstream_usage?.input_tokens])]);
  if(signature===state.listVersion)return;
  state.listVersion=signature;
  const root=$("packet-rows"), fragment=document.createDocumentFragment(); renderSort();
  listTokenObserver.disconnect();visibleTokenRows.clear();listTokenCells.clear();
  for (const p of state.packets) {
    const flags=packetProtection(p),protection=[];
    if(flags.predictive)protection.push(t("预测保护"));
    if(flags.overflow)protection.push(t("超限保护"));
    if(flags.retry)protection.push(t("重试保护"));
    if(p.overflow_compaction_calls>0)protection.push(t("生成摘要 ")+nf.format(p.overflow_compaction_calls)+t(" 次"));
    if(p.overflow_cache_hit===true)protection.push(t("复用摘要"));
    const row=el("tr",["request-"+packetStatus(p),state.selected===p.id?"selected":""].filter(Boolean).join(" ")), date=new Date(p.created*1000);
    const time=el("button","packet-open");
    time.append(el("span","packet-date",dateFormat.format(date)),el("span","packet-clock",clockFormat.format(date)));
    time.title=timestampFormat.format(date);
    time.setAttribute("aria-label",t("查看 ")+time.title+t(" 的请求"));
    const type=requestKind(p);
    const original=originalBytes(p), upload=compressedBytes(p);
    const before=el("span","packet-size",compact(original));
    before.title=original===null?t("原始请求大小未记录"):t("原始请求：")+bytes(original);
    const after=el("span","packet-size",compact(upload));
    after.title=upload===null?t("转发请求大小未记录"):t("转发请求：")+bytes(upload);
    row.dataset.packetId=p.id;row.dataset.session=p.thread_id||"";row.title=(states[p.state]||p.state)+(p.status?" · HTTP "+p.status:"")+"\n"+(p.thread_id?"session_id: "+p.thread_id:t("会话未识别"));
    if(flags.retry)row.title+="\n"+(p.native_retry_advised===true?t("已向 Codex 发送重试提示"):t("自动重试：{count} 次").replace("{count}",nf.format(capacityRetryCount(p))));
    if(protection.length)after.title+=t("\n已触发请求保护：")+protection.join(" · ");
    for (const [index,value] of [time,type,before].entries()) {
      const td=el("td",index===1?"packet-kind":"");
      if(value instanceof Node)td.append(value);else td.textContent=value;
      row.append(td);
    }
    const compressed=el("td",["packet-compressed",flags.predictive?"predictive-protected":"",flags.overflow?"overflow-protected":"",flags.retry?"retry-protected":""].filter(Boolean).join(" "));
    compressed.title=after.title;compressed.append(after);row.append(compressed);
    const tokens=el("td","packet-tokens");listTokenCells.set(p.id,tokens);renderListTokens(p);row.append(tokens);
    bindSelectableRow(row,current=>selectPacket(current.dataset.packetId));fragment.append(row);
  }
  reconcileContent(root,fragment);
  for(const row of root.children) {
    listTokenCells.set(row.dataset.packetId,row.querySelector(".packet-tokens"));
    listTokenObserver.observe(row);
  }
}
function loadMorePackets() {
  if(!state.canLoadMore || state.listController || document.hidden)return;
  const list=$("packet-list");
  if(list.scrollHeight-list.scrollTop-list.clientHeight>180)return;
  return loadPackets(true).catch(fail);
}
async function loadPackets(append=false,background=false) {
  if(workspaceView!=="records")return;
  // The middle pane must always belong to a selected session, including at startup.
  if(!state.filters.thread||!selectedSessionGroup())return;
  if((append || background) && state.listController)return;
  state.listController?.abort();
  if(state.sort!=="input_tokens")tokenSortCache?.controller?.abort();
  const controller=new AbortController();state.listController=controller;
  const requestId=++state.listRequest;
  const list=$("packet-list");
  if(!append && !background)list.scrollTop=0;
  list.setAttribute("aria-busy","true");
  renderListEmpty();
  $("list-loading").hidden=!append && state.sort!=="input_tokens";
  $("list-loading").textContent=t(state.sort==="input_tokens"?"正在读取输入 Token…":"正在加载更多记录…");
  let loaded=false;
  const revision=state.status?.storage.revision;
  const q=new URLSearchParams({limit:"100",sort:state.sort,direction:state.direction});
  if(state.filters.kind)q.set("kind",state.filters.kind);
  if(state.filters.status)q.set("status",state.filters.status);
  if(state.filters.thread) {
    const group=selectedSessionGroup();q.set("thread_group",group?.thread_id||state.filters.thread);
    for(const row of group?.members||[])q.append("thread",row.thread_id);
  }
  if (append && state.packets.length) q.set("after",state.packets.at(-1).id);
  try {
    const values=state.sort==="input_tokens"?await tokenSortedPackets(q,revision,controller.signal):state.sort==="compressed_bytes"?await compressedSortedPackets(q,revision,controller.signal):await sessionPacketPage(q,controller.signal);
    if(requestId!==state.listRequest)return;
    state.listRevision=revision;
    state.canLoadMore=values.length===100;
    if (append) {state.packets.push(...values); state.older=true;} else {state.packets=values;state.older=false;}
    renderList();
    loaded=true;
    if (!state.selected && state.packets.length) await selectPacket(state.packets[0].id,false,requestId);
    else if (state.selected) { const p=state.packets.find(p=>p.id===state.selected); if(p) {const ver=[p.state,p.status,p.duration_ms,p.outbound_wire_bytes,p.outbound_content_encoding,p.relay_content_encoding,p.relay_zstd_level,p.transport_content_encoding,p.transport_zstd_level,p.usage_override_request_bytes,p.usage_override_applied,p.capacity_retry_count,p.native_retry_advised,p.native_retry_delay_ms,p.capacity_retry_diagnostic?.updated,p.details_evicted].join(":");if(ver!==state.detailVersion) await selectPacket(state.selected,true);} else await selectPacket(state.selected,true); }
  } catch(e) {if(e.name!=="AbortError")throw e;}
  finally {
    if(state.listController===controller) {
      state.listController=null;
      list.setAttribute("aria-busy","false");$("list-loading").hidden=true;refreshTokenSort();renderTokenSortLoading();
      renderListEmpty();
      if(loaded)requestAnimationFrame(loadMorePackets);
    }
  }
}
async function refreshStatus(fill=false) {
  const revision=settingsRevision, connectionRevision=linkRevision, modeRevision=compressionRevision, quittingRevision=exitRevision, status=await api("/api/status");
  if(settingsSaving||compressionSaving||exitSaving||revision!==settingsRevision||connectionRevision!==linkRevision||modeRevision!==compressionRevision||quittingRevision!==exitRevision)return;
  state.status=status;
  if(!settingsEditing&&["zh","en"].includes(status.ui_language)&&uiI18n.language!==status.ui_language)
    await setInterfaceLanguage(status.ui_language);
  renderStatus(fill);
}
function renderConnectionActions() {
  const s=state.status;if(!s)return;
  renderCompressionMode();
}
function renderStatus(fill=false) {
  const s=state.status;if(!s)return;
  $("desktop-build").hidden=!s.ui_live_reload;
  $("ui-refresh").hidden=!s.ui_live_reload;
  renderConnectionActions();
  $("empty-connect").hidden=!!s.profile.active;
  renderStorageInfo();
  if(fill) fillSettings();else {fillEntryPort();renderCompressionMode();renderLanguageSelection();}
  renderSessions();renderList();
  renderProtectionPanelState();
  renderExitRequest();
  renderConnectPrompt();
  if(s.connection_reload_required&&!connectionRestartNotified&&!linkSaving&&!connectSaving&&!compressionSaving&&!exitSaving&&!document.querySelector("dialog[open]")) {
    connectionRestartNotified=true;
    showConfigurationFeedback([t("Codex 接入与原生压缩")]);
  }
}
function storageLimitSettings(status) {
  const precise=Number.isSafeInteger(status.max_storage_bytes),legacyUnit=1048576;
  const size=precise?status.max_storage_bytes:status.storage?.max_bytes??status.max_storage_mb*legacyUnit;
  return {value:size/1000000,min:(status.storage_limit_min_bytes??16*legacyUnit)/1000000,
    max:(status.storage_limit_max_bytes??8192*legacyUnit)/1000000,step:precise?"any":String(legacyUnit/1000000),
    key:precise?"max_storage_bytes":"max_storage_mb",toBackend:value=>Math.round(Number(value)*1000000/(precise?1:legacyUnit))};
}
function fillSettings() {
  const s=state.status,p=s.profile;
  const storage=storageLimitSettings(s),storageInput=$("storage-limit");
  storageInput.min=storage.min;storageInput.max=storage.max;storageInput.step=storage.step;
  $("packet-limit").max=s.packet_limit_max??20000;
  storageInput.title=storage.key==="max_storage_bytes"?"":t("当前后台的存储上限步长为 1.048576 MB；更新后台后支持精确设置。");
  fillEntryPort();
  if(!settingsEditing) {
    storageInput.value=storage.value;$("packet-limit").value=s.max_packets;
    $("start-on-login").checked=!!s.start_on_login;
    $("capacity-retry").checked=!!s.capacity_retry_enabled;
    $("capacity-retry-count").value=s.capacity_retry_max_retries??15;
    $("capacity-retry-delay").value=s.capacity_retry_delay_seconds??30;
    $("sharing-port").value=s.sharing_port??47846;
    $("exit-port").value=s.egress_port??47847;
  }
  for(const id of ["storage-limit","packet-limit"])$(id).disabled=settingsSaving;
  $("sharing-settings").hidden=!s.sharing_settings_supported;
  for(const id of ["sharing-port","exit-port"])$(id).disabled=settingsSaving||!s.sharing_settings_supported;
  $("start-on-login").disabled=settingsSaving||!s.start_on_login_supported;
  $("capacity-retry").disabled=settingsSaving||!s.capacity_retry_supported;
  $("capacity-retry-count").disabled=settingsSaving||s.capacity_retry_mode!=="native"||!Number.isInteger(s.capacity_retry_max_retries);
  $("capacity-retry-delay").disabled=settingsSaving||s.capacity_retry_mode!=="native"||!Number.isInteger(s.capacity_retry_delay_seconds);
  fillImageAnalysisMode();
  $("startup-hint").textContent=t(s.start_on_login_supported?(s.startup_state_known===false?"无法读取开机启动状态，请重新保存设置。":"登录 Windows 后自动启动。"):"开机启动仅支持已安装的 Windows 版本。");
  $("runtime-settings-message").hidden=!!s.capacity_retry_supported;
  $("runtime-settings-message").textContent=s.capacity_retry_supported?"":t("当前中转暂不支持此功能。");
  renderConnectionActions();
  renderCompressionMode();renderLanguageSelection();fillZstdLevel();fillImageDirectory();fillPacketProtection();syncContextDefaults();
  updateSettingsSaveState(false);
}
function imageAnalysisMode(status=state.status) {
  return ["native","disabled","optimized"].includes(status?.image_analysis_mode)?status.image_analysis_mode:
    status?.image_isolation_enabled?"optimized":"native";
}
function selectedImageAnalysisMode() {
  return $("image-analysis-mode").querySelector("input:checked")?.value??imageAnalysisMode();
}
function fillImageAnalysisMode() {
  const s=state.status,hasModes=["native","disabled","optimized"].includes(s.image_analysis_mode);
  for(const input of $("image-analysis-mode").querySelectorAll("input")) {
    if(!settingsEditing)input.checked=input.value===imageAnalysisMode(s);
    const unavailable=input.value==="disabled"&&!hasModes;
    input.disabled=settingsSaving||!(s.image_isolation_supported||hasModes)||unavailable;
    input.closest("label").title=unavailable?t("当前后台暂不支持禁用模式，升级后可用。"):"";
  }
  const descriptions={native:"卸载优化工具，恢复原生图片解析",disabled:"关闭原生与优化图片工具",
    optimized:"主请求仅保留图片路径"};
  $("image-isolation-hint").textContent=t(s.image_isolation_supported||hasModes?
    descriptions[selectedImageAnalysisMode()]:"当前中转暂不支持此功能。");
}
function fillZstdLevel() {
  const s=state.status,input=$("zstd-level"),supported=Number.isInteger(s.zstd_level);
  $("auto-zstd-level-hint").textContent=Number.isInteger(s.auto_image_zstd_level)?t("对话按大小选档，图片请求使用{level}级；开启后禁用手动选择。").replace("{level}",s.auto_image_zstd_level):t("根据请求大小自动选择合适的压缩等级，开启后禁用手动选择。");
  const selected=settingsEditing?Number(input.value):(s.zstd_level??3);
  const automatic=$("packet-size-protection").checked&&$("auto-zstd-level").checked;
  $("compression-settings").classList.toggle("is-automatic",automatic);
  $("compression-settings").setAttribute("aria-disabled",String(automatic));
  input.value=String(selected);
  input.disabled=settingsSaving||!supported||automatic;
  $("zstd-level-open").disabled=input.disabled;
  $("zstd-level-value").textContent=t("{level}级").replace("{level}",selected);
  fillImageZstdLevel(automatic);
  if($("zstd-level-dialog").open) {
    if(zstdLevelPickerTarget().input.disabled)closeZstdLevelPicker();else renderZstdLevelRows();
  }
  const message="";
  $("zstd-level-message").textContent=message;$("zstd-level-message").hidden=!message;
}
function fillImageZstdLevel(automatic) {
  const s=state.status,input=$("image-zstd-level");
  const selected=settingsEditing&&input.value?Number(input.value):(s.image_zstd_level??6);
  input.value=String(selected);
  input.disabled=settingsSaving||!Number.isInteger(s.image_zstd_level)||automatic;
  $("image-zstd-level-open").disabled=input.disabled;
  $("image-zstd-level-value").textContent=t("{level}级").replace("{level}",automatic?(s.auto_image_zstd_level??6):selected);
}
function zstdLevelPickerTarget() {
  const image=$("zstd-level-dialog").dataset.target==="image";
  return {image,input:$(image?"image-zstd-level":"zstd-level"),trigger:$(image?"image-zstd-level-open":"zstd-level-open"),
    estimates:image?imageZstdLevelEstimates:zstdLevelEstimates};
}
function renderZstdLevelRows() {
  const {image,input,estimates}=zstdLevelPickerTarget(),selected=Number(input.value);
  $("zstd-level-title").textContent=t(image?"选择图片压缩等级":"选择对话压缩等级");
  $("zstd-level-note").textContent=t(image?
    "基于7种生成测试图、每级3轮测试估算，大小相对原始未压缩包，耗时为单次压缩中位数。":
    "基于120条请求、每级3轮测试估算，大小相对原始未压缩包，耗时为单次压缩中位数。");
  const rankedValues=[1,2,3].map(column=>
    [...new Set(estimates.map(row=>Number(row[column].toFixed(column===3?3:2))))].sort((a,b)=>a-b));
  const estimateCell=(column,value,text)=>{
    const values=rankedValues[column],rank=values.indexOf(Number(value.toFixed(column===2?3:2)));
    // Spread displayed ranks across the palette; equal displayed values share a color.
    const ratio=values.length===1?0.5:rank/(values.length-1);
    const quality=column===2?1-ratio:ratio;
    const cell=el("td",`num zstd-quality zstd-quality-${Math.round(quality*21)}`,text);
    return cell;
  };
  $("zstd-level-options").replaceChildren(...estimates.map(([level,smallSize,largeSize,time])=>{
    const current=level===selected,row=el("tr",current?"selected":"");row.dataset.level=String(level);
    const cell=el("td"),button=el("button","zstd-level-option");button.type="button";
    button.setAttribute("aria-pressed",String(current));button.tabIndex=current?0:-1;
    const mark=el("span","zstd-level-check",current?"✓":"");mark.setAttribute("aria-hidden","true");
    button.append(mark,el("span","",t("{level}级").replace("{level}",level)));
    cell.append(button);row.append(cell,estimateCell(0,smallSize,"-"+smallSize.toFixed(2)+"%"),estimateCell(1,largeSize,"-"+largeSize.toFixed(2)+"%"),estimateCell(2,time,time.toFixed(3)+" ms"));
    return row;
  }));
}
function openZstdLevelPicker(target="conversation") {
  const dialog=$("zstd-level-dialog");
  if(!$("settings-dialog").open||dialog.open)return;
  dialog.dataset.target=target==="image"?"image":"conversation";
  const {trigger}=zstdLevelPickerTarget();if(trigger.disabled)return;
  renderZstdLevelRows();dialog.showModal();trigger.setAttribute("aria-expanded","true");
  const selected=dialog.querySelector(".zstd-level-option[aria-pressed='true']");
  selected?.focus({preventScroll:true});selected?.scrollIntoView({block:"center"});
}
function closeZstdLevelPicker(restoreFocus=true) {
  if($("zstd-level-dialog").open)$("zstd-level-dialog").close();
  const {trigger}=zstdLevelPickerTarget();trigger.setAttribute("aria-expanded","false");
  if(restoreFocus&&$("settings-dialog").open&&!trigger.disabled)trigger.focus({preventScroll:true});
}
function selectZstdLevel(value) {
  const {input}=zstdLevelPickerTarget(),level=Number(value);
  fillZstdLevel();
  if(input.disabled||!Number.isInteger(level)||level<1||level>22)return;
  input.value=String(level);input.dispatchEvent(new Event("change",{bubbles:true}));
  closeZstdLevelPicker();
}
function navigateZstdLevels(event) {
  if(!["ArrowUp","ArrowDown","Home","End"].includes(event.key))return;
  const options=[...$("zstd-level-options").querySelectorAll(".zstd-level-option")],index=options.indexOf(document.activeElement);
  if(index<0)return;
  event.preventDefault();
  const next=event.key==="Home"?0:event.key==="End"?options.length-1:Math.max(0,Math.min(options.length-1,index+(event.key==="ArrowDown"?1:-1)));
  options.forEach((option,i)=>option.tabIndex=i===next?0:-1);
  options[next].focus({preventScroll:true});options[next].scrollIntoView({block:"nearest"});
}
function renderLanguageSelection() {
  for(const button of document.querySelectorAll("#language-toggle button")) {
    button.setAttribute("aria-pressed",String(button.dataset.language===(settingsEditing?settingsLanguage:uiI18n.language)));
    button.disabled=settingsSaving;
  }
}
function renderCompressionMode() {
  const s=state.status;if(!s)return;
  const connected=!!s.profile?.active;
  const unknown=!!s.profile?.config_error;
  for(const button of document.querySelectorAll("#request-compression button")) {
    const disconnect=button.dataset.compression==="disconnected";
    button.setAttribute("aria-pressed",String(!unknown&&(disconnect?!connected:connected)));
    button.disabled=compressionSaving||settingsSaving||exitSaving||connectSaving||connectChecking||!!linkSaving||unknown||(disconnect?!connected||!s.profile.can_restore:connected);
  }
  $("request-compression").setAttribute("aria-busy",String(compressionSaving));
  setText($("compression-hint"),t(connected?"由本软件管理 zstd 压缩，保留完整内容。":"直连原上游，由 Codex 管理原生压缩。"));
  $("request-compression").title=$("compression-hint").textContent;
  $("compression-confirm-dialog").setAttribute("aria-busy",String(compressionSaving));
  for(const id of ["compression-confirm-cancel","compression-confirm-apply"])$(id).disabled=compressionSaving;
  setText($("compression-confirm-apply"),t(compressionTarget==="disconnected"?(compressionSaving?"正在取消接入…":"确认断开"):(compressionSaving?"正在切换…":"确认断开")));
  if(!connected&&!compressionSaving&&$("compression-confirm-dialog").open)cancelCompressionChange();
}
function showConfigurationFeedback(items,{summary="",reloadRequired=true}={}) {
  uiI18n.text($("settings-restart-title"),reloadRequired?"重启 ChatGPT App 后生效":"操作已完成");
  uiI18n.text($("settings-restart-description"),reloadRequired?"设置已保存。以下配置仍需重启 ChatGPT App 后生效：":"配置无需更改，无需重启。");
  uiI18n.text($("settings-restart-summary"),summary);$("settings-restart-summary").hidden=!summary;
  $("settings-restart-items").replaceChildren(...items.map(label=>el("li","",label)));
  $("settings-restart-items").hidden=!items.length;
  if(!$("settings-restart-dialog").open)$("settings-restart-dialog").showModal();
}
function showConnectionFeedback(which,result={}) {
  connectionRestartNotified=true;
  const reloadRequired=result.reload_required??!!state.status?.connection_reload_required;
  const items=reloadRequired?[t("Codex 接入与原生压缩")]:[];
  if(result.loopback_proxy?.changed)items.push(t("本机代理例外"));
  showConfigurationFeedback(items,{reloadRequired,
    summary:()=>(which==="activate"?t("已接入。"):t("已恢复原配置。"))+(result.loopback_proxy?.changed?t("已修复本机代理例外。"):"")});
}
function requestCompressionChange(mode) {
  if(mode==="connected")return requestConnection("header");
  const s=state.status,dialog=$("compression-confirm-dialog");
  if(!s||!s.profile?.active||compressionSaving||settingsSaving||settingsOpening||settingsEditing||linkSaving||exitSaving||connectSaving||$("connect-confirm-dialog").open||$("exit-confirm-dialog").open||dialog.open)return;
  const disconnect=mode==="disconnected";
  if(!disconnect||!s.profile.can_restore||s.profile.config_error)return;
  compressionTarget=mode;
  $("compression-confirm-title").textContent=t("取消接入");
  $("compression-confirm-description").textContent=t("恢复原上游并开启 Codex 原生压缩？");
  $("compression-confirm-hint").textContent=t("重启 ChatGPT App 生效。");
  renderCompressionMode();
  $("compression-confirm-message").hidden=true;
  dialog.showModal();
}
function cancelCompressionChange() {
  if(compressionSaving)return;
  $("compression-confirm-dialog").close();compressionTarget=null;
}
async function confirmCompressionChange() {
  const s=state.status,mode=compressionTarget,disconnect=mode==="disconnected";
  if(!s||!s.profile?.active||compressionSaving||settingsSaving||linkSaving)return;
  if(!disconnect||!s.profile.can_restore||s.profile.config_error)return;
  compressionSaving=true;compressionRevision++;renderConnectionActions();
  $("compression-confirm-message").hidden=true;
  try {
    const result=await applyLinkAction("restore");
    $("compression-confirm-dialog").close();compressionTarget=null;
    showConnectionFeedback("restore",result);
    renderStatus();
  } catch(error) {
    uiI18n.text($("compression-confirm-message"),error.message);
    $("compression-confirm-message").hidden=false;
  } finally {compressionSaving=false;compressionRevision++;renderConnectionActions();}
}
function renderConnectPrompt() {
  const s=state.status;if(!s)return;
  const dialog=$("connect-confirm-dialog");
  if(s.ui_desktop&&s.ui_window_opened!==desktopOpenRevision) {
    desktopOpenRevision=s.ui_window_opened;startupConnectChecked=dialog.open||connectChecking||connectSaving;
  }
  dialog.setAttribute("aria-busy",String(connectSaving));
  for(const key of ["cancel","apply"])$("connect-confirm-"+key).disabled=connectSaving;
  setText($("connect-confirm-apply"),t(connectSaving?"正在接入…":"确认接入"));
  if(dialog.open&&s.profile?.active&&!connectSaving)cancelConnectPrompt();
  if(startupConnectChecked||!s.ui_desktop||s.ui_exit_request||exitFinished)return;
  if(s.profile?.config_error||typeof s.profile?.active!=="boolean")return;
  if(s.profile.active){startupConnectChecked=true;return;}
  if(settingsSaving||settingsOpening||settingsEditing||linkSaving||compressionSaving||exitSaving||document.querySelector("dialog[open]"))return;
  requestConnection("startup").catch(fail);
}
async function requestConnection(source="header") {
  const s=state.status,dialog=$("connect-confirm-dialog");
  if(!s||s.profile?.active||s.profile?.config_error||connectSaving||connectChecking||linkSaving||settingsSaving||settingsOpening||compressionSaving||exitSaving||dialog.open||$("exit-confirm-dialog").open||$("settings-restart-dialog").open)return;
  if(settingsEditing)return;
  startupConnectChecked=true;connectChecking=true;renderConnectionActions();
  try {await refreshStatus();}
  finally {connectChecking=false;renderConnectionActions();}
  const current=state.status;
  if(current.profile?.active||current.profile?.config_error||current.ui_exit_request||exitSaving||exitFinished||$("exit-confirm-dialog").open)return;
  connectSource=source;
  $("connect-confirm-cancel").textContent=t(source==="startup"?"暂不接入":"取消");
  $("connect-confirm-hint").textContent=(current.loopback_proxy_repair_supported?t("接入时自动检查并补齐本机代理例外。"):"")+t("接入后关闭原生压缩，由本软件管理 zstd。配置变更后需重启 ChatGPT App。");
  $("connect-confirm-message").hidden=true;renderConnectPrompt();dialog.showModal();
}
function cancelConnectPrompt() {
  if(connectSaving)return;
  startupConnectChecked=true;
  $("connect-confirm-dialog").close();connectSource=null;
}
async function confirmConnection() {
  if(connectSaving||!connectSource)return;
  connectSaving=true;renderConnectionActions();renderConnectPrompt();
  $("connect-confirm-message").hidden=true;
  try {
    const result=await linkAction("activate");
    if(!state.status.profile.active)throw Error("接入未完成，请检查 Codex 配置后重试。");
    $("connect-confirm-dialog").close();connectSource=null;
    showConnectionFeedback("activate",result);
  } catch(error) {
    uiI18n.text($("connect-confirm-message"),error.message);$("connect-confirm-message").hidden=false;
  } finally {connectSaving=false;renderConnectionActions();renderConnectPrompt();}
}
function renderExitRequest() {
  const s=state.status,dialog=$("exit-confirm-dialog");
  if(!s||exitFinished)return;
  const id=s.ui_exit_request;
  if(!id) {
    if(!exitSaving){exitRequest=null;if(dialog.open)dialog.close();}
    return;
  }
  if(settingsSaving||settingsOpening||compressionSaving||linkSaving||$("compression-confirm-dialog").open)return;
  if(connectSaving)return;
  if($("connect-confirm-dialog").open)cancelConnectPrompt();
  const fresh=exitRequest!==id;
  exitRequest=id;
  const p=s.profile||{},unknown=typeof p.active!=="boolean"||!!(p.config_error||s.provider_sync_error),connected=!!p.active;
  $("exit-confirm-description").textContent=t(unknown?"无法确认当前接入状态。退出后中转服务将停止，仍使用此出口的 Codex 将无法发送请求。":connected?
    "当前仍处于接入状态。退出后中转服务将停止，Codex 将无法通过当前出口发送请求。":
    "当前为“断开”状态，退出将停止中转服务。");
  $("exit-confirm-hint").textContent=t(connected||unknown?
    "建议选择“切换并退出”，恢复原出口后重启 ChatGPT App。直接退出会保留接入配置，下次使用前需重新打开本程序。":
    "如刚切换为“断开”，请重启 ChatGPT App 使原出口生效。");
  dialog.setAttribute("aria-busy",String(exitSaving));
  for(const key of ["cancel","direct","restore"])$("exit-confirm-"+key).disabled=exitSaving;
  $("exit-confirm-restore").disabled=exitSaving||unknown||connected&&!p.can_restore;
  $("exit-confirm-restore").title=unknown||connected&&!p.can_restore?t("无法自动恢复原出口，请检查接入配置。"):"";
  if(fresh)$("exit-confirm-message").hidden=true;
  if(fresh&&!connected&&!unknown){confirmExit("disconnected");return;}
  if(!dialog.open)dialog.showModal();
}
async function confirmExit(which) {
  if(!exitRequest||exitSaving||exitFinished)return;
  if(which==="restore"&&$("exit-confirm-restore").disabled)return;
  exitSaving=true;exitRevision++;renderConnectionActions();renderExitRequest();
  $("exit-confirm-message").hidden=true;
  try {
    const result=await api("/api/exit",{request_id:exitRequest,action:which});
    if(which==="cancel"?!result.cancelled:!result.stopped)throw Error("退出未完成，请稍后重试。");
    exitFinished=which!=="cancel";
    state.status.ui_exit_request=null;exitRequest=null;$("exit-confirm-dialog").close();
  } catch(error) {
    uiI18n.text($("exit-confirm-message"),error.message);$("exit-confirm-message").hidden=false;
    if(!$("exit-confirm-dialog").open)$("exit-confirm-dialog").showModal();
  } finally {
    exitSaving=false;exitRevision++;renderConnectionActions();renderExitRequest();
    if(!exitFinished)try{await refreshStatus();}catch(_){}
  }
}
function imageDefaultDirectory() {
  const s=state.status,dataDir=s.data_dir||"",separator=dataDir.includes("\\")?"\\":"/";
  return s.image_save_dir_default||(dataDir?dataDir.replace(/[\\/]+$/,"")+separator+"image-attachments":"");
}
function fillImageDirectory() {
  fillStorageDirectories();
  const s=state.status,input=$("image-save-dir"),supported=!!s.image_save_dir_supported;
  const defaultPath=imageDefaultDirectory();
  if(!settingsEditing)input.value=s.image_save_dir||defaultPath;
  input.placeholder=defaultPath;
  for(const id of ["image-save-dir","image-save-dir-reset"])$(id).disabled=settingsSaving||!supported||selectedImageAnalysisMode()!=="optimized";
  fillDirectoryOpenButtons();
}
function fillStorageDirectories() {
  const s=state.status,supported=!!s.storage_paths_supported;
  for(const [id,key] of [["log-dir","log_dir"],["cache-dir","cache_dir"]]) {
    if(!settingsEditing)$(id).value=s[key]||s.data_dir||"";
    $(id).placeholder=s.data_dir||"";
    $(id).disabled=settingsSaving||!supported;$(id+"-reset").disabled=settingsSaving||!supported;
  }
  $("storage-paths-hint").hidden=!s.storage_paths_pending;
  $("storage-paths-hint").textContent=s.storage_paths_pending?t("目录已保存，重启本软件后生效，原目录数据保留。"):"";
}
function fillDirectoryOpenButtons() {
  for(const id of ["log-dir","cache-dir","image-save-dir"])
    $(id+"-open").disabled=settingsSaving||!state.status.open_directory_supported||directoryOpening.has(id);
}
async function openSettingsDirectory(id) {
  if(settingsSaving||!state.status.open_directory_supported||directoryOpening.has(id))return;
  const input=$(id),path=input.value.trim()||input.placeholder;
  directoryOpening.add(id);fillDirectoryOpenButtons();setSettingsMessage("");
  try {await api("/api/open-directory",{path});}
  catch(error) {setSettingsMessage(error.message,true);}
  finally {directoryOpening.delete(id);fillDirectoryOpenButtons();}
}
function fillEntryPort() {
  const s=state.status,port=s.entry_port,supported=!!s.entry_port_supported&&!!port;
  if(!settingsEditing)$("entry-port").value=port?.configured_port??port?.current_port??s.relay_port??47844;
  $("entry-port").disabled=settingsSaving||!supported;
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
  else if(s.profile?.active){label="";kind="active";}
  node.textContent=t(label);node.dataset.state=kind;node.hidden=!label;
  node.title=supported?t("当前端口：{port}").replace("{port}",port.current_port)+(port.pending?" · "+t("待生效端口：{port}").replace("{port}",port.configured_port)+"\n"+t("入口服务重新启动后应用新端口，重启 ChatGPT App 生效。"):""):"";
  if(kind==="error")node.title=t(label)+(node.title?"\n"+node.title:"");
}
function providerStatus(s) {
  if(s.profile.config_error || s.provider_sync_error)return t("无法读取 Codex 配置。");
  if(s.auto_detect_providers && s.profile.all_providers)return t("已自动接管 {count} 个 provider").replace("{count}",s.provider_count);
  return s.profile.active?t("已配置接入"):t("尚未接入");
}
function contextMessage(text,error=false) { const node=$("context-message");uiI18n.text(node,text);node.className="help"+(error?" error":"");node.hidden=!node.textContent; }
function syncContextDefaults() {
  for(const key of ["context","compact"]) {
    const unavailable=settingsSaving||!contextSettings;
    const input=$(key+"-tokens"),useDefault=$(key+"-default").checked;
    $(key+"-default").disabled=unavailable;
    if(useDefault) {
      if(input.value&&input.validity.valid)contextCustomValues.set(key,input.value);
      input.value="";
    }
    input.disabled=unavailable;
    input.readOnly=useDefault;
  }
}
function editContextValue(key) {
  if(!settingsEditing||settingsSaving||!contextSettings)return;
  const useDefault=$(key+"-default");
  if(!useDefault.checked)return;
  useDefault.checked=false;
  $(key+"-tokens").value=contextCustomValues.get(key)||"";
  syncContextDefaults();contextMessage("");updateSettingsSaveState();
}
function fillContextSettings(data) {
  contextSettings=data;
  const maximum=data.max_context_tokens/1000;
  renderContextPlaceholders();
  for(const [key,value] of [["context",data.context_tokens],["compact",data.compact_tokens]]) {
    if(!settingsEditing)$(key+"-default").checked=value==null;
    $(key+"-tokens").max=String(maximum);
    if(!settingsEditing) {
      $(key+"-tokens").value=value==null?"":String(value/1000);
      if(value!=null)contextCustomValues.set(key,String(value/1000));
    }
  }
  syncContextDefaults();
}
function renderContextPlaceholders() {
  if(!contextSettings)return;
  for(const key of ["context","compact"])$(key+"-tokens").placeholder="";
}
async function loadContextSettings() {
  contextMessage("");
  try {fillContextSettings(await api("/api/context-settings"));}
  catch(error) {contextSettings=null;syncContextDefaults();contextMessage(()=>t("上下文配置读取失败：")+t(error.message),true);}
}
let updateStatus=null, updateBusy=false, updateAction="", updateFeedbackVisible=false;
let updateRevision=0,updateStatusLoading=false;
function readSettingsDraft() {
  const draft={
    entry_port:$("entry-port").value,
    sharing_port:$("sharing-port").value,
    egress_port:$("exit-port").value,
    zstd_level:$("zstd-level").value,
    image_zstd_level:$("image-zstd-level").value,
    packet_size_protection_enabled:$("packet-size-protection").checked,
    auto_zstd_level_enabled:$("packet-size-protection").checked&&$("auto-zstd-level").checked,
    budget:$("budget").value,
    start_on_login:$("start-on-login").checked,
    capacity_retry_enabled:$("capacity-retry").checked,
    capacity_retry_max_retries:$("capacity-retry-count").value,
    capacity_retry_delay_seconds:$("capacity-retry-delay").value,
    image_analysis_mode:selectedImageAnalysisMode(),
    image_save_dir:$("image-save-dir").value,
    log_dir:$("log-dir").value,
    cache_dir:$("cache-dir").value,
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
  const node=$("settings-message");uiI18n.text(node,text);node.className="help "+(error?"error":"muted");
}
function renderSettingsSaveProgress() {
  $("settings-save").setAttribute("aria-busy",String(settingsSaving));
  $("settings-save-label").textContent=t(settingsSaving?"保存中":"保存");
  const log=$("settings-save-log"),states={running:["…","处理中"],done:["✓","已保存"],error:["×","失败"]};
  log.replaceChildren(...settingsSaveProgress.steps.map(step=>{
    const item=el("li"),[mark,label]=states[step.state];item.dataset.state=step.state;
    const icon=el("span","settings-step-icon",mark);icon.setAttribute("aria-hidden","true");
    item.append(icon,el("span","settings-step-label",t(step.label)),el("span","settings-step-state",t(label)));
    return item;
  }));
  log.hidden=!settingsSaveProgress.steps.length;log.scrollTop=log.scrollHeight;
  if(settingsSaving) {
    const completed=settingsSaveProgress.steps.filter(step=>step.state==="done").length;
    setSettingsMessage(()=>t("正在保存设置…")+" "+completed+"/"+settingsSaveProgress.total);
  }
}
function resetSettingsSaveProgress() {
  settingsSaveProgress={total:0,steps:[]};renderSettingsSaveProgress();
}
async function runSettingsSaveStep(label,apply) {
  const step={label,state:"running"};settingsSaveProgress.steps.push(step);renderSettingsSaveProgress();
  try {await apply();step.state="done";}
  catch(error) {step.state="error";throw error;}
  finally {renderSettingsSaveProgress();}
}
function updateSettingsSaveState(clearMessage=true) {
  const changed=new Set(settingsEditing?changedSettings():[]),dirty=changed.size>0;
  for(const node of $("settings-dialog").querySelectorAll("[data-settings-field]"))
    node.classList.toggle("settings-dirty",changed.has(node.dataset.settingsField));
  $("settings-save").disabled=settingsSaving||!!linkSaving||!dirty;
  $("settings-restore").disabled=settingsSaving||!dirty;$("settings-close").disabled=settingsSaving;
  if(clearMessage&&settingsEditing&&!settingsSaving)setSettingsMessage(dirty?"有未保存的修改":"");
  if(state.status)renderEntryPortStatus();
}
function trackSettingsRestart(field,before,after) {
  const original=settingsRestartChanges.has(field)?settingsRestartChanges.get(field):before;
  if(original===after)settingsRestartChanges.delete(field);
  else settingsRestartChanges.set(field,original);
}
function finishSettingsSave() {
  const labels={image_analysis_mode:"图片解析",
    context_tokens:"有效上下文",compact_tokens:"自动压缩阈值",capacity_retry:"容量不足重试"};
  const items=[...settingsRestartChanges.keys()].map(field=>t(labels[field]));
  if(state.status.entry_port?.pending)items.unshift(t("入口端口"));
  if(!items.length){closeSettings();return;}
  setSettingsMessage("设置已保存。");
  showConfigurationFeedback(items);
  if(settingsRestartChanges.has("capacity_retry"))connectionRestartNotified=true;
}
async function saveSettings() {
  if(!settingsEditing||settingsSaving||linkSaving||!settingsBaseline||$("settings-restart-dialog").open)return;
  const draft=readSettingsDraft(),changed=new Set(changedSettings(draft)),s=state.status;
  if(!changed.size)return;
  for(const input of $("settings-dialog").querySelectorAll(".settings-content input:not(:disabled)")) {
    if(!input.reportValidity())return;
  }
  const protectionSupported="packet_size_protection_enabled" in s,storage=storageLimitSettings(s);
  const entries=[
    ["zstd_level","zstd_level",Number(draft.zstd_level),Number.isInteger(s.zstd_level)],
    ["sharing_port","sharing_port",Number(draft.sharing_port),s.sharing_settings_supported],
    ["egress_port","egress_port",Number(draft.egress_port),s.sharing_settings_supported],
    ["image_zstd_level","image_zstd_level",Number(draft.image_zstd_level),Number.isInteger(s.image_zstd_level)],
    ["packet_size_protection_enabled","packet_size_protection_enabled",draft.packet_size_protection_enabled,protectionSupported],
    ["auto_zstd_level_enabled","auto_zstd_level_enabled",draft.auto_zstd_level_enabled,typeof s.auto_zstd_level_enabled==="boolean"],
    ["budget","budget_bytes",Math.round(Number(draft.budget)*1000),protectionSupported],
    ["start_on_login","start_on_login",draft.start_on_login,s.start_on_login_supported],
    ["capacity_retry_enabled","capacity_retry_enabled",draft.capacity_retry_enabled,s.capacity_retry_supported],
    ["capacity_retry_max_retries","capacity_retry_max_retries",Number(draft.capacity_retry_max_retries),Number.isInteger(s.capacity_retry_max_retries)],
    ["capacity_retry_delay_seconds","capacity_retry_delay_seconds",Number(draft.capacity_retry_delay_seconds),Number.isInteger(s.capacity_retry_delay_seconds)],
    ["image_analysis_mode","image_analysis_mode" in s?"image_analysis_mode":"image_isolation_enabled",
      "image_analysis_mode" in s?draft.image_analysis_mode:draft.image_analysis_mode==="optimized",
      s.image_isolation_supported||["native","disabled","optimized"].includes(s.image_analysis_mode)],
    ["image_save_dir","image_save_dir",draft.image_save_dir===imageDefaultDirectory()?"":draft.image_save_dir,s.image_save_dir_supported],
    ["log_dir","log_dir",draft.log_dir===s.data_dir?"":draft.log_dir,s.storage_paths_supported],
    ["cache_dir","cache_dir",draft.cache_dir===s.data_dir?"":draft.cache_dir,s.storage_paths_supported],
    ["max_storage_mb",storage.key,storage.toBackend(draft.max_storage_mb),true],
    ["max_packets","max_packets",Number(draft.max_packets),true]
  ].filter(([field,,,supported])=>supported&&changed.has(field));
  const contextChanged=contextSettings&&["context_tokens","compact_tokens"].some(key=>changed.has(key));
  const portChanged=s.entry_port_supported&&s.entry_port&&changed.has("entry_port");
  const controls=new Map([...$("settings-dialog").querySelectorAll("input,select,button")].map(node=>[node,node.disabled]));
  let saved=0,error=null;
  settingsSaveProgress={total:Number(!!entries.length)+Number(!!contextChanged)+Number(!!portChanged)+Number(changed.has("language")),steps:[]};
  settingsSaving=true;settingsRevision++;
  for(const node of controls.keys())node.disabled=true;
  $("settings-dialog").setAttribute("aria-busy","true");renderSettingsSaveProgress();
  try {
    if(entries.length) {
      const label=entries.some(([field])=>["log_dir","cache_dir","image_save_dir"].includes(field))?"存储位置":
        entries.some(([field])=>field==="image_analysis_mode")?"图片与运行设置":
        entries.some(([field])=>field==="start_on_login")?"开机启动与运行设置":"运行设置";
      await runSettingsSaveStep(label,async()=>{
        const values=Object.fromEntries(entries.map(([,key,value])=>[key,value]));
        if(!s.retention_policy&&Number.isInteger(s.max_detail_packets)&&entries.some(([field])=>["max_packets","max_storage_mb"].includes(field)))
          values.max_detail_packets=Number(draft.max_packets);
        const result=await api("/api/settings",values);
        state.status={...state.status,...result};
        for(const [field] of entries)settingsBaseline[field]=draft[field];
        if(entries.some(([field])=>field==="image_analysis_mode"))
          trackSettingsRestart("image_analysis_mode",imageAnalysisMode(s),imageAnalysisMode(result));
        if(result.capacity_retry_mode==="native"&&result.profile?.active&&
           entries.some(([field])=>["capacity_retry_enabled","capacity_retry_max_retries"].includes(field)))
          trackSettingsRestart("capacity_retry",s.capacity_retry_enabled?s.capacity_retry_max_retries:null,
            result.capacity_retry_enabled?result.capacity_retry_max_retries:null);
        saved++;
      });
    }
    if(contextChanged) {
      await runSettingsSaveStep("上下文设置",async()=>{
        const values=Object.fromEntries(["context_tokens","compact_tokens"].map(key=>[key,draft[key]===null?null:Math.round(Number(draft[key])*1000)]));
        const previous=contextSettings;
        contextSettings=await api("/api/context-settings",values);
        if(contextSettings.changed||contextSettings.reload_required)
          for(const field of ["context_tokens","compact_tokens"])trackSettingsRestart(field,previous[field],contextSettings[field]);
        settingsBaseline.context_tokens=draft.context_tokens;settingsBaseline.compact_tokens=draft.compact_tokens;
        saved++;
      });
    }
    if(portChanged) {
      await runSettingsSaveStep("入口端口",async()=>{
        state.status.entry_port=await api("/api/entry-port",{port:Number(draft.entry_port)});
        settingsBaseline.entry_port=draft.entry_port;saved++;
      });
    }
    if(changed.has("language")) {
      await runSettingsSaveStep("语言",async()=>{
        if(["zh","en"].includes(state.status.ui_language)) {
          const result=await api("/api/settings",{ui_language:draft.language});
          if(result.ui_language!==draft.language)throw Error("语言设置未保存，请重试。");
          state.status={...state.status,...result};
        }
        try {await setInterfaceLanguage(draft.language);}
        finally {if(uiI18n.language===draft.language)settingsBaseline.language=draft.language;}
        saved++;
      });
    }
    if(changedSettings().length)throw Error("有未保存的修改");
  } catch(failure) {error=failure;}
  finally {
    settingsSaving=false;settingsRevision++;
    for(const [node,disabled] of controls)if(node.isConnected)node.disabled=disabled;
    $("settings-dialog").setAttribute("aria-busy","false");
    renderSettingsSaveProgress();
    fillSettings();renderContextPlaceholders();renderUpdates();renderStatus();updateSettingsSaveState(false);
    if(error)setSettingsMessage(()=>t(saved?"部分设置已保存，剩余修改未保存：":"保存失败：")+t(error.message),true);
    else finishSettingsSave();
  }
}
function renderUpdates() {
  const u=updateStatus, busy=settingsSaving||updateBusy||["checking","downloading","upgrading"].includes(u?.state);
  const currentVersion=u?.current_version||state.status?.version;
  $("updates-versions").textContent=currentVersion?t("当前版本：{version}").replace("{version}",currentVersion):"";
  $("updates-check").disabled=busy;$("updates-install").disabled=busy||u?.state!=="available";
  const labels={idle:"可检查 GitHub 上的最新版本。",checking:"正在检查更新…",current:"已是最新版本。",available:"发现新版本，可以升级。",downloading:"正在下载并校验升级包…",upgrading:"正在升级，旧请求继续完成…",complete:"升级已完成。"};
  const displayedState=updateBusy?(updateAction==="check"?"checking":"downloading"):u?.state;
  const checking=displayedState==="checking",installing=["downloading","upgrading"].includes(displayedState);
  if(checking||installing)updateFeedbackVisible=true;
  $("updates-check").setAttribute("aria-busy",String(checking));
  $("updates-install").setAttribute("aria-busy",String(installing));
  $("updates-check-label").textContent=t(checking?"检查中":"检查更新");
  $("updates-install-label").textContent=t(installing?"升级中":"立即升级");
  const message=$("updates-message");
  message.hidden=!updateFeedbackVisible;
  message.textContent=displayedState==="error"?t(u.error):displayedState==="available"&&u.latest_version?t("最新版本：{version}").replace("{version}",u.latest_version):t(labels[displayedState]||labels.idle);
  message.title=message.textContent;
  message.classList.toggle("error",displayedState==="error");
}
async function loadUpdates() {
  if(updateStatusLoading||updateBusy)return;
  const revision=updateRevision;updateStatusLoading=true;
  try {const result=await api("/api/updates");if(revision===updateRevision&&!updateBusy)updateStatus=result;}
  catch(error){if(revision===updateRevision&&!updateBusy)updateStatus={...updateStatus,state:"error",error:error.message};}
  finally {updateStatusLoading=false;renderUpdates();}
}
async function performUpdate(which) {
  if(settingsSaving||updateBusy||["checking","downloading","upgrading"].includes(updateStatus?.state))return;
  updateRevision++;updateBusy=true;updateAction=which;updateFeedbackVisible=true;renderUpdates();
  try {
    updateStatus=await api("/api/updates",{action:which});
    // Older services can acknowledge an install before its first state write.
    if(which==="install"&&updateStatus.state==="available")updateStatus={...updateStatus,state:"downloading"};
  }
  catch(error){updateStatus={...updateStatus,state:"error",error:error.message};}
  finally {updateBusy=false;updateAction="";renderUpdates();}
}
async function openSettings() {
  if(settingsOpening||compressionSaving||exitSaving||connectSaving||connectChecking||$("connect-confirm-dialog").open||$("exit-confirm-dialog").open||$("compression-confirm-dialog").open||$("settings-restart-dialog").open||$("settings-dialog").open)return;
  settingsOpening=true;
  updateFeedbackVisible=updateBusy||["checking","downloading","upgrading"].includes(updateStatus?.state);
  try {
    await Promise.all([refreshStatus(true),loadContextSettings(),loadUpdates()]);
    settingsLanguage=uiI18n.language;
    settingsBaseline=readSettingsDraft();settingsEditing=true;
    resetSettingsSaveProgress();
    renderLanguageSelection();renderConnectionActions();renderUpdates();updateSettingsSaveState();
    $("settings-dialog").showModal();
  } finally {settingsOpening=false;}
}
async function selectPacket(id,refresh=false,expectedListRequest=null) {
  const requestId=++state.detailRequest;
  let m;
  try {m=await api("/api/packets/"+id);}
  catch(error) {
    if(requestId!==state.detailRequest)return;
    if(error.status!==404||!refresh||state.selected!==id)throw error;
    cancelTokenRead();tokenWindow=null;
    state.selected=null;state.detail=null;state.detailVersion="";state.text=null;
    requestBodyCache=null;requestBodyView=null;requestBodySequence++;clearJsonFocus();clearJsonDownload();
    $("packet-detail").hidden=true;$("empty-state").hidden=false;renderList();return;
  }
  if(requestId!==state.detailRequest || (refresh && state.selected!==id) || (expectedListRequest!==null && expectedListRequest!==state.listRequest)) return;
  const changed=state.selected!==id, previousDetail=state.detail, detailsChanged=!!m.details_evicted!==!!previousDetail?.details_evicted; state.selected=id; state.detail=m; state.detailVersion=[m.state,m.status,m.duration_ms,m.outbound_wire_bytes,m.outbound_content_encoding,m.relay_content_encoding,m.relay_zstd_level,m.transport_content_encoding,m.transport_zstd_level,m.usage_override_request_bytes,m.usage_override_applied,m.capacity_retry_count,m.native_retry_advised,m.native_retry_delay_ms,m.capacity_retry_diagnostic?.updated,m.details_evicted].join(":");
  showTab(state.tab);
  $("empty-state").hidden=true; $("packet-detail").hidden=false; renderList();
  if(changed || tokenVersion(m)!==tokenVersion(previousDetail||{})) {cancelTokenRead();tokenWindow=cachedTokenWindow(m);}
  else if(tokenWindow?.state==="unavailable") tokenWindow=null;
  loadTokenWindow();
  renderOverview(m); renderCompression(m); renderHeaders(m);
  if(changed)$("diff-content").replaceChildren(el("p","muted",t("加载中…")));
  if(changed||detailsChanged) {state.path=""; state.text=null; $("body-results").hidden=true; await loadTree("");if(requestId===state.detailRequest)await loadDiff();}
  else if(state.tab==="diff") await loadDiff();
}
function renderProtectionPanelState() {
  const panel=$("packet-protection-panel");if(!panel)return;
  const enabled={size:state.status?.packet_size_protection_enabled===true,retry:state.status?.capacity_retry_enabled===true,
    image:state.status?.packet_size_protection_enabled===true};
  const rows=[...panel.querySelectorAll("[data-protection]")];
  const disabled=!rows.some(row=>enabled[row.dataset.protection]);
  panel.classList.toggle("protection-disabled",disabled);
  panel.setAttribute("aria-disabled",String(disabled));
  panel.title=disabled?t("可在设置中开启请求保护"):"";
  for(const row of rows) {
    const inactive=!enabled[row.dataset.protection];
    row.classList.toggle("protection-inactive",inactive);
    row.title=inactive?t(row.dataset.protection==="retry"?"可在设置中开启重试保护":"可在设置中开启请求大小保护"):"";
  }
}
function renderCompression(m) {
  const root=document.createDocumentFragment();
  const sizes=panel(t("请求大小")), original=originalBytes(m), compressed=compressedBytes(m);
  fact(sizes,t("原始请求"),bytes(original)).title=t("原始请求大小（图片处理、传输压缩前）");
  fact(sizes,t("转发请求"),bytes(compressed)).title=t("转发请求大小（图片处理、传输压缩后，超限摘要处理前）");
  const summaryTriggered=m.overflow_compaction_calls>0||m.overflow_cache_hit===true;
  const hasTransport=typeof m.transport_content_encoding==="string";
  // Older summary records only retain the final body's encoding and grade.
  // Those values cannot describe the pre-summary packet shown beside them.
  const encoding=hasTransport?m.transport_content_encoding:summaryTriggered?null:m.relay_content_encoding||m.outbound_content_encoding;
  fact(sizes,t("压缩方式"),encoding==="identity"?t("不压缩"):encoding||"—");
  const level=hasTransport?m.transport_zstd_level:summaryTriggered?null:m.relay_zstd_level;
  fact(sizes,t("压缩等级"),Number.isInteger(level)&&level>=1&&level<=22?level:"—");
  if(m.image_upload_optimized) {
    fact(sizes,t("缩图前转发请求"),bytes(m.image_upload_before_bytes));
  }
  if(original>0 && compressed!==null)fact(sizes,t("减小比例"),(100*(1-compressed/original)).toFixed(1)+"%").title=t("包含图片处理和传输压缩带来的大小变化，按超限摘要处理前的正文计算。");
  if(summaryTriggered) {
    const estimate=requestTokenUsage(m), actual=m.upstream_usage?.input_tokens;
    fact(sizes,t("输入 Token（估算）"),sortInputTokens(estimate)!==null?"≈ "+nf.format(estimate.input_tokens):"—").title=t("超限摘要前输入 Token（估算）");
    fact(sizes,t("上游实际输入 Token"),Number.isSafeInteger(actual)&&actual>=0?nf.format(actual):"—");
  }
  root.append(sizes);
  if(m.image_upload_optimized&&Array.isArray(m.image_upload_images)&&m.image_upload_images.length) {
    const resolutions=panel(t("图片分辨率"));
    const resolution=(width,height)=>Number.isSafeInteger(width)&&width>0&&Number.isSafeInteger(height)&&height>0?`${width} × ${height}`:"—";
    for(const [index,image] of m.image_upload_images.entries()) {
      const name=image.mask?t("编辑蒙版"):t("参考图 {index}").replace("{index}",index+1);
      fact(resolutions,name+" · "+t("压缩前"),resolution(image.original_width,image.original_height));
      fact(resolutions,name+" · "+t("压缩后"),resolution(image.width,image.height));
    }
    root.append(resolutions);
  }
  const steps=panel(t("请求保护"));steps.id="packet-protection-panel";steps.classList.add("packet-protection-panel");
  const triggerStatus=triggered=>triggered?t("已触发"):m.source==="live"?t("未触发"):t("暂无记录");
  const predictiveArmed=Number.isSafeInteger(m.usage_override_request_bytes)&&m.usage_override_request_bytes>0;
  const predictiveSatisfied=m.usage_override_applied===false&&m.reported_usage!=null;
  const waiting=["captured","streaming"].includes(m.state);
  const predictiveStatus=m.usage_override_applied===true?t("已触发"):predictiveSatisfied?t("已满足"):predictiveArmed?t(waiting?"等待响应":"未完成"):triggerStatus(false);
  const predictive=fact(steps,t("预测保护"),predictiveStatus);predictive.dataset.protection="size";
  if(predictiveArmed&&m.usage_override_applied!==true&&!predictiveSatisfied)predictive.title=t("已达到保护条件；需要完整响应中的有效 Token 用量才能完成改写。");
  const summaryFailed=typeof m.relay_error==="string"&&m.relay_error.startsWith("overflow_");
  fact(steps,t("超限保护"),summaryFailed?t("处理失败"):triggerStatus(summaryTriggered)).dataset.protection="size";
  fact(steps,t("重试保护"),triggerStatus(m.native_retry_advised===true||capacityRetryCount(m)>0)).dataset.protection="retry";
  if(m.request_kind==="image_edit"||m.request_kind==="image_generation"||/\/images\/(?:edits|generations)\/*(?:\?.*)?$/.test(m.path||"")) {
    fact(steps,t("参考图缩小"),triggerStatus(m.image_upload_optimized===true)).dataset.protection="image";
  }
  root.append(steps);reconcileContent($("compression-content"),root);renderProtectionPanelState();
}
function bars(title, rows, total,bodyAvailable=true) {
  const p=panel(title);
  for(const r of rows) { const row=el("div","bar-row"), label=el("div","bar-label");row.dataset.renderKey="bar:"+r.name; label.append(bodyAvailable?link(r.name,()=>goPath(r.path)):el("span","",r.name),el("span","",bytes(r.bytes)+" · "+(r.bytes/(total||1)*100).toFixed(1)+"%")); const progress=el("progress");progress.max=total||1;progress.value=r.bytes;row.append(label,progress);p.append(row); }
  return p;
}
function renderOverview(m) {
  const root=document.createDocumentFragment();
  if(m.inspection!=="json") {reconcileContent($("overview"),el("p","muted",t("正文未展开：")+(t(m.inspection_error)||t("正文为空"))+t("。转发仍按原始正文进行。")));return;}
  const cols=el("div","two-columns");
  const fields=[...(m.top_fields||[]).map(r=>({...r,path:"/"+r.name.replaceAll("~","~0").replaceAll("/","~1")})),{name:t("字段名、标点及空白"),bytes:m.json_envelope_bytes,path:""}].sort((a,b)=>b.bytes-a.bytes);
  cols.append(bars(t("字段构成"),fields,m.decoded_bytes,!m.details_evicted));
  const groups=Object.entries(m.input_groups||{}).map(([name,size])=>({name:kind(name),bytes:size,path:"/input"})).sort((a,b)=>b.bytes-a.bytes);
  cols.append(bars(t("input 构成"),groups,(m.top_fields.find(r=>r.name==="input")||{}).bytes,!m.details_evicted));root.append(cols);reconcileContent($("overview"),root);
}
function jsonValueNode(value,key=null,comma=false,root=false,path="",nodes=new Map()) {
  const object=value!==null&&typeof value==="object", array=Array.isArray(value);
  const keys=object?Object.keys(value):[];
  const longString=!root&&typeof value==="string"&&value.length>180;
  const expandable=keys.length>0||longString;
  const collapsible=expandable&&!root;
  const node=el(collapsible?"details":"div",expandable?(root?"json-root":"json-node"):"json-line");
  const line=expandable?el(root?"div":"summary",root?"json-line":""):node;
  node.dataset.jsonPath=path;node.jsonLine=line;nodes.set(path,node);
  if(key!==null)line.append(el("span","json-key",JSON.stringify(key)),document.createTextNode(": "));
  if(longString) {
    const preview=el("span","json-string-short");
    preview.append(el("span","json-string",JSON.stringify(value.slice(0,120))),el("span","json-ellipsis"," …"));
    const full=el("span","json-string json-string-full");
    line.append(preview,full,document.createTextNode(comma?",":""));node.append(line);
    const expand=()=>{if(node.open&&!full.hasChildNodes())full.textContent=JSON.stringify(value);};
    node.jsonExpand=()=>{node.open=true;expand();};node.addEventListener("toggle",expand);
    return node;
  }
  if(!object||!keys.length) {
    const kind=value===null?"null":typeof value;
    line.append(el("span","json-"+kind,JSON.stringify(value)),document.createTextNode(comma?",":""));
    return node;
  }
  const close=(array?"]":"}")+(comma?",":"");
  line.append(document.createTextNode(array?"[":"{"));
  if(!root)line.append(el("span","json-collapsed"," … "+close));
  const children=el("div","json-children");
  node.append(line,children,el("div","json-closing",close));
  let loaded=false,offset=0,more=null;
  const childPath=childKey=>path+"/"+childKey.replaceAll("~","~0").replaceAll("/","~1");
  function appendPage() {
    more?.remove();more=null;
    const fragment=document.createDocumentFragment(),end=Math.min(offset+100,keys.length);
    for(;offset<end;offset++) {
      const childKey=keys[offset];
      fragment.append(jsonValueNode(value[childKey],array?null:childKey,offset<keys.length-1,false,childPath(childKey),nodes));
    }
    children.append(fragment);
    if(offset<keys.length) {
      more=el("button","json-more",t("展开更多…"));more.type="button";
      more.addEventListener("click",appendPage);children.append(more);
    }
  }
  function expand() {if((root||node.open)&&!loaded){loaded=true;appendPage();}}
  node.jsonExpand=()=>{if(collapsible)node.open=true;expand();};
  node.jsonReveal=childKey=>{
    const index=keys.indexOf(childKey);if(index<0)return null;
    node.jsonExpand();while(offset<=index)appendPage();
    return nodes.get(childPath(childKey));
  };
  if(root)expand();
  else node.addEventListener("toggle",expand);
  return node;
}
function clearJsonDownload() {
  if(requestBodyDownloadUrl)URL.revokeObjectURL(requestBodyDownloadUrl);
  requestBodyDownloadUrl="";
  const download=$("json-download");download.hidden=true;download.removeAttribute("href");
}
function clearJsonFocus() {
  requestBodyHighlight?.classList.remove("json-focused");requestBodyHighlight=null;
}
async function ensureJsonView(id,sequence) {
  if(state.detail?.id===id&&state.detail.details_evicted)throw new Error("请求详情已回收，仅保留基本信息。");
  if(requestBodyView?.id===id&&requestBodyView.language===uiI18n.language)return requestBodyView;
  const root=$("json-tree");root.textContent=t("加载中…");requestBodyView=null;clearJsonFocus();clearJsonDownload();
  if(requestBodyCache?.id!==id) {
    const pending={id,promise:null};
    pending.promise=api("/api/packets/"+id+"/export").then(data=>{
      if(!data.view||!Object.prototype.hasOwnProperty.call(data.view,"body"))throw new Error("此请求没有可解析的 JSON 正文");
      return data.view.body;
    }).catch(error=>{if(requestBodyCache===pending)requestBodyCache=null;throw error;});
    requestBodyCache=pending;
  }
  const body=await requestBodyCache.promise;
  if(state.selected!==id||sequence!==requestBodySequence)return null;
  const nodes=new Map(),node=jsonValueNode(body,null,false,true,"",nodes);
  root.replaceChildren(node);root.scrollTop=0;root.scrollLeft=0;
  requestBodyView={id,body,nodes,root:node,language:uiI18n.language};
  if(state.detail?.id===id)renderHeaders(state.detail);
  const text=JSON.stringify(body,null,2);
  requestBodyDownloadUrl=URL.createObjectURL(new Blob([text+"\n"],{type:"application/json;charset=utf-8"}));
  const download=$("json-download");download.href=requestBodyDownloadUrl;download.download="codex-request-"+id+".json";download.hidden=false;
  return requestBodyView;
}
function focusJsonPath(view,path) {
  if(path&&(!path.startsWith("/")||/~(?![01])/.test(path)))throw new Error("JSON 路径不存在。");
  const segments=path?path.slice(1).split("/").map(segment=>segment.replace(/~1/g,"/").replace(/~0/g,"~")):[];
  let value=view.body;
  for(const segment of segments) {
    if(value===null||typeof value!=="object"||!Object.prototype.hasOwnProperty.call(value,segment))throw new Error("JSON 路径不存在。");
    value=value[segment];
  }
  let node=view.root;
  for(const segment of segments)node=node.jsonReveal(segment);
  node.jsonExpand?.();clearJsonFocus();
  const line=node.jsonLine||node;requestBodyHighlight=line;line.classList.add("json-focused");
  const container=$("json-tree"),offset=line.getBoundingClientRect().top-container.getBoundingClientRect().top;
  container.scrollTop=Math.max(0,container.scrollTop+offset-container.clientHeight*.35);container.scrollLeft=0;
}
async function loadTree(path) {
  clearTimeout(treeQueryTimer);
  if(!state.selected)return;
  const id=state.selected, sequence=++requestBodySequence;
  state.path=path;$("tree-query").value=path;$("body-results").hidden=true;
  $("request-body-view").hidden=false;clearJsonFocus();
  $("tree-query").disabled=!!state.detail?.details_evicted;
  if(state.detail?.details_evicted) {
    requestBodyView=null;requestBodyCache=null;clearJsonDownload();
    $("json-tree").textContent=t("请求详情已回收，仅保留基本信息。");return;
  }
  try {
    const view=await ensureJsonView(id,sequence);
    if(!view||state.selected!==id||sequence!==requestBodySequence)return;
    if(path)focusJsonPath(view,path);
    else {$("json-tree").scrollTop=0;$("json-tree").scrollLeft=0;}
  } catch(error) {
    if(state.selected===id&&sequence===requestBodySequence) {
      fail(error);
      if(!requestBodyView)$("json-tree").textContent="";
    }
  }
}
async function goPath(path) {showTab("tree");await loadTree(path);}
function scheduleTreeQuery(event) {
  clearTimeout(treeQueryTimer);requestBodySequence++;
  $("body-results").hidden=true;clearJsonFocus();
  if(event.isComposing||!state.selected)return;
  if(!$("tree-query").value.trim()) {submitTreeQuery().catch(fail);return;}
  treeQueryTimer=setTimeout(()=>submitTreeQuery().catch(fail),180);
}
async function submitTreeQuery() {
  clearTimeout(treeQueryTimer);
  if(!state.selected)return;
  const query=$("tree-query").value.trim();
  if(!query || query.startsWith("/")) {
    $("body-results").hidden=true;
    await loadTree(query);
  } else await searchBody(query);
}
async function searchBody(query) {
  const id=state.selected,sequence=++requestBodySequence,root=$("body-results");
  $("request-body-view").hidden=false;clearJsonFocus();
  root.hidden=false;root.replaceChildren(el("small","muted",t("加载中…")));
  try {
    const [view,results]=await Promise.all([ensureJsonView(id,sequence),api("/api/packets/"+id+"/search?q="+encodeURIComponent(query))]);
    if(!view||state.selected!==id||sequence!==requestBodySequence||$("tree-query").value.trim()!==query)return;
    root.replaceChildren(el("small","muted",t("找到 ")+results.length+t(" 项（最多 60 项）")));
    for(const hit of results) {
      const n=el("div","search-hit");
      n.append(link(hit.path||t("根节点"),()=>loadTree(hit.path)),el("p","",hit.preview));root.append(n);
    }
    if(results.length) {state.path=results[0].path||"";focusJsonPath(view,state.path);}
  } catch(error) {
    if(state.selected===id&&sequence===requestBodySequence) {
      root.hidden=true;fail(error);
      if(!requestBodyView)$("json-tree").textContent="";
    }
  }
}
function packetDiffRows(data,current=null) {
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
  if(items.length&&!fields.has("input")) {
    const size=current?.top_fields?.find(field=>field.name==="input")?.bytes;
    const total=Number.isFinite(size)&&size>=0?size:null;
    fields.set("input",{field:"input",scope:"field",before:total,after:total,delta:0,change:"changed",path:"/input"});
  }
  const names=new Set(fields.keys());
  const rows=[];
  for(const name of [...names].sort()) {
    if(fields.has(name))rows.push(fields.get(name));
    if(name==="input")rows.push(...items);
  }
  return rows;
}
function summaryPacketDiff(previous,current) {
  const difference=key=>Number.isSafeInteger(previous[key])&&Number.isSafeInteger(current[key])?current[key]-previous[key]:null;
  const before=compressedBytes(previous),after=compressedBytes(current),fieldsAvailable=Array.isArray(previous.top_fields)&&Array.isArray(current.top_fields);
  const left=new Map((fieldsAvailable?previous.top_fields:[]).map(row=>[row.name,row.bytes])),right=new Map((fieldsAvailable?current.top_fields:[]).map(row=>[row.name,row.bytes]));
  return {previous_id:previous.id,state:"summary_only",fields_available:fieldsAvailable,
    decoded_delta:difference("decoded_bytes"),wire_delta:difference("wire_bytes"),upload_delta:difference("outbound_wire_bytes"),
    compressed_delta:before!==null&&after!==null?after-before:null,
    fields:fieldsAvailable?[...new Set([...left.keys(),...right.keys()])].sort().filter(name=>left.get(name)!==right.get(name)).map(name=>({name,before:left.get(name)??0,after:right.get(name)??0})):[]};
}
async function readSummaryPacketDiff(data,current,active) {
  if(!current?.thread_id||!current.source||!Number.isFinite(current.created))return data;
  let previousId=data.previous_id;
  if(!previousId) {
    const query=new URLSearchParams({limit:"50",thread:current.thread_id,before:String(current.created),sort:"created",direction:"desc",include_get:"1"});
    for(let page=0;page<8;page++) {
      const rows=await api("/api/packets?"+query);if(!active())return null;
      if(!Array.isArray(rows))return data;
      const previous=rows.find(row=>row.thread_id===current.thread_id&&row.source===current.source&&row.created<current.created);
      if(previous){previousId=previous.id;break;}
      if(rows.length<50)return null;
      const next=rows.at(-1)?.id;if(!next||query.get("after")===next)return data;query.set("after",next);
    }
  }
  if(!previousId)return data;
  let previous;
  try {previous=await api("/api/packets/"+previousId);}catch(error){if(error.status===404)return data;throw error;}
  if(!active())return null;
  if(previous?.thread_id!==current.thread_id||previous.source!==current.source||!(previous.created<current.created))return data;
  return summaryPacketDiff(previous,current);
}
async function loadDiff() {
  const id=state.selected,current=state.detail,active=()=>state.selected===id&&state.detail===current;
  let data=await api("/api/packets/"+id+"/diff");if(!active())return;
  if(data?.state==="details_unavailable")data=await readSummaryPacketDiff(data,current,active);if(!active())return;
  if(data?.state==="details_unavailable") {
    reconcileContent($("diff-content"),el("p","muted",t("暂时无法取得可比较的统计信息。")));return;
  }
  const delta=data?await compressedDelta(data,current):null;if(!active())return;
  const root=document.createDocumentFragment();
  if(!data){reconcileContent($("diff-content"),el("p","muted",t("没有可比较的上条请求。")));return;}
  const p=panel(t("相对上条请求"));fact(p,t("转发请求变化"),signed(delta));fact(p,t("原始请求变化"),signed(data.decoded_delta));root.append(p);
  if(data.state==="summary_only")root.append(el("small","muted",t("输入条目详情已回收，以下显示统计变化。")));
  const changes=panel(t("变化明细")),rows=packetDiffRows(data,state.detail);
  const labels={added:t("新增"),removed:t("移除"),changed:t("内容改变")};
  if(rows.length) {
    const grid=table([t("字段与条目"),t("变化"),t("当前")],rows.map(r=>{
      const entry=el("div",r.scope==="field"?"diff-field-entry":"diff-item-entry");
      if(r.scope==="field")entry.append(el("span","",r.field),el("span","muted",t("字段合计")));
      else {
        entry.append(r.path?link("#"+r.index,()=>goPath(r.path)):el("span","","#"+r.index));
        entry.append(el("span","cell-note",kind(r.kind)+(r.name?" · "+r.name:"")));
      }
      const change=r.change==="grow"||r.change==="shrink"?signed(r.delta):labels[r.change];
      return [entry,change,bytes(r.after)];
    }));
    grid.classList.add("diff-table");
    grid.querySelectorAll("tbody tr").forEach((row,index)=>{
      const current=rows[index],next=rows[index+1];
      row.dataset.renderKey=JSON.stringify([current.scope,current.field,current.index,current.kind,current.name]);
      row.classList.add(current.scope==="field"?"diff-field-row":"diff-item-row");
      if(current.scope==="item"&&(next?.scope!=="item"||next.field!==current.field))row.classList.add("diff-item-last");
    });
    changes.append(grid);
  }
  else changes.append(el("p","muted",t(data.state==="summary_only"?(data.fields_available===false?"缺少可比较的字段统计。":"保留的字段大小未变化。"):"没有发生变化。")));
  root.append(changes);reconcileContent($("diff-content"),root);
}
function localImagePath(value) {
  if(typeof value!=="string"||value.length>4096)return null;
  value=value.trim();
  if(/^file:/i.test(value)) {
    try {const url=new URL(value);value=decodeURIComponent((url.hostname&&url.hostname!=="localhost"?"//"+url.hostname:"")+url.pathname).replace(/^\/([A-Za-z]:[/\\])/,"$1");}
    catch(_) {return null;}
  }
  return !/[\r\n]/.test(value)&&/\.(png|jpe?g|webp|gif|bmp|tiff?|heic|heif|avif)$/i.test(value)&&(/^(?:[A-Za-z]:[/\\]|\/|\\\\)/.test(value))?value:null;
}
function requestImagePaths(meta,body) {
  const rewrites=new Map();
  if(meta.image_path_rewrites&&typeof meta.image_path_rewrites==="object"&&!Array.isArray(meta.image_path_rewrites))
    for(const [from,to] of Object.entries(meta.image_path_rewrites).slice(0,32)) {
      const source=localImagePath(from),target=localImagePath(to);if(source&&target)rewrites.set(source,target);
    }
  const paths=new Set(), add=value=>{const path=localImagePath(value);if(path&&paths.size<32)paths.add(rewrites.get(path)||path);};
  if(Array.isArray(meta.image_paths))meta.image_paths.forEach(add);
  for(const item of Array.isArray(body?.input)?body.input:[]) {
    if(item?.role!=="user")continue;
    for(const part of typeof item.content==="string"?[{text:item.content}]:Array.isArray(item.content)?item.content:[]) {
      if(!part||typeof part!=="object")continue;
      if(part.type==="input_image")for(const key of ["image_path","path","image_url"])add(part[key]);
      if(typeof part.text==="string") {
        add(part.text);
        for(const match of part.text.matchAll(/<image\b[^>]*\bpath=["']([^"'\r\n]+)["'][^>]*>/g))add(match[1]);
      }
    }
  }
  return [...paths];
}
function imagePathGroups(paths) {
  const groups=new Map();
  for(const path of paths) {
    const split=Math.max(path.lastIndexOf("/"),path.lastIndexOf("\\"));
    const directory=path.slice(0,split+1),name=path.slice(split+1);
    const windows=/^(?:[A-Za-z]:[/\\]|\\\\|\/\/)/.test(path);
    const key=windows?"windows:"+directory.replace(/\\/g,"/").replace(/\/+/g,"/").toLowerCase():"path:"+directory;
    if(!groups.has(key))groups.set(key,{directory,files:[]});
    groups.get(key).files.push({name,path});
  }
  return [...groups.values()];
}
function renderImageInfo(paths) {
  const images=panel(t("图片信息"));images.id="request-image-info";
  if(!paths.length) {images.append(el("p","muted",t("未记录图片路径")));return images;}
  for(const group of imagePathGroups(paths)) {
    const section=el("section","image-path-group");section.dataset.renderKey="image-folder:"+group.directory;section.setAttribute("aria-label",t("图片目录")+"："+group.directory);
    const directory=el("code","image-path-directory",group.directory);directory.title=group.directory;
    const list=el("ul","image-path-list");
    for(const {name,path} of group.files) {
      const item=el("li","image-path-item"),filename=el("code","image-path-name",name);
      item.dataset.renderKey="image:"+path;
      filename.title=path;
      item.append(filename);list.append(item);
    }
    section.append(directory,list);images.append(section);
  }
  return images;
}
function renderHeaders(m) {
  const root=document.createDocumentFragment(),p=panel(t("请求与响应"));
  if(m.source!=="live")fact(p,t("数据来源"),t("历史重建（大小为估算）"));
  fact(p,t("模型"),m.model||t("未标记"));
  const endings={completed:"完成",failed:"失败",incomplete:"未完成",cancelled:"已取消",interrupted:"已中断"};
  const ended=m.response_summary?.status,code=Number.isInteger(m.status)&&m.status>=100&&m.status<=599?m.status:null;
  const relayFailed=["relay_error","client_disconnected","interrupted"].includes(m.state);
  const result=relayFailed?states[m.state]:code>=400?t("失败"):Object.hasOwn(endings,ended)?t(endings[ended]):m.state==="complete"?t("已结束"):states[m.state]||t("尚未收到");
  fact(p,t("结果"),result+(code!==null?" · HTTP "+code:""));
  fact(p,t("总耗时"),Number.isFinite(m.duration_ms)&&m.duration_ms>=0?nf.format(Math.round(m.duration_ms/100)/10)+" "+t("秒"):"—");
  if(typeof m.relay_error==="string"&&m.relay_error)fact(p,t("错误原因"),t(m.relay_error));
  const session=sessionSummaries.find(row=>row.thread_id===m.thread_id);
  const auxiliary=auxiliarySessionKind(session),purpose=purposeFromMetadata(m);
  const specialKind=[purpose,m.request_kind,auxiliary].find(kind=>kind&&kind!=="subagent"&&Object.hasOwn(auxiliaryLabels,kind));
  if(m.details_evicted)fact(p,t("请求详情"),t("已回收"));
  root.append(p);
  if(specialKind) {
    const context=panel(t("会话信息"));context.id="request-session-info";
    fact(context,"session_id",m.thread_id||"—").classList.add("request-context");
    const nativeTitle=session?.title_source==="index_task"||session?.title_source==="subagent"?null:session?.title;
    fact(context,t("会话名称"),nativeTitle||"—").classList.add("request-context");
    const parentId=session?.parent_thread_id||m.parent_thread_id;
    if(parentId&&parentId!==m.thread_id) {
      fact(context,"parent_session_id",parentId).classList.add("request-context");
      const parent=sessionSummaries.find(row=>row.thread_id===parentId);
      const parentTitle=parent?.title_source==="index_task"||parent?.title_source==="subagent"?null:parent?.title;
      fact(context,t("父会话名称"),parentTitle||"—").classList.add("request-context");
    }
    fact(context,t("任务类型"),t(auxiliaryLabels[specialKind]));
    root.append(context);
  }
  if(specialKind==="image_analysis") {
    const body=requestBodyView?.id===m.id?requestBodyView.body:null,paths=requestImagePaths(m,body);
    root.append(renderImageInfo(paths));
  }
  reconcileContent($("headers-content"),root);
}
async function setInterfaceLanguage(language,{persist=true}={}) {
  const changed=language!==uiI18n.language;
  uiI18n.setLanguage(language,{persist});
  if(!changed)return;
  const revision=++interfaceLanguageRevision;
  const buttons=document.querySelectorAll("#language-toggle button");
  for(const button of buttons)button.disabled=true;
  try {
    tokenTrendView?.render();
    if(document.documentElement.classList.contains("desktop-titlebar"))renderWindowState({enabled:true,maximized:document.documentElement.classList.contains("desktop-maximized")});
    state.listVersion="";state.sessionVersion="";
    renderStatus(true);renderContextPlaceholders();renderSettingsSaveProgress();renderUpdates();
    if($("connect-confirm-dialog").open)renderConnectPrompt();
    if($("exit-confirm-dialog").open)renderExitRequest();
    if(state.status) {
      if(!("packet_size_protection_enabled" in state.status))$("packet-size-message").textContent=t("当前中转暂不支持此功能。");
    }
    if(state.detail) {
      showTab(state.tab);renderOverview(state.detail);renderCompression(state.detail);renderHeaders(state.detail);
      const selected=state.selected, path=state.path, query=$("tree-query").value, searchVisible=!$("body-results").hidden;
      await loadTree(path);
      if(revision!==interfaceLanguageRevision)return;
      if(state.selected===selected && state.path===path && $("tree-query").value===path) {
        $("tree-query").value=query;
        if(searchVisible&&query.trim()&&!query.trim().startsWith("/"))await searchBody(query.trim());
      }
      await loadDiff();
      if(revision!==interfaceLanguageRevision)return;
    }
    if(state.status)await refreshFilters();
  } finally {if(revision===interfaceLanguageRevision){for(const button of buttons)button.disabled=settingsSaving;renderLanguageSelection();}}
}
for(const button of document.querySelectorAll("#language-toggle button"))action(button.id,async()=>{
  if(!settingsEditing||settingsSaving)return;
  settingsLanguage=button.dataset.language;renderLanguageSelection();updateSettingsSaveState();
  await setInterfaceLanguage(settingsLanguage,{persist:false});
});
action("settings-open",openSettings);action("empty-connect",openSettings);
function discardSettingsDraft() {
  if(!settingsEditing&&!settingsBaseline&&settingsLanguage===null)return;
  const language=settingsBaseline?.language??state.status?.ui_language??uiI18n.language;
  settingsEditing=false;settingsBaseline=null;settingsLanguage=null;
  const restore=setInterfaceLanguage(language,{persist:false});
  resetSettingsSaveProgress();
  if(state.status)fillSettings();if(contextSettings)fillContextSettings(contextSettings);renderUpdates();
  setSettingsMessage("");
  return restore.catch(fail);
}
function restoreSettings() {
  if(!settingsEditing||settingsSaving||$("settings-restart-dialog").open)return;
  discardSettingsDraft();
  settingsLanguage=uiI18n.language;settingsBaseline=readSettingsDraft();settingsEditing=true;
  renderLanguageSelection();renderConnectionActions();updateSettingsSaveState();
}
function closeSettings() {
  if(settingsSaving||$("settings-restart-dialog").open)return;
  closeZstdLevelPicker(false);
  $("settings-dialog").close();return discardSettingsDraft();
}
action("settings-restart-confirm",()=>{
  $("settings-restart-dialog").close();settingsRestartChanges.clear();closeSettings();
});
$("settings-restart-dialog").addEventListener("cancel",event=>event.preventDefault());
action("settings-close",closeSettings);action("settings-restore",restoreSettings);
for(const id of ["log-dir","cache-dir","image-save-dir"])action(id+"-open",()=>openSettingsDirectory(id));
for(const id of ["log-dir","cache-dir"])action(id+"-reset",()=>{
  if(settingsSaving||!state.status.storage_paths_supported)return;
  $(id).value=state.status.data_dir||"";updateSettingsSaveState();
});
$("settings-dialog").addEventListener("cancel",event=>{event.preventDefault();closeSettings();});
$("settings-dialog").addEventListener("close",()=>{
  if(!$("settings-dialog").open){closeZstdLevelPicker(false);discardSettingsDraft();}
});
for(const id of ["capacity-retry-count","capacity-retry-delay"])$(id).addEventListener("input",event=>{
  if(event.target.valueAsNumber>99)event.target.value="99";
});
$("settings-dialog").querySelector(".settings-content").addEventListener("input",()=>updateSettingsSaveState());
$("settings-dialog").querySelector(".settings-content").addEventListener("change",()=>updateSettingsSaveState());
$("zstd-level").addEventListener("change",fillZstdLevel);
$("image-zstd-level").addEventListener("change",fillZstdLevel);
action("zstd-level-open",()=>openZstdLevelPicker("conversation"));action("image-zstd-level-open",()=>openZstdLevelPicker("image"));action("zstd-level-close",()=>closeZstdLevelPicker());
$("zstd-level-options").addEventListener("click",event=>{
  const row=event.target.closest("tr[data-level]");if(row)selectZstdLevel(row.dataset.level);
});
$("zstd-level-dialog").addEventListener("keydown",navigateZstdLevels);
$("zstd-level-dialog").addEventListener("cancel",event=>{event.preventDefault();closeZstdLevelPicker();});
$("zstd-level-dialog").addEventListener("close",()=>{for(const id of ["zstd-level-open","image-zstd-level-open"])$(id).setAttribute("aria-expanded","false");});
$("zstd-level-dialog").addEventListener("click",event=>{
  const dialog=$("zstd-level-dialog"),rect=dialog.getBoundingClientRect();
  if(event.target===dialog&&(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom))closeZstdLevelPicker();
});
$("image-analysis-mode").addEventListener("change",()=>{fillImageAnalysisMode();fillImageDirectory();});
action("settings-save",saveSettings);
action("ui-refresh",()=>{if(state.status?.ui_live_reload)location.reload();});
action("updates-check",()=>performUpdate("check"));action("updates-install",()=>performUpdate("install"));
setInterval(()=>{if($("settings-dialog").open&&!settingsSaving&&!updateBusy&&["checking","downloading","upgrading"].includes(updateStatus?.state))loadUpdates();},2000);
for(const button of document.querySelectorAll("#request-compression button"))action(button.id,()=>requestCompressionChange(button.dataset.compression));
action("compression-confirm-cancel",cancelCompressionChange);action("compression-confirm-apply",confirmCompressionChange);
$("compression-confirm-dialog").addEventListener("cancel",event=>{event.preventDefault();cancelCompressionChange();});
$("compression-confirm-dialog").addEventListener("close",()=>{if(!$("compression-confirm-dialog").open)compressionTarget=null;});
action("exit-confirm-cancel",()=>confirmExit("cancel"));action("exit-confirm-direct",()=>confirmExit("direct"));action("exit-confirm-restore",()=>confirmExit("restore"));
$("exit-confirm-dialog").addEventListener("cancel",event=>{event.preventDefault();confirmExit("cancel");});
action("connect-confirm-cancel",cancelConnectPrompt);action("connect-confirm-apply",confirmConnection);
$("connect-confirm-dialog").addEventListener("cancel",event=>{event.preventDefault();cancelConnectPrompt();});
function fillPacketProtection() {
  const s=state.status,supported="packet_size_protection_enabled" in s,automatic=$("auto-zstd-level"),autoSupported=typeof s.auto_zstd_level_enabled==="boolean";
  if(!settingsEditing){$("packet-size-protection").checked=!!s.packet_size_protection_enabled;$("budget").value=s.budget_bytes/1000;automatic.checked=autoSupported&&s.auto_zstd_level_enabled;}
  if(!$("packet-size-protection").checked)automatic.checked=false;
  $("budget").min=(s.packet_size_min_bytes||50001)/1000;$("budget").max=(s.packet_size_max_bytes||134217728)/1000;
  $("packet-size-protection").disabled=settingsSaving||!supported;
  $("budget").disabled=$("packet-size-protection").disabled||!$("packet-size-protection").checked;
  automatic.disabled=$("packet-size-protection").disabled||!$("packet-size-protection").checked||!autoSupported;
  automatic.closest(".runtime-toggle").title=!autoSupported?t("当前中转暂不支持按请求大小自动压缩。"):!$("packet-size-protection").checked?t("请先开启请求大小保护"):"";
  $("packet-size-message").hidden=supported;
  if(!supported)$("packet-size-message").textContent=t("当前中转暂不支持此功能。");
  fillZstdLevel();
}
$("packet-size-protection").addEventListener("change",fillPacketProtection);
$("auto-zstd-level").addEventListener("change",fillZstdLevel);
for(const key of ["context","compact"]) {
  const input=$(key+"-tokens"),useDefault=$(key+"-default");
  input.closest(".context-value").addEventListener("click",()=>{
    editContextValue(key);if(!input.disabled)input.focus();
  });
  input.addEventListener("input",()=>contextMessage(""));
  useDefault.addEventListener("change",()=>{
    if(!useDefault.checked)input.value=contextCustomValues.get(key)||"";
    syncContextDefaults();contextMessage("");
  });
}
action("image-save-dir-reset",()=>{
  if(settingsSaving||!state.status.image_save_dir_supported||selectedImageAnalysisMode()!=="optimized")return;
  $("image-save-dir").value=imageDefaultDirectory();updateSettingsSaveState();
});
async function applyLinkAction(which) {
  linkRevision++;
  try {
    if(which==="activate") {
      const current=await api("/api/status");state.status={...state.status,...current};
      if(current.profile?.active&&!current.profile.config_error&&!current.provider_sync_error)
        return {active:true,changed:false,reload_required:false};
    }
    const result=await api("/api/link",{action:which,...(which==="activate"?{upstream:state.status.upstream}:{})});
    const status=await api("/api/status");state.status={...state.status,...status};
    if(which==="activate"&&(!status.profile.active||status.profile.config_error||status.provider_sync_error))
      throw Error("接入未完成，请检查 Codex 配置后重试。");
    if(which==="restore"&&status.profile.active)throw Error("恢复未完成，请重试。");
    return result;
  } finally {linkRevision++;}
}
async function linkAction(which) {
  const s=state.status;
  if(linkSaving||settingsSaving||compressionSaving||!s)return;
  if(which==="activate"?(s.profile.active||s.profile.config_error):!s.profile.can_restore)return;
  linkSaving=which;renderConnectionActions();updateSettingsSaveState(false);
  try {
    const result=await applyLinkAction(which);renderStatus(true);
    return result;
  } finally {linkSaving="";renderConnectionActions();updateSettingsSaveState(false);}
}
$("session-search").addEventListener("input",renderSessions);
$("packet-list").addEventListener("scroll",loadMorePackets,{passive:true});
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
$("tree-query").addEventListener("input",scheduleTreeQuery);
$("tree-query").addEventListener("compositionend",scheduleTreeQuery);
$("tree-query").addEventListener("keydown",e=>{if(e.key==="Enter" && !e.isComposing){e.preventDefault();Promise.resolve().then(submitTreeQuery).catch(fail);}});
function isTrendGeneration(packet) {
  const kinds=[purposeFromMetadata(packet),packet.request_purpose,packet.request_kind,packet.session_kind];
  return hasModelTokenUsage(packet)&&kinds.every(kind=>!kind||["turn","generation","subagent"].includes(kind));
}
class TokenTrendView {
  constructor(services) {
    this.services=services;this.active=false;this.loading=false;this.hydrating=false;
    this.packets=new Map();this.native=new Map();this.hours=1;this.windowEnd=null;this.loadedBounds=null;this.requestBounds=null;this.windowTimer=null;this.previewPackets=null;this.oldestCreated=null;this.boundsReady=false;this.paintRefresh=null;
    this.revision=null;this.lastFull=0;this.query="";this.error=false;this.limited=false;
    this.series=[];this.hover=null;this.hoverChart=null;this.hoverSeries=null;this.hoverEvent=null;this.focusEvent=null;this.frame=null;this.legendVersion="";
    this.charts=[{key:"size",field:"size",prefix:"trend-size"},{key:"token",field:"input",prefix:"trend"}].map(chart=>({
      ...chart,svg:$(chart.prefix+"-chart"),plot:$(chart.prefix+"-plot"),tooltip:$(chart.prefix+"-tooltip"),empty:$(chart.prefix+"-empty"),points:[],eventNodes:[],eventKey:null,bounds:null,focusLayer:null
    }));
    this.tooltipHost=$("trend-charts");
    this.timeAxis=$("trend-time-axis");this.guide=$("trend-focus");this.timeTooltip=$("trend-time-tooltip");
    for(const icon of document.querySelectorAll("[data-trend-marker]")) {
      const key=icon.dataset.trendMarker,shape=TokenTrendView.markerShape(key);if(!shape)continue;
      const {tag,...attrs}=shape;icon.replaceChildren(this.svgNode(tag,{...attrs,class:"trend-event trend-event-"+key}));
    }
    this.eventLegend=["predictive","overflow","retry","compact"].map(key=>({key,button:$("trend-event-"+key)}));
    for(const {key,button} of this.eventLegend) {
      const highlight=(field,value)=>{this[field]=value;this.hover=null;this.hoverChart=null;this.hoverSeries=null;this.emphasis=null;this.renderFocus();};
      button.onpointerenter=()=>highlight("hoverEvent",key);button.onpointerleave=()=>highlight("hoverEvent",null);
      button.onfocus=()=>highlight("focusEvent",key);button.onblur=()=>highlight("focusEvent",null);
    }
    this.range=$("trend-range-slider");
    this.statusRange();this.updateRange();
    this.range.oninput=()=>this.seek(Number(this.range.value));
    this.tooltipHost.addEventListener("wheel",event=>this.navigateWheel(event),{passive:false});
    $("trend-range").addEventListener("wheel",event=>this.navigateWheel(event,true),{passive:false});
    $("trend-search").oninput=event=>{this.query=event.currentTarget.value.trim().toLowerCase();this.hover=null;this.hoverChart=null;this.hoverSeries=null;this.emphasis=null;this.render();};
    this.resize=new ResizeObserver(()=>this.scheduleRender());
    for(const chart of this.charts) {
      this.resize.observe(chart.plot);
      chart.svg.onpointermove=event=>this.pointAt(event,chart);
      chart.svg.onpointerleave=()=>{this.hover=null;this.hoverChart=null;this.hoverSeries=null;this.renderFocus();};
    }
    // SVG redraws can drop pointerleave when the hovered child is replaced.
    document.addEventListener("pointermove",event=>{
      if(this.active&&this.hoverChart&&!this.charts.some(chart=>chart.svg.contains(event.target))) {
        this.hover=null;this.hoverChart=null;this.hoverSeries=null;this.renderFocus();
      }
    },{passive:true});
  }
  static events(packet) {
    const result=[];
    if(packet.usage_override_applied===true)result.push("predictive");
    if(packet.overflow_compaction_calls>0||packet.overflow_cache_hit===true||String(packet.relay_error||"").startsWith("overflow_"))result.push("overflow");
    if(packet.native_retry_advised===true||packet.capacity_retry_count>0)result.push("retry");
    if(packet.is_compaction)result.push("compact");
    return result;
  }
  static markerShape(key) {
    if(key==="predictive")return {tag:"path",d:"M0,-4.8L4.8,0L0,4.8L-4.8,0Z"};
    if(key==="overflow")return {tag:"path",d:"M0,-4.8L4.8,4.8L-4.8,4.8Z"};
    if(key==="retry")return {tag:"circle",cx:0,cy:0,r:4};
    if(key==="compact")return {tag:"rect",x:-4,y:-4,width:8,height:8};
    return null;
  }
  static input(packet,data) {
    return sortInputTokens(requestTokenUsage(packet,data));
  }
  static packet(packet) {
    // Older backends return full metadata. Keep only fields used by the charts and native usage lookup.
    const result={};
    for(const key of ["id","created","thread_id","method","path","state","status","duration_ms","is_compaction","request_kind","request_purpose","session_kind","image_isolation_worker","usage_override_applied","usage_override_request_bytes","overflow_compaction_calls","overflow_cache_hit","overflow_original_wire_bytes","relay_error","capacity_retry_count","native_retry_advised","outbound_wire_bytes"])
      if(Object.hasOwn(packet,key))result[key]=packet[key];
    if(packet.upstream_usage&&Object.hasOwn(packet.upstream_usage,"input_tokens"))result.upstream_usage={input_tokens:packet.upstream_usage.input_tokens};
    if(packet.request_token_estimate)result.request_token_estimate=packet.request_token_estimate;
    return result;
  }
  static size(packet) {return compressedBytes(packet);}
  static sizeLabel(bytes) {return bytes>=1000&&bytes<1000000?Math.round(bytes/1000)+" KB":compact(bytes);}
  static sizeScale(maximum) {
    return TokenTrendView.scale(maximum||1000);
  }
  static scale(maximum) {
    const rough=(maximum||1000)/5,power=10**Math.floor(Math.log10(rough));
    const step=[1,2,2.5,5,10].map(value=>value*power).find(value=>value>=rough)||power*10;
    return {step,max:Math.max(step,Math.ceil(maximum/step)*step)};
  }
  static color(id) {let hash=2166136261;for(const char of id)hash=Math.imul(hash^char.charCodeAt(0),16777619);return "hsl("+((hash>>>0)%360)+" 62% 42%)";}
  static timeTicks(start,end,width) {
    const target=(end-start)/Math.max(2,Math.min(8,Math.floor(width/100)));
    const step=[1,2,5,10,15,30,60,120,300,600,1200,1800,3600,7200,10800,21600,43200].find(value=>value>=target)||43200;
    const day=new Date(start*1000);day.setHours(0,0,0,0);const origin=day.getTime()/1000,ticks=[];
    for(let stamp=origin+Math.ceil((start-origin)/step)*step;stamp<=end;stamp+=step)ticks.push(stamp);
    return ticks;
  }
  static compactTimeScale(start,end,activity,coverage={start,end}) {
    const intervals=activity.filter(([a,b])=>Number.isFinite(a)&&Number.isFinite(b)&&b>=a&&a-60<=coverage.end&&b+60>=coverage.start)
      .map(([a,b])=>[Math.max(coverage.start,a-60),Math.min(coverage.end,b+60)]).sort((a,b)=>a[0]-b[0]);
    // Keep one global time coordinate: each confirmed long idle interval retains one minute.
    const gaps=[],addGap=(a,b)=>{
      const from=Math.max(start,a),to=Math.min(end,b);
      if(b-a>=600&&to>from)gaps.push({start:from,end:to,length:60*(to-from)/(b-a)});
    };
    let cursor=coverage.start;
    for(const [a,b]of intervals){addGap(cursor,a);cursor=Math.max(cursor,b);}
    if(intervals.length)addGap(cursor,coverage.end);
    const segments=[];let virtual=0;cursor=start;
    for(const gap of gaps) {
      if(gap.start>cursor){segments.push({start:cursor,end:gap.start,a:virtual,b:virtual+gap.start-cursor});virtual+=gap.start-cursor;}
      const gapUnits=gap.length;
      gap.a=virtual;gap.b=virtual+gapUnits;segments.push({...gap,compressed:true});virtual+=gapUnits;cursor=gap.end;
    }
    if(cursor<end){segments.push({start:cursor,end,a:virtual,b:virtual+end-cursor});virtual+=end-cursor;}
    const locate=(value,key)=>{
      let lo=0,hi=segments.length-1;
      while(lo<hi){const mid=(lo+hi)>>1;if(segments[mid][key]<value)lo=mid+1;else hi=mid;}
      return segments[lo];
    };
    const offset=value=>{
      value=Math.max(start,Math.min(end,value));const part=locate(value,"end");
      return part.a+(value-part.start)/(part.end-part.start)*(part.b-part.a);
    };
    const at=value=>{
      value=Math.max(0,Math.min(virtual,value));const part=locate(value,"b");
      return part.start+(value-part.a)/(part.b-part.a)*(part.end-part.start);
    };
    return {start,end,length:virtual,offset,at,gaps,segments};
  }
  timeScale(now=Date.now()/1000) {
    const packets=this.previewPackets||this.packets,bounds=this.timeline(now),cached=this.axisCache;
    if(cached&&cached.packets===packets&&cached.size===packets.size&&cached.revision===this.revision&&cached.loaded===this.loadedBounds&&cached.limited===this.limited&&cached.oldest===this.oldestCreated&&Math.floor(cached.now)===Math.floor(now))return cached.scale;
    const activity=[];let first=now;
    for(const packet of packets.values()) {
      if(!Number.isFinite(packet.created))continue;first=Math.min(first,packet.created);
      if(!packet.is_compaction&&!this.services.isGeneration(packet))continue;
      const duration=Number.isFinite(packet.duration_ms)&&packet.duration_ms>=0?packet.duration_ms/1000:0;
      activity.push([packet.created,["captured","streaming"].includes(packet.state)?now:packet.created+duration]);
    }
    const loaded=this.loadedBounds,coverage=loaded?{start:Math.max(bounds.start,loaded.since,first),end:Math.min(now,loaded.before??now)}:{start:bounds.start,end:bounds.start};
    const scale=TokenTrendView.compactTimeScale(bounds.start,bounds.end,activity,coverage);
    this.axisCache={packets,size:packets.size,revision:this.revision,loaded,limited:this.limited,oldest:this.oldestCreated,now,scale};return scale;
  }
  chartAxis(view,width) {
    const {start,end,scale,startOffset,duration}=view;
    const fraction=stamp=>(scale.offset(stamp)-startOffset)/duration,timeAt=value=>scale.at(startOffset+Math.max(0,Math.min(1,value))*duration);
    const gaps=scale.gaps.filter(gap=>gap.end>start&&gap.start<end);
    let ticks=TokenTrendView.timeTicks(start,end,width);
    if(gaps.length) {
      const candidates=scale.segments.filter(part=>!part.compressed&&part.end>start&&part.start<end).flatMap(part=>{
        const a=Math.max(start,part.start),b=Math.min(end,part.end);
        return [a,...TokenTrendView.timeTicks(a,b,(fraction(b)-fraction(a))*width),b];
      });
      ticks=[];
      for(const stamp of [...new Set(candidates)].sort((a,b)=>a-b))if(!ticks.length||(fraction(stamp)-fraction(ticks.at(-1)))*width>=80)ticks.push(stamp);
    }
    return {fraction,timeAt,gaps,ticks};
  }
  rangeLimit() {return !this.boundsReady?1:Math.min(12,Math.max(1,this.timeScale().length/3600));}
  timeline(now=Date.now()/1000) {return {start:Math.min(now-3600,this.boundsReady&&Number.isFinite(this.oldestCreated)?this.oldestCreated-1:now-3600),end:now};}
  viewport(now=Date.now()/1000) {
    const scale=this.timeScale(now),duration=Math.min(scale.length,Math.max(1,Math.min(12,this.hours||1))*3600);
    const endOffset=Math.max(duration,Math.min(scale.length,Number.isFinite(this.windowEnd)?scale.offset(this.windowEnd):scale.length)),startOffset=endOffset-duration;
    return {start:scale.at(startOffset),end:scale.at(endOffset),startOffset,endOffset,duration,scale};
  }
  covers(bounds,view=this.viewport()) {return !!bounds&&view.start>=bounds.since-.01&&(bounds.before===null||view.end<bounds.before+.01);}
  queryWindow(now=Date.now()/1000) {
    const count=this.services.status()?.storage?.count;
    if((Number.isSafeInteger(count)&&count<=20000)||(!Number.isSafeInteger(count)&&!this.segmented))return {since:this.timeline(now).start,before:null};
    const view=this.viewport(now),end=Math.min(now,view.end+Math.max(0,(12*3600-(view.end-view.start))/2));
    return {since:Math.min(view.start,end-12*3600),before:end>=now?null:end+.001};
  }
  navigateWheel(event,onRange=false) {
    if(!this.active||event.metaKey)return;
    const raw=Math.abs(event.deltaX||0)>Math.abs(event.deltaY||0)?event.deltaX:event.deltaY;
    if(!Number.isFinite(raw)||!raw)return;
    if(!this.boundsReady){if(event.ctrlKey)event.preventDefault();return;}
    event.preventDefault();const delta=raw*(event.deltaMode===1?16:event.deltaMode===2?400:1);
    if(event.ctrlKey) {
      let fraction=.5,anchorTime=null;
      if(Number.isFinite(event.clientX)) {
        if(onRange) {
          const rect=this.range.getBoundingClientRect(),width=rect.width,view=this.viewport();
          const thumb=Math.min(width,Math.max(12,width*view.duration/view.scale.length));
          fraction=(event.clientX-rect.left-(width-thumb)*Number(this.range.value))/Math.max(1,thumb);
        }else {
          const chart=this.charts[0],rect=chart.svg.getBoundingClientRect(),bounds=chart.bounds;
          if(bounds&&rect.width) {
            fraction=((event.clientX-rect.left)*bounds.width/rect.width-bounds.left)/(bounds.width-bounds.left-bounds.right);
            anchorTime=bounds.axis.timeAt(fraction);
          }
        }
      }
      this.setRange(this.hours*Math.exp(Math.max(-240,Math.min(240,delta))/450),Math.max(0,Math.min(1,fraction)),anchorTime);
    }else {
      const bounds=this.charts[0].bounds,width=bounds?bounds.width-bounds.left-bounds.right:500;
      this.pan(Math.max(-1,Math.min(1,delta/Math.max(1,width)))*this.viewport().duration);
    }
  }
  statusRange() {
    const storage=this.services.status()?.storage;
    if(!storage||!Object.hasOwn(storage,"oldest_created"))return false;
    const oldest=storage.oldest_created;
    if(oldest!==null&&(!Number.isFinite(oldest)||oldest<0))return false;
    this.oldestCreated=oldest??Date.now()/1000;this.boundsReady=true;return true;
  }
  async loadRange(controller,full) {
    if(!this.statusRange()&&(!this.boundsReady||full)) {
      // Legacy backends already support indexed ascending pagination: one row establishes the range.
      try {
        const rows=await this.services.api("/api/packets?limit=1&sort=created&direction=asc&view=trend",undefined,controller.signal);
        controller.signal.throwIfAborted();if(!this.active||this.controller!==controller)return false;
        if(!Array.isArray(rows)||(rows.length&&(!Number.isFinite(rows[0]?.created)||rows[0].created<0)))throw Error("invalid trend range");
        this.oldestCreated=rows.length?rows[0].created:Date.now()/1000;this.boundsReady=true;
      }catch(error){controller.signal.throwIfAborted();if(!this.boundsReady)throw error;}
    }
    if(this.range)this.updateRange();this.scheduleRender();return true;
  }
  updateRange() {
    const maximum=this.rangeLimit();this.hours=Math.max(1,Math.min(maximum,Number.isFinite(this.hours)?this.hours:1));
    const view=this.viewport(),duration=view.duration,travel=view.scale.length-duration;
    if(Number.isFinite(this.windowEnd))this.windowEnd=view.end;
    this.range.disabled=!this.boundsReady;this.range.setAttribute("aria-busy",String(!this.boundsReady));
    this.range.max="1";this.range.value=String(travel>0?Math.max(0,Math.min(1,view.startOffset/travel)):1);
    const width=this.range.clientWidth||180;
    this.range.style.setProperty("--trend-thumb-width",Math.min(width,Math.max(12,width*duration/view.scale.length))+"px");
    const label=t("{hours} 小时").replace("{hours}",nf.format(Math.round(this.hours*100)/100));
    this.range.setAttribute("aria-valuetext",this.time(view.start)+" – "+this.time(view.end)+" · "+label+(view.scale.gaps.length?" · "+t("已跳过空档"):""));
  }
  navigate(end,scale=this.timeScale()) {
    const duration=Math.min(scale.length,this.hours*3600);
    end=Math.max(duration,Math.min(scale.length,end));this.windowEnd=end>=scale.length-.001?null:scale.at(end);
    this.hover=null;this.hoverChart=null;this.hoverSeries=null;this.updateRange();this.scheduleRender();this.ensureWindow();
  }
  setRange(hours,fraction=.5,anchorTime=null) {
    if(!this.boundsReady||!Number.isFinite(hours))return;
    const view=this.viewport(),next=Math.max(1,Math.min(this.rangeLimit(),hours));if(Math.abs(next-this.hours)<1e-9)return;
    fraction=Number.isFinite(fraction)?Math.max(0,Math.min(1,fraction)):.5;
    const anchor=Number.isFinite(anchorTime)?view.scale.offset(anchorTime):view.startOffset+view.duration*fraction,duration=Math.min(view.scale.length,next*3600);
    this.hours=next;this.navigate(anchor+(1-fraction)*duration,view.scale);
  }
  pan(seconds) {if(this.boundsReady&&Number.isFinite(seconds)&&seconds){const view=this.viewport();this.navigate(view.endOffset+seconds,view.scale);}}
  seek(position) {
    if(!this.boundsReady||!Number.isFinite(position))return;
    const view=this.viewport();
    this.navigate(view.duration+Math.max(0,Math.min(1,position))*(view.scale.length-view.duration),view.scale);
  }
  ensureWindow() {
    if(!this.active||this.loading&&!this.requestBounds)return;
    if(this.loading&&!this.covers(this.requestBounds))this.cancelPending();
    clearTimeout(this.windowTimer);this.windowTimer=null;
    if(this.covers(this.loadedBounds)||this.covers(this.requestBounds))return;
    this.windowTimer=setTimeout(()=>{this.windowTimer=null;this.refresh(true);},120);
  }
  cancelPending() {this.controller?.abort();this.nativeController?.abort();clearTimeout(this.windowTimer);this.windowTimer=null;this.controller=null;this.nativeController=null;this.requestBounds=null;this.paintRefresh=null;this.previewPackets=null;this.loading=false;this.hydrating=false;}
  setActive(active) {
    this.active=active;this.hover=null;this.hoverChart=null;this.hoverSeries=null;this.hoverEvent=null;this.focusEvent=null;this.emphasis=null;
    if(!active){this.cancelPending();this.guide.replaceChildren();this.timeTooltip.hidden=true;for(const chart of this.charts)chart.tooltip.hidden=true;}
    else {
      this.render();const ticket={};this.paintRefresh=ticket;
      requestAnimationFrame(()=>setTimeout(()=>{if(this.active&&this.paintRefresh===ticket){this.paintRefresh=null;this.refresh();}},0));
    }
  }
  scheduleRender() {if(!this.active||this.frame!==null)return;this.frame=requestAnimationFrame(()=>{this.frame=null;this.render();});}
  async refresh(force=false) {
    if(!this.active||document.hidden||this.loading||this.paintRefresh)return;
    const revision=this.services.status()?.storage?.revision,now=Date.now();
    let full=force||!this.lastFull||now-this.lastFull>=30000||!this.covers(this.loadedBounds);
    if(!full&&revision===this.revision){this.render();this.hydrate();return;}
    const controller=new AbortController();this.controller=controller;this.loading=true;this.error=false;
    clearTimeout(this.windowTimer);this.windowTimer=null;
    const timeout=setTimeout(()=>controller.abort(),60000),incoming=new Map();
    const limit=this.services.status()?.trend_packets_supported?300:50,maximum=20000;let published=0;
    try {
      if(!await this.loadRange(controller,full))return;
      if(!this.covers(this.loadedBounds))full=true;
      const queryBounds=full?this.queryWindow(now/1000):this.loadedBounds,{since,before}=queryBounds;this.requestBounds=queryBounds;
      let after=null,limited=false;
      for(let page=0;page<Math.ceil(maximum/limit);page++) {
        const query=new URLSearchParams({limit:String(limit),sort:"created",direction:"desc",view:"trend",since:String(since)});if(after)query.set("after",after);
        if(before!==null)query.set("before",String(before));
        const values=await this.services.api("/api/packets?"+query,undefined,controller.signal);
        controller.signal.throwIfAborted();if(!Array.isArray(values))throw Error("invalid trend data");
        for(const packet of values)if(packet?.id&&Number.isFinite(packet.created)&&packet.created>=since&&(before===null||packet.created<before))incoming.set(packet.id,TokenTrendView.packet(packet));
        if(!published||Date.now()-published>=100) {
          if(!this.active||this.controller!==controller)return;
          this.previewPackets=new Map([...this.packets,...incoming]);this.scheduleRender();published=Date.now();
          await new Promise(resolve=>setTimeout(resolve,0));controller.signal.throwIfAborted();
        }
        if(values.length<limit||values.at(-1).created<since)break;
        if(!full&&values.some(packet=>this.packets.has(packet.id)))break;
        const next=values.at(-1).id;if(next===after)throw Error("invalid trend cursor");after=next;
        if(page===Math.ceil(maximum/limit)-1)limited=true;
      }
      if(!full) {
        const pending=[...this.packets.values()].filter(packet=>["captured","streaming"].includes(packet.state)&&!incoming.has(packet.id));
        const offset=this.activeCursor||0,jobs=pending.length?Array.from({length:Math.min(12,pending.length)},(_,i)=>pending[(offset+i)%pending.length]):[];
        let at=0;await Promise.all(Array.from({length:Math.min(3,jobs.length)},async()=>{
          while(at<jobs.length){const packet=jobs[at++];try{const value=await this.services.api("/api/packets/"+packet.id+"?view=trend",undefined,controller.signal);if(value?.id)incoming.set(value.id,TokenTrendView.packet(value));}catch(error){controller.signal.throwIfAborted();}}
        }));this.activeCursor=offset+jobs.length;
      }
      controller.signal.throwIfAborted();if(!this.active||this.controller!==controller)return;
      if(full){
        this.packets=incoming;this.loadedBounds=limited&&incoming.size?{...queryBounds,since:Math.min(...[...incoming.values()].map(packet=>packet.created))}:queryBounds;
        this.lastFull=now;this.limited=limited;this.segmented=limited;
      }else for(const [id,packet] of incoming)this.packets.set(id,packet);
      for(const [id,packet] of this.packets)if(packet.created<since||(before!==null&&packet.created>=before))this.packets.delete(id);
      if(this.packets.size>maximum){
        const keep=[...this.packets.values()].sort((a,b)=>b.created-a.created||b.id.localeCompare(a.id)).slice(0,maximum);
        this.packets=new Map(keep.map(packet=>[packet.id,packet]));this.limited=true;this.segmented=true;
        this.loadedBounds={...this.loadedBounds,since:Math.max(this.loadedBounds.since,keep.at(-1).created)};
      }
      for(const id of this.native.keys())if(!this.packets.has(id))this.native.delete(id);
      this.axisCache=null;this.revision=revision;this.error=false;
    } catch(error) {if(this.active&&this.controller===controller)this.error=true;}
    finally {
      clearTimeout(timeout);if(this.controller===controller){this.loading=false;this.controller=null;this.requestBounds=null;this.previewPackets=null;if(this.active){this.render();this.hydrate();}}
    }
  }
  tokenData(packet) {
    const direct=this.services.peek(packet);if(direct?.state==="available")return direct;
    const cached=this.native.get(packet.id);return cached?.version===tokenVersion(packet)?cached.data:null;
  }
  input(packet) {return TokenTrendView.input(packet,this.tokenData(packet));}
  hydrate() {
    if(!this.active||document.hidden||this.hydrating)return;
    const now=Date.now(),jobs=[...this.packets.values()].filter(packet=>{
      if(!packet.thread_id||!this.services.isGeneration(packet)||packet.usage_override_applied&&!overflowApplied(packet)||packet.status!==200||!["streaming","complete"].includes(packet.state)||this.input(packet)!==null)return false;
      const cached=this.native.get(packet.id);return !cached||cached.version!==tokenVersion(packet)||cached.expires<=now;
    }).sort((a,b)=>b.created-a.created).slice(0,8);
    if(!jobs.length)return;
    const controller=new AbortController();this.nativeController=controller;this.hydrating=true;let at=0;
    this.hydration=Promise.all(Array.from({length:Math.min(2,jobs.length)},async()=>{
      while(at<jobs.length&&!controller.signal.aborted) {
        const packet=jobs[at++],version=tokenVersion(packet);let data;
        try {data=await this.services.read(packet,controller.signal);}catch(error){if(controller.signal.aborted)return;data={state:"unavailable"};}
        if(controller.signal.aborted)return;
        if(!data||typeof data!=="object")data={state:"unavailable"};
        if(this.packets.has(packet.id)&&tokenVersion(this.packets.get(packet.id))===version)this.native.set(packet.id,{version,data,expires:Date.now()+(data.state==="available"?300000:["loading","pending"].includes(data.state)?1000:30000)});
      }
    })).finally(()=>{if(this.nativeController===controller){this.hydrating=false;this.nativeController=null;this.scheduleRender();}});
    return this.hydration;
  }
  model() {
    const names=new Map(this.services.sessions().map(row=>[row.thread_id,row.title||row.task_name||row.thread_id]));
    const groups=new Map();let unknown=0;
    const view=this.viewport(),now=view.end,since=view.start;
    for(const packet of (this.previewPackets||this.packets).values()) {
      if(!Number.isFinite(packet.created)||(!packet.is_compaction&&!this.services.isGeneration(packet)))continue;
      if(packet.created<since||packet.created>now)continue;
      if(typeof packet.thread_id!=="string"||!packet.thread_id){unknown++;continue;}
      if(!groups.has(packet.thread_id))groups.set(packet.thread_id,{id:packet.thread_id,title:names.get(packet.thread_id)||packet.thread_id,color:TokenTrendView.color(packet.thread_id),points:[],latest:0});
      const group=groups.get(packet.thread_id);group.latest=Math.max(group.latest,packet.created);
      group.points.push({packet,input:packet.is_compaction?null:this.input(packet),size:TokenTrendView.size(packet),events:TokenTrendView.events(packet)});
    }
    const series=[...groups.values()].sort((a,b)=>b.latest-a.latest||a.id.localeCompare(b.id));
    for(const group of series)group.points.sort((a,b)=>a.packet.created-b.packet.created||a.packet.id.localeCompare(b.packet.id));
    const matched=series.filter(group=>!this.query||(group.title+" "+group.id).toLowerCase().includes(this.query));
    return {series,matched,visible:matched,unknown,now,view};
  }
  svgNode(tag,attrs={},text) {const node=document.createElementNS("http://www.w3.org/2000/svg",tag);for(const [name,value]of Object.entries(attrs))node.setAttribute(name,String(value));if(text!==undefined)node.textContent=text;return node;}
  time(value,withSeconds=false) {
    const date=new Date(value*1000),locale=uiI18n.language==="en"?"en-GB":"zh-CN";
    return date.toLocaleString(locale,{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",...(withSeconds?{second:"2-digit"}:{})});
  }
  renderLegend(model) {
    const signature=JSON.stringify([uiI18n.language,this.query,model.matched.map(group=>[group.id,group.title])]);
    if(signature===this.legendVersion)return;this.legendVersion=signature;
    const fragment=document.createDocumentFragment();
    for(const group of model.matched) {
      const item=el("div","trend-series-item"),swatch=el("span","trend-series-swatch"),label=el("span","",group.title);
      item.dataset.renderKey="trend-series:"+group.id;item.style.setProperty("--series-color",group.color);
      item.setAttribute("role","listitem");item.title=group.title+"\n"+group.id;
      item.onpointerenter=()=>{this.emphasis=group.id;this.renderFocus();};item.onpointerleave=()=>{this.emphasis=null;this.renderFocus();};
      item.append(swatch,label);fragment.append(item);
    }
    reconcileContent($("trend-series"),fragment);
  }
  eventKey() {return this.hoverEvent||this.focusEvent||null;}
  marker(point,layer) {
    const events=point.events.filter(value=>["overflow","compact","predictive","retry"].includes(value));
    const key=["overflow","compact","predictive","retry"].find(value=>events.includes(value)),shape=TokenTrendView.markerShape(key);
    if(!shape)return;
    const {tag,...attrs}=shape,x=point.x,y=point.y;
    const selected=this.eventKey(),node=this.svgNode("g",{class:"trend-event-node"+(selected?(events.includes(selected)?" highlighted":" dimmed"):""),"data-events":events.join(" "),"data-packet":point.packet.id,transform:`translate(${x} ${y})`});
    node.append(this.svgNode(tag,{...attrs,class:"trend-event trend-event-"+key,stroke:point.series.color}));
    if(key!=="overflow"&&events.length>1)node.append(this.svgNode("text",{x:6,y:-6,class:"trend-event-count",fill:point.series.color},"+"+(events.length-1)));
    layer.append(node);return node;
  }
  render() {
    if(!this.active)return;
    this.updateRange();const model=this.model();this.series=model.series;this.renderLegend(model);
    if(!model.visible.some(group=>group.id===this.hoverSeries&&group.points.some(point=>point.input!==null||point.size!==null)))this.hoverSeries=null;
    const emphasis=this.eventKey()?null:this.emphasis||this.hoverSeries||(this.hover?model.visible.find(group=>group.points.some(point=>point.packet.id===this.hover&&(point.input!==null||point.size!==null)))?.id:null);
    const {start,end}=model.view;
    const axis=this.chartAxis(model.view,Math.max(1,this.charts[0].plot.clientWidth-132));
    for(const chart of this.charts)this.renderChart(chart,model,start,end,emphasis,axis);
    this.renderTimeAxis();
    this.renderFocus();
  }
  renderChart(chart,model,start,end,emphasis,axis) {
    const width=chart.plot.clientWidth,height=chart.plot.clientHeight;
    if(!width||!height){chart.points=[];chart.eventNodes=[];chart.bounds=null;return;}
    chart.eventKey=this.eventKey();chart.svg.classList.toggle("trend-event-filtering",!!chart.eventKey);chart.eventNodes=[];
    const left=104,right=28,top=30,bottom=12,plotWidth=Math.max(1,width-left-right),plotHeight=Math.max(1,height-top-bottom);
    const known=model.visible.flatMap(group=>group.points).filter(point=>point[chart.field]!==null);
    const budget=this.services.status()?.budget_bytes,limit=chart.key==="size"&&Number.isSafeInteger(budget)&&budget>0?budget:null;
    const maximum=Math.max(limit??0,...known.map(point=>point[chart.field])),scale=chart.key==="size"?TokenTrendView.sizeScale(maximum):TokenTrendView.scale(maximum);
    const x=value=>left+axis.fraction(value)*plotWidth,y=value=>top+plotHeight-value/scale.max*plotHeight;
    const ticks=axis.ticks;
    chart.bounds={width,height,left,right,top,bottom,start,end,x,ticks,axis};
    const root=document.createDocumentFragment();chart.svg.setAttribute("viewBox",`0 0 ${width} ${height}`);
    root.append(this.svgNode("text",{x:36,y:top+plotHeight/2,transform:`rotate(-90 36 ${top+plotHeight/2})`,"text-anchor":"middle","dominant-baseline":"central",class:"trend-axis-title"},t(chart.key==="size"?"请求大小":"输入 Token")));
    for(let value=0;value<=scale.max+scale.step*.01;value+=scale.step) {
      const py=y(value),label=chart.key==="size"?TokenTrendView.sizeLabel(value):value>=1000000?nf.format(value/1000000)+"M":value>=1000?nf.format(value/1000)+"k":nf.format(value);
      root.append(this.svgNode("line",{x1:left,y1:py,x2:width-right,y2:py,class:"trend-grid"}),this.svgNode("text",{x:left-12,y:py+4,"text-anchor":"end",class:"trend-axis-label"},label));
    }
    for(const stamp of ticks) {
      const px=x(stamp);
      root.append(this.svgNode("line",{x1:px,y1:top,x2:px,y2:height-bottom,class:"trend-grid trend-time-grid","data-time":stamp}));
    }
    if(axis.gaps.length) {
      const id=chart.prefix+"-idle-hatch",defs=this.svgNode("defs"),pattern=this.svgNode("pattern",{id,width:7,height:7,patternUnits:"userSpaceOnUse"});
      pattern.append(this.svgNode("path",{d:"M-1,1L1,-1M0,7L7,0M6,8L8,6",class:"trend-fold-hatch"}));defs.append(pattern);root.append(defs);
      for(const gap of axis.gaps) {
        const a=x(Math.max(start,gap.start)),b=x(Math.min(end,gap.end)),center=(a+b)/2,span=Math.max(12,b-a);
        const from=Math.max(left,center-span/2),to=Math.min(width-right,center+span/2);
        const band=this.svgNode("g",{class:"trend-time-fold","data-start":gap.start,"data-end":gap.end});
        band.append(this.svgNode("title",{},t("已跳过无请求时段：{start} – {end}").replace("{start}",this.time(gap.start,true)).replace("{end}",this.time(gap.end,true))),
          this.svgNode("rect",{x:from,y:top,width:to-from,height:plotHeight,class:"trend-fold-shade"}),
          this.svgNode("rect",{x:from,y:top,width:to-from,height:plotHeight,fill:`url(#${id})`}),
          this.svgNode("path",{d:`M${from},${top}V${height-bottom}M${to},${top}V${height-bottom}`,class:"trend-fold-edge"}));root.append(band);
      }
    }
    chart.points=[];
    for(const group of model.visible) {
      // Set brightness before attachment: layout reads can otherwise start a fade from full opacity.
      const layer=this.svgNode("g",{class:"trend-series-line"+(emphasis&&group.id!==emphasis?" dimmed":""),"data-series":group.id}),events=this.svgNode("g");let path="",pen=false,lastDot=null,lastPoint=null;
      for(const source of group.points) {
        const value=source[chart.field];if(value===null)continue;
        const point={...source,value,series:group,x:x(source.packet.created),y:y(value)};
        path+=(pen?"L":"M")+point.x.toFixed(2)+","+point.y.toFixed(2);pen=true;chart.points.push(point);
        if(point.events.some(event=>["predictive","overflow","retry","compact"].includes(event))){point.node=this.marker(point,events);chart.eventNodes.push(point.node);}
        else if(!lastDot||Math.hypot(point.x-lastDot.x,point.y-lastDot.y)>=4){point.node=this.svgNode("circle",{cx:point.x,cy:point.y,r:2.1,fill:group.color,class:"trend-sample"});layer.append(point.node);lastDot=point;}
        lastPoint=point;
      }
      if(lastPoint&&lastDot!==lastPoint&&!lastPoint.events.length){lastPoint.node=this.svgNode("circle",{cx:lastPoint.x,cy:lastPoint.y,r:2.1,fill:group.color,class:"trend-sample"});layer.append(lastPoint.node);}
      layer.prepend(this.svgNode("path",{d:path,class:"trend-curve",stroke:group.color}));layer.append(events);root.append(layer);
    }
    if(limit!==null) {
      const py=y(limit),guide=this.svgNode("g",{class:"trend-budget","pointer-events":"none"});
      guide.append(this.svgNode("line",{x1:left,y1:py,x2:width-right,y2:py,class:"trend-budget-line"}),
        this.svgNode("text",{x:width-right,y:py-8,"text-anchor":"end",class:"trend-budget-label"},t("阈值 {size}").replace("{size}",TokenTrendView.sizeLabel(limit))));
      root.append(guide);
    }
    chart.focusLayer=this.svgNode("g",{"pointer-events":"none"});root.append(chart.focusLayer);chart.svg.replaceChildren(root);
    chart.empty.hidden=known.length>0;
    const missingWindow=!this.packets.size||!this.covers(this.loadedBounds);
    chart.empty.textContent=t((this.loading||this.windowTimer!==null)&&missingWindow?"正在加载趋势…":this.error&&missingWindow?"暂时无法加载趋势，请稍后重试":!model.matched.length?(this.query?"没有匹配的会话":"此时间范围内没有请求"):chart.key==="size"?"暂无可确认的请求大小":"暂无可确认的 Token 用量");
  }
  renderTimeAxis() {
    const bounds=this.charts[0].bounds,root=document.createDocumentFragment();
    if(bounds) {
      const {width,left,right,x,ticks,axis}=bounds;
      this.timeAxis.setAttribute("viewBox",`0 0 ${width} 32`);
      root.append(this.svgNode("line",{x1:left,y1:1,x2:width-right,y2:1,class:"trend-time-tick"}));
      const withSeconds=ticks.some(stamp=>new Date(stamp*1000).getSeconds()!==0);
      for(const stamp of ticks) {
        const px=x(stamp);
        const label=new Date(stamp*1000).toLocaleTimeString(uiI18n.language==="en"?"en-GB":"zh-CN",{hour:"2-digit",minute:"2-digit",...(withSeconds?{second:"2-digit"}:{}),hourCycle:"h23"});
        root.append(this.svgNode("line",{x1:px,y1:1,x2:px,y2:5,class:"trend-time-tick","data-time":stamp}),this.svgNode("text",{x:px,y:22,"text-anchor":px>width-right-40?"end":px<left+40?"start":"middle",class:"trend-axis-label","data-time":stamp},label));
      }
      for(const gap of axis.gaps) {
        const a=x(Math.max(bounds.start,gap.start)),b=x(Math.min(bounds.end,gap.end)),center=(a+b)/2,offset=3;
        const mark=this.svgNode("g",{class:"trend-time-break","data-start":gap.start,"data-end":gap.end});
        mark.append(this.svgNode("title",{},t("已跳过无请求时段：{start} – {end}").replace("{start}",this.time(gap.start,true)).replace("{end}",this.time(gap.end,true))),
          this.svgNode("rect",{x:center-11,y:0,width:22,height:13,rx:3}),this.svgNode("path",{d:`M${center-offset+1},2l-2,7M${center+offset+1},2l-2,7`}));root.append(mark);
      }
    }
    this.timeAxis.replaceChildren(root);
  }
  pointAt(event,chart) {
    if(!chart.bounds)return;
    const rect=chart.svg.getBoundingClientRect();if(!rect.width||!rect.height)return;
    const x=event.clientX-rect.left,y=event.clientY-rect.top,scaleX=rect.width/chart.bounds.width,scaleY=rect.height/chart.bounds.height;
    let nearest=null,distance=28*28,line=null,lineDistance=8*8,previous=null,lastX=0,lastY=0;
    for(const point of chart.points) {
      const px=point.x*scaleX,py=point.y*scaleY,value=(px-x)**2+(py-y)**2;
      if(value<distance){nearest=point;distance=value;}
      // Match the rendered segments, including connections across missing values.
      if(previous?.series.id===point.series.id) {
        const dx=px-lastX,dy=py-lastY,length=dx*dx+dy*dy;
        const ratio=length?Math.max(0,Math.min(1,((x-lastX)*dx+(y-lastY)*dy)/length)):0;
        const gap=(lastX+ratio*dx-x)**2+(lastY+ratio*dy-y)**2;
        if(gap<lineDistance){line=point;lineDistance=gap;}
      }
      previous=point;lastX=px;lastY=py;
    }
    const series=line&&lineDistance<distance?line.series.id:nearest?.series.id||null;
    this.hover=nearest?.series.id===series?nearest.packet.id:null;
    this.hoverSeries=this.hover?null:series;
    this.hoverChart=chart.key;this.renderFocus();
  }
  renderFocus() {
    if(!this.active)return;
    const event=this.eventKey(),anchor=event?null:this.charts.map(chart=>chart.points.find(value=>value.packet.id===this.hover)).find(Boolean),emphasis=event?null:this.emphasis||this.hoverSeries||anchor?.series.id;
    for(const {key,button} of this.eventLegend)button.classList.toggle("highlighted",key===event);
    for(const item of $("trend-series").children) {
      const active=!!emphasis&&item.dataset.renderKey==="trend-series:"+emphasis;
      item.classList.toggle("active",active);item.setAttribute("aria-current",String(active));
    }
    this.guide.replaceChildren();
    this.timeTooltip.hidden=true;
    const host=this.tooltipHost.getBoundingClientRect(),originX=host.left+(this.tooltipHost.clientLeft||0),originY=host.top+(this.tooltipHost.clientTop||0);
    if(anchor&&this.charts[0].bounds) {
      const first=this.charts[0],area=this.guide.getBoundingClientRect(),rect=first.svg.getBoundingClientRect(),axis=this.timeAxis.getBoundingClientRect();
      if(area.width&&area.height) {
        this.guide.setAttribute("viewBox",`0 0 ${area.width} ${area.height}`);
        const px=rect.left-area.left+first.bounds.x(anchor.packet.created)*rect.width/first.bounds.width;
        this.guide.append(this.svgNode("line",{x1:px,x2:px,y1:rect.top-area.top+first.bounds.top*rect.height/first.bounds.height,y2:axis.top-area.top+5,class:"trend-focus-line"}));
        this.timeTooltip.textContent=this.time(anchor.packet.created,true);this.timeTooltip.hidden=false;
        const timeX=rect.left-originX+first.bounds.x(anchor.packet.created)*rect.width/first.bounds.width;
        this.timeTooltip.style.left=Math.max(8,Math.min(this.tooltipHost.clientWidth-this.timeTooltip.offsetWidth-8,timeX-this.timeTooltip.offsetWidth/2))+"px";
        this.timeTooltip.style.top=axis.top-originY+Math.max(0,(axis.height-this.timeTooltip.offsetHeight)/2)+"px";
      }
    }
    for(const chart of this.charts) {
      chart.tooltip.hidden=true;
      if(!chart.bounds||!chart.focusLayer)continue;
      chart.svg.classList.toggle("trend-event-filtering",!!event);
      if(chart.eventKey!==event) {
        for(const node of chart.eventNodes) {
          const matched=!!event&&node.dataset.events.split(" ").includes(event);
          node.classList.toggle("highlighted",matched);node.classList.toggle("dimmed",!!event&&!matched);
        }
        chart.eventKey=event;
      }
      for(const line of chart.svg.querySelectorAll("[data-series]"))line.classList.toggle("dimmed",!!emphasis&&line.dataset.series!==emphasis);
      chart.hoverNode?.classList.toggle("trend-hover-node",false);chart.hoverNode=null;
      chart.focusLayer.replaceChildren();if(!anchor)continue;
      const point=chart.points.find(value=>value.packet.id===this.hover);
      if(point?.node){chart.hoverNode=point.node;point.node.classList.toggle("trend-hover-node",true);}
      else if(point)chart.focusLayer.append(this.svgNode("circle",{cx:point.x,cy:point.y,r:3.6,fill:point.series.color,class:"trend-sample trend-hover-node"}));
      chart.tooltip.hidden=false;
      const estimated=chart.key!=="size"&&requestTokenUsage(anchor.packet,this.tokenData(anchor.packet))?.estimated===true;
      chart.tooltip.replaceChildren(el("span","",t(chart.key==="size"?"转发请求":estimated?"输入 Token（估算）":"输入 Token")),el("strong","",point?(chart.key==="size"?TokenTrendView.sizeLabel(point.value):(estimated?"≈ ":"")+nf.format(point.value)):"—"));
      const rect=chart.svg.getBoundingClientRect(),panelTop=rect.top-originY;
      const px=rect.left-originX+chart.bounds.x(anchor.packet.created)*rect.width/chart.bounds.width;
      const py=panelTop+(point?point.y:(chart.bounds.top+chart.bounds.height-chart.bounds.bottom)/2)*rect.height/chart.bounds.height;
      const width=chart.tooltip.offsetWidth,height=chart.tooltip.offsetHeight,minTop=panelTop+6,maxTop=panelTop+rect.height-height-6;
      const left=px+14+width>this.tooltipHost.clientWidth-8?px-width-14:px+14,top=py-height-10;
      chart.tooltip.style.left=Math.max(8,Math.min(this.tooltipHost.clientWidth-width-8,left))+"px";
      chart.tooltip.style.top=Math.max(minTop,Math.min(maxTop,top<minTop?py+10:top))+"px";
    }
  }
}
tokenTrendView=new TokenTrendView({api,status:()=>state.status,sessions:()=>sessionSummaries,peek:listTokens,read:readListTokens,isGeneration:isTrendGeneration});
function setWorkspaceView(view) {
  workspaceView=["trends","sharing"].includes(view)?view:"records";
  $("records-view").hidden=workspaceView!=="records";$("token-trend-view").hidden=workspaceView!=="trends";
  if($("sharing-view"))$("sharing-view").hidden=workspaceView!=="sharing";
  $("view-records").setAttribute("aria-pressed",String(workspaceView==="records"));$("view-trends").setAttribute("aria-pressed",String(workspaceView==="trends"));
  $("view-sharing")?.setAttribute("aria-pressed",String(workspaceView==="sharing"));
  try {localStorage.setItem("packet-inspector-view",workspaceView);} catch(_) {}
  tokenTrendView.setActive(workspaceView==="trends");
  globalThis.InspectorSharing?.setActive(workspaceView==="sharing");
  if(workspaceView==="sharing"&&!globalThis.InspectorSharing&&$("sharing-message")) {
    $("sharing-message").textContent=t("当前后台不支持共享，请更新软件后使用。");$("sharing-message").hidden=false;
    if($("sharing-cards"))$("sharing-cards").hidden=true;
  }
  if(workspaceView!=="records"){cancelTokenRead();state.listController?.abort();}else if(state.status){loadPackets(false,true).catch(fail);loadTokenWindow();loadListTokens();}
}
$("view-records").onclick=()=>setWorkspaceView("records");$("view-trends").onclick=()=>setWorkspaceView("trends");
$("view-sharing")?.addEventListener("click",()=>setWorkspaceView("sharing"));
try {const saved=localStorage.getItem("packet-inspector-view");if(["trends","sharing"].includes(saved))setWorkspaceView(saved);} catch(_) {}
async function poll() {
  if(state.polling || document.hidden)return;
  state.polling=true;
  try {
    await refreshStatus();
    if(workspaceView==="sharing")return;
    if(workspaceView==="trends") {
      if(state.filterRevision!==state.status.storage.revision)await refreshFilters();
      await refreshSessionSummaries();await tokenTrendView.refresh();return;
    }
    const revision=state.status.storage.revision, pending=[];
    if(state.filterRevision!==revision)pending.push(refreshFilters());
    if(!state.older && (state.listRevision!==revision || state.sort==="input_tokens" && (tokenSortCache?.dirty || tokenSortCache?.expires<=Date.now())))pending.push(loadPackets(false,true));
    await Promise.all(pending);
    await Promise.all([refreshSessionSummaries(),loadTokenWindow()]);loadListTokens();
  } catch(e) {fail(e);} finally {state.polling=false;}
}
const pollTimer=setInterval(poll,2000);
document.addEventListener("visibilitychange",()=>{if(document.hidden){tokenTrendView.cancelPending();return;}if(workspaceView==="trends")tokenTrendView.refresh();else if(workspaceView==="records"){loadTokenWindow();loadMorePackets();loadListTokens();}});
(async()=>{state.polling=true;try{await Promise.all([refreshStatus(true),refreshFilters()]);await refreshSessionSummaries();}finally{state.polling=false;}})().catch(fail);
