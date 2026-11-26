"use strict";

globalThis.InspectorConnections = {
  create({request}) {
    const get=id=>document.getElementById(id), tr=value=>uiI18n.t(value);
    const panel=get("connection-panel"), controls=get("connection-controls");
    const headerToggle=get("request-compression");
    const localLabel=controls.querySelector(".connection-local-label");
    const restartBadge=get("restart-notice-open");
    const restartSurface = restartBadge.querySelector(".restart-notice-surface");
    const restartLabel = get("restart-notice-label");
    const restartMotion = matchMedia("(prefers-reduced-motion: no-preference)");
    const viewToggle=get("view-toggle");
    const settingsButton=get("settings-open");
    const dialog=get("devices-confirm-dialog"), list=get("connection-devices-list");
    const applyButton=get("devices-confirm-apply"), cancelButton=get("devices-confirm-cancel");
    const nativePopover="popover" in HTMLElement.prototype;
    const nodes=new Map(), confirmNodes=new Map(), pending=new Map(), reconnectPrompts=new Map();
    let rows=[],hasServers=false,loading=false,submitting=false,catalogError="",revision="",timer=null,closeTimer=null,hover=false;
    let openRequested=false,focusOnOpen=false;
    let plan=null,finished=false,disposed=false;
    let surfaceAnimation, labelAnimation;
    let restartBendFactor = 0;
    const text=(node,value)=>{if(node.textContent!==value)node.textContent=value;};
    const isOpen=()=>nativePopover?panel.matches(":popover-open"):!panel.hidden;
    const inDropdown=node=>controls.contains(node)||panel.contains(node)||(isOpen()&&restartBadge.contains(node));
    const busy=row=>!!row.busy;
    const desired=action=>action==="activate"?"connected":"disconnected";
    const operationError=(row,action)=>row.error||(action==="reconnect"?row.codex_reconnect?.error:"");
    const completed=(row,action)=>action==="reconnect"?row.codex_reconnect?.state==="reloaded":row.state===desired(action);
    const needsReconnect=row=>row.reload_required&&!row.error;
    function stateText(row) {
      if(row.busy)return tr(row.action==="reconnect"?"正在重新连接…":row.action==="activate"?"正在接入…":row.action==="restore"?"正在断开…":"正在检查…");
      if(row.error||row.state==="error")return tr("接入异常");
      const value=tr(row.state==="connected"?"已接入":row.state==="disconnected"?"已断开":"待检查");
      if(row.reload_required)return value+" · "+tr("请重新连接远程 Codex");
      return value+(row.warning_count?" · "+tr("部分配置未完成，请检查此设备的接入详情。"):"");
    }
    function statusIndicator(row) {
      const reload=row.reload_required?tr("配置待生效，请点击重连图标重新加载此服务器的 Codex。"):"";
      const error=row.error||row.codex_reconnect?.error;
      if(error||row.state==="error")return {kind:"error",message:[
        tr(row.error||row.state==="error"?"接入异常":"重连结果未确认"),error?tr(error):"",reload].filter(Boolean).join("\n")};
      let message=reload||stateText(row);
      if(reload&&row.warning_count)message+="\n"+tr("部分配置未完成，请检查此设备的接入详情。");
      if(row.codex_reconnect?.warning)message+="\n"+tr(row.codex_reconnect.warning);
      const kind=reload?"pending":row.warning_count||row.codex_reconnect?.warning?"warning":
        row.state==="connected"?"connected":row.state==="disconnected"?"disconnected":"unknown";
      return {kind,message};
    }
    function element(tag,cls="") {const node=document.createElement(tag);node.className=cls;return node;}
    function rowNodes(row) {
      if(nodes.has(row.id))return nodes.get(row.id);
      const root=element("div","connection-device"),info=element("div","connection-device-info");
      const label=element("strong"),state=element("span","connection-device-state");
      label.addEventListener("pointerenter", () => {
        if (label.scrollWidth > label.clientWidth) label.title = label.textContent;
        else label.removeAttribute("title");
      });
      label.addEventListener("pointerleave", () => label.removeAttribute("title"));
      state.setAttribute("role","img");
      const buttons=element("div","language-toggle connection-device-actions");buttons.setAttribute("role","group");
      const actions={};
      for(const action of ["restore","activate"]) {
        const button=element("button");button.type="button";
        button.addEventListener("click",()=>confirm(action,row.id));buttons.append(button);actions[action]=button;
      }
      const tools=element("div","connection-device-controls"),reconnect=element("button","connection-device-reconnect");
      reconnect.type="button";
      const icon=document.createElementNS("http://www.w3.org/2000/svg","svg");
      const links=document.createElementNS(icon.namespaceURI,"path");
      icon.setAttribute("viewBox","0 0 24 24");
      icon.setAttribute("aria-hidden","true");
      icon.setAttribute("focusable","false");
      links.setAttribute("d","M10.5 13.5a4.5 4.5 0 0 0 6.36 0l3-3a4.5 4.5 0 1 0-6.36-6.36l-1.65 1.65m1.65 4.71a4.5 4.5 0 0 0-6.36 0l-3 3a4.5 4.5 0 1 0 6.36 6.36l1.65-1.65");
      icon.append(links);
      reconnect.append(icon);
      reconnect.addEventListener("click",()=>confirm("reconnect",row.id));
      tools.append(reconnect,buttons);info.append(state,label);root.append(info,tools);list.append(root);
      const value={root,label,state,buttons,actions,reconnect};nodes.set(row.id,value);return value;
    }
    function render() {
      for(const [id,value] of nodes)if(!rows.some(row=>row.id===id)){value.root.remove();nodes.delete(id);}
      for(const row of rows) {
        const value=rowNodes(row);text(value.label,row.name);
        const indicator=statusIndicator(row);value.state.dataset.status=indicator.kind;
        const stateLabel=row.busy?stateText(row):indicator.message;
        value.state.title=stateLabel;value.state.setAttribute("aria-label",stateLabel);
        value.root.dataset.state=row.error?"error":row.state;
        value.root.setAttribute("aria-busy",String(!!row.busy));
        value.buttons.setAttribute("aria-label",row.name);
        value.buttons.setAttribute("aria-description",stateLabel);
        for(const [action,button] of Object.entries(value.actions)) {
          text(button,tr(action==="activate"?"接入":"断开"));
          button.setAttribute("aria-pressed",String(row.state===desired(action)&&!row.error));
          button.disabled=busy(row)||submitting||(row.state===desired(action)&&!row.error);
        }
        value.reconnect.classList.toggle("needs-reconnect",!!row.reload_required&&!busy(row)&&!submitting);
        const reconnectLabel=tr("重连")+" Codex";
        const nativeHint=row.codex_reconnect?.error||row.codex_reconnect?.warning;
        const description=tr(nativeHint||"重载此服务器当前账号的 Codex 后台，使配置生效。");
        value.reconnect.title=reconnectLabel+"\n"+description;
        value.reconnect.setAttribute("aria-label",reconnectLabel+" · "+row.name);
        value.reconnect.setAttribute("aria-description",description);
      }
      const note=get("connection-devices-status");
      text(note,tr(catalogError));note.hidden=!catalogError;
      renderConfirmation();
      place();
    }
    function renderConfirmation() {
      if(!plan)return;
      const notice=get("devices-confirm-error"), noticeText=catalogError||plan.error;
      text(notice,tr(noticeText));notice.hidden=!noticeText;
      const selected=plan.targets.map(target=>rows.find(row=>row.id===target.id)||{...target,state:"error",error:tr("设备列表已变化，请重新确认影响范围。")});
      for(const row of selected) {
        let node=confirmNodes.get(row.id);
        if(!node){node=element("li");const title=element("strong"),detail=element("span");node.append(title,detail);confirmNodes.set(row.id,node);get("devices-confirm-targets").append(node);}
        const error=operationError(row,plan.action),reloaded=plan.action==="reconnect"&&finished&&completed(row,plan.action);
        text(node.firstChild,row.name);text(node.lastChild,error?tr(error):reloaded?tr(row.codex_reconnect?.warning||"Codex 后台已重载，等待 ChatGPT 恢复连接。"):stateText(row));
        node.dataset.state=error?"error":row.state;
      }
      const running=selected.some(row=>pending.has(row.id));
      const failed=finished&&selected.some(row=>operationError(row,plan.action)||!completed(row,plan.action));
      const followup=plan.followup&&!plan.submitted;
      applyButton.hidden=finished&&!failed;
      applyButton.disabled=submitting||running||selected.some(busy)||!!catalogError;
      text(applyButton,tr(submitting||running?"正在执行…":failed?"重试":followup?"立即重连":
        plan.action==="reconnect"?"确认重新连接":plan.action==="activate"?"确认接入":"确认断开"));
      text(cancelButton,tr(finished?"完成":running?"关闭":followup?"稍后重连":"取消"));
      get("devices-confirm-hint").hidden=finished&&!failed&&plan.action!=="reconnect"&&!selected[0].reload_required;
      dialog.setAttribute("aria-busy",String(submitting||running));
    }
    function toggleDivider(toggle) {
      const [disconnect, connect] = toggle.children;
      return (disconnect.getBoundingClientRect().right + connect.getBoundingClientRect().left) / 2;
    }
    function place() {
      if (!isOpen()) return;
      const trigger = controls.getBoundingClientRect();
      const localToggle = headerToggle.getBoundingClientRect();
      const settingsLeft = settingsButton.getBoundingClientRect().left;
      const panelRight = Math.min(innerWidth - 12, (localToggle.right + settingsLeft) / 2);
      const frameEndPadding = Math.max(0, panelRight - localToggle.right);
      const label = localLabel.getBoundingClientRect();
      const deviceToggle = list.querySelector(".connection-device-actions");
      const address = list.querySelector(".connection-device-info strong");
      panel.style.setProperty("--connection-panel-top", `${trigger.bottom + 4}px`);
      panel.style.paddingInlineEnd = "";
      if (deviceToggle) {
        const localEnd = localToggle.right - toggleDivider(headerToggle);
        const remoteEnd = deviceToggle.getBoundingClientRect().right - toggleDivider(deviceToggle);
        // Align both dividers while allowing for the scrollbar and the two different button widths.
        const edgeInset = panel.offsetWidth - panel.clientWidth - 1;
        panel.style.paddingInlineEnd = `${Math.max(0, frameEndPadding + localEnd - remoteEnd - edgeInset)}px`;
      }
      const addressInset = address ? address.getBoundingClientRect().left - panel.getBoundingClientRect().left : 28;
      const minWidth = addressInset + label.width + 8 + localToggle.width + frameEndPadding;
      const availableWidth = panelRight - viewToggle.getBoundingClientRect().right - 4;
      panel.style.setProperty("--connection-panel-min-width", `${minWidth}px`);
      panel.style.setProperty("--connection-panel-available-width", `${Math.max(minWidth, availableWidth)}px`);
      const {width} = panel.getBoundingClientRect();
      const panelLeft = Math.max(12, panelRight - width);
      panel.style.left = `${panelLeft}px`;
      controls.style.setProperty("--connection-frame-left", `${panelLeft - trigger.left}px`);
      controls.style.setProperty("--connection-frame-width", `${width}px`);
      // The local label and the end of the compact badge align with the server addresses.
      controls.parentElement.style.setProperty("--connection-local-start", `${panelLeft + addressInset - trigger.left}px`);
    }
    function restartSurfaceFrame() {
      const {x, y, width, height} = restartSurface.getBoundingClientRect();
      const {borderRadius, backgroundColor, borderTopColor} = getComputedStyle(restartSurface);
      return {x:x+width/2, y:y+height/2, width, height, borderRadius, backgroundColor, borderColor:borderTopColor};
    }
    function setPanelMode(mode) {
      const expanded = mode !== null;
      if (expanded === controls.hasAttribute("data-server-panel-open")) return;
      // Read the current frame so reversing an unfinished transition starts where it is visible.
      const from = restartMotion.matches && restartBadge.dataset.visible === "true" ? restartSurfaceFrame() : null;
      surfaceAnimation?.cancel();
      labelAnimation?.cancel();
      if (expanded) {
        controls.dataset.serverPanelOpen = mode;
        place();
      } else controls.removeAttribute("data-server-panel-open");
      if (!from) return;

      // Animate the surface independently of the button's hit target.
      const to = restartSurfaceFrame();
      if (expanded) {
        const direction = Math.random() < .5 ? -1 : 1;
        restartBendFactor = direction * (.6 + Math.random() * .4);
      }
      const startX = from.x - to.x, startY = from.y - to.y;
      // Budget for the visible offset when reversing an unfinished arc.
      const maxBend = Math.max(0, Math.min(7 - Math.abs(startY), Math.abs(startX) / 4));
      const bend = restartBendFactor * maxBend;
      const arrival = expanded ? .78 : 1;
      const steps = 12;
      const path = Array.from({length:steps + 1}, (_, index) => {
        const time = index / steps;
        const progress = time * time * (3 - 2 * time); // Ease in and out along the arc.
        const remaining = 1 - progress;
        const x = startX * remaining;
        const y = (startY + 4 * bend * progress) * remaining;
        return {offset:time * arrival, transform:`translate(${x}px, ${y}px)`};
      });

      const capsule = expanded ? from : to, dot = expanded ? to : from;
      const taper = {
        width: dot.width + (capsule.width - dot.width) * .43,
        height: dot.height + (capsule.height - dot.height) * .32,
        borderRadius: capsule.x > dot.x
          ? "28% 78% 78% 28% / 55% 50% 50% 55%" : "78% 28% 28% 78% / 50% 55% 55% 50%"
      };
      const shapeFrame = ({x, y, width, height, ...paint}, offset) => ({
        ...paint,
        offset,
        inlineSize: `${width}px`,
        blockSize: `${height}px`
      });
      const shape = [shapeFrame(from, 0), shapeFrame(taper, expanded ? .49 : .42)];
      if (expanded) {
        const squeezed = {...to, width:to.width * .82, height:to.height * 1.18};
        shape.push({...shapeFrame(squeezed, arrival), easing:"ease-out"});
      }
      shape.push(shapeFrame(to, 1));
      const timing = {duration:350};
      const frames = [...path, ...shape].sort((a, b) => a.offset - b.offset);
      surfaceAnimation = restartSurface.animate(frames, timing);
      if (!expanded) labelAnimation = restartLabel.animate([{opacity:0}, {opacity:0, offset:.65}, {opacity:1}], timing);
    }
    function hide() {
      clearTimeout(closeTimer);
      openRequested=false;focusOnOpen=false;
      if(nativePopover){if(isOpen())panel.hidePopover();}else{panel.hidden=true;}
      setPanelMode(null);
    }
    function focusFirstAction() {
      controls.dataset.serverPanelOpen="";
      panel.querySelector("button:not(:disabled):not([hidden])")?.focus();
    }
    function show(focus=false) {
      clearTimeout(closeTimer);
      if(document.querySelector("dialog[open]"))return;
      if(isOpen()) {
        if(focus)focusFirstAction();
        return;
      }
      openRequested=true;focusOnOpen ||= focus;
      poll();
    }
    function openAfterPoll() {
      const focus=focusOnOpen;
      const shouldOpen=openRequested&&hasServers&&(hover||controls.contains(document.activeElement));
      openRequested=false;focusOnOpen=false;
      if(!shouldOpen||disposed||document.querySelector("dialog[open]"))return;
      if(nativePopover)panel.showPopover({source:controls});else{panel.hidden=false;}
      setPanelMode(hover&&!focus?"hover":"");
      if(focus)focusFirstAction();
    }
    function leave() {
      hover=false;clearTimeout(closeTimer);
      closeTimer=setTimeout(()=>{if(!hover&&!inDropdown(document.activeElement))hide();},180);
    }
    function schedule() {
      clearTimeout(timer);
      if(disposed)return;
      if(pending.size||rows.some(row=>row.busy))timer=setTimeout(poll,700);
      else if(isOpen()||dialog.open)timer=setTimeout(poll,15000);
    }
    async function poll() {
      if(loading||disposed)return;
      loading=true;
      // A status request already in flight may predate a new operation.
      const observed=[...pending];
      try {
        const value=await request("/api/connections");
        if(!Array.isArray(value.targets))throw Error(tr("无法读取设备接入状态。"));
        rows=value.targets.filter(row=>row.kind==="ssh");
        hasServers=rows.length>0;catalogError=value.error||"";revision=value.revision;
        if(!hasServers)hide();
        for(const [id,{operationId,action}] of observed) {
          const row=rows.find(target=>target.id===id);
          if(!row||row.operation_id!==operationId){
            pending.delete(id);catalogError=tr("设备状态已变化，请刷新后检查操作结果。");continue;
          }
          if(row.busy)continue;
          pending.delete(id);
          if(action!=="reconnect"&&completed(row,action)&&needsReconnect(row))reconnectPrompts.set(id,operationId);
        }
        if(plan?.submitted&&!plan.targets.some(row=>pending.has(row.id)))finished=true;
      } catch(error) {
        if(error.status===404){rows=[];catalogError=tr("当前运行版本不支持设备接入管理，请更新 EXE 后使用。");}
        else catalogError=error.message;
      }
      finally {loading=false;offerReconnect();render();openAfterPoll();schedule();}
    }
    function offerReconnect() {
      if(!reconnectPrompts.size||disposed||submitting||catalogError)return;
      if(document.querySelector("dialog[open]:not(#devices-confirm-dialog)"))return;
      if(dialog.open&&(!finished||plan.action==="reconnect"))return;
      for(const [id,operationId] of reconnectPrompts) {
        const row=rows.find(target=>target.id===id);
        if(!row||row.operation_id!==operationId||!needsReconnect(row)){reconnectPrompts.delete(id);continue;}
        if(row.busy||(dialog.open&&plan.targets[0].id!==id))continue;
        reconnectPrompts.delete(id);
        openConfirmation("reconnect",row,true);
        return;
      }
    }
    async function confirm(action,id) {
      if(action!=="reconnect")await poll();
      if(disposed||document.querySelector("dialog[open]"))return;
      const target=rows.find(row=>row.id===id);
      if(!target||(catalogError&&action!=="reconnect")){show();return;}
      openConfirmation(action,target);
      if(action==="reconnect")poll();
    }
    function openConfirmation(action,target,followup=false) {
      hide();finished=false;plan={action,targets:[{id:target.id,name:target.name}],revision,submitted:false,followup,error:""};
      confirmNodes.clear();get("devices-confirm-targets").replaceChildren();
      text(get("devices-confirm-title"),tr(followup?"现在重新连接 Codex？":action==="reconnect"?"重新连接 Codex":action==="activate"?"接入服务器":"断开服务器"));
      text(get("devices-confirm-description"),tr(action==="reconnect"?"将重载所选服务器当前账号的 Codex 后台，使配置生效。该账号正在执行或排队的任务可能中断。":action==="activate"?
        "软件会先验证连接，再备份并更新此服务器的 Codex 配置。":
        "将恢复此服务器的原始出口并开启 Codex 原生压缩。中转服务保持运行。"));
      text(get("devices-confirm-hint"),tr(action==="reconnect"?"ChatGPT 将短暂断开该服务器并尝试自动恢复连接。中转服务保持运行。":"配置变更后，请重新连接该服务器的 Codex。"));
      renderConfirmation();
      if(dialog.open)cancelButton.focus();
      else dialog.showModal();
      schedule();
    }
    async function apply() {
      const current=plan;
      if(!current||submitting||current.targets.some(row=>pending.has(row.id)))return;
      const {action,revision}=current;
      const targets=current.targets.filter(target=>{const row=rows.find(row=>row.id===target.id);return !finished||!row||operationError(row,action)||!completed(row,action);});
      if(targets.some(target=>{const row=rows.find(row=>row.id===target.id);return !row||busy(row);}))return;
      submitting=true;current.error="";render();
      try {
        const value=await request("/api/connections",{targets:targets.map(row=>row.id),action,revision});
        if(!value.accepted||typeof value.accepted!=="object")throw Error(tr("未能确认操作已开始，请刷新状态后检查。"));
        current.submitted=true;finished=false;
        for(const [id,operationId] of Object.entries(value.accepted)) {
          pending.set(id,{operationId,action});
          const row=rows.find(target=>target.id===id);
          if(row) {
            Object.assign(row,{busy:true,action,error:"",operation_id:operationId});
            if(action==="reconnect")delete row.codex_reconnect;
          }
        }
      } catch(error) {current.error=error.message;}
      finally {submitting=false;offerReconnect();render();schedule();}
    }
    if(!nativePopover){panel.hidden=true;panel.classList.add("connection-panel-fallback");}
    else panel.addEventListener("toggle",()=>{if(!isOpen())setPanelMode(null);});
    restartMotion.addEventListener("change",()=>{
      if(!restartMotion.matches) {
        surfaceAnimation?.cancel();
        labelAnimation?.cancel();
      }
    });
    for(const node of [controls,panel]) {
      node.addEventListener("pointerenter",event=>{if(event.pointerType==="touch")return;hover=true;show();});
      node.addEventListener("pointerleave",leave);
      node.addEventListener("focusin",()=>{
        if(node===panel&&isOpen())controls.dataset.serverPanelOpen="";
        show();
      });
      node.addEventListener("focusout",leave);
    }
    restartBadge.addEventListener("pointerenter",event=>{
      if(isOpen()&&event.pointerType!=="touch"){hover=true;clearTimeout(closeTimer);}
    });
    restartBadge.addEventListener("pointerleave",()=>{if(isOpen())leave();});
    controls.addEventListener("keydown",event=>{
      if(event.key==="ArrowDown"){event.preventDefault();show(true);}
    });
    document.addEventListener("pointerdown",event=>{if(!inDropdown(event.target))hide();});
    document.addEventListener("keydown",event=>{if(event.key==="Escape"&&(isOpen()||openRequested)){hide();event.stopPropagation();}});
    globalThis.addEventListener("resize",place);
    applyButton.addEventListener("click",apply);
    cancelButton.addEventListener("click",()=>dialog.close());
    document.addEventListener("close",event=>{
      if(event.target instanceof HTMLDialogElement)offerReconnect();
      if(event.target===dialog)schedule();
    },true);
    globalThis.addEventListener("pagehide",()=>{disposed=true;clearTimeout(timer);clearTimeout(closeTimer);});
    render();poll();
    return {render,hide};
  }
};

// Saved configuration can outlive both the desktop page and a relay worker.
// Older workers use a local ledger until the next normal application upgrade.
globalThis.InspectorRestartNotice = {
  connectionProblems(s={},details=null) {
    const event=s?.connection_resume;
    if(["pending","checking","recovering"].includes(event?.state))return [];
    const rows=[],add=(scope,reason,detail="")=>rows.push({scope,reason,detail:String(detail).slice(0,1000)});
    const error=event?.error||s?.provider_sync_error;
    if(error)add("","接入配置同步失败",error);
    const providerReasons={session_provider_migration_failed:"会话提供商配置迁移失败",
      session_provider_ambiguous:"会话提供商配置存在冲突，未自动迁移。"};
    for(const row of s?.provider_warnings??event?.result?.warnings??details?.warnings??[])
      add(row.provider||row.file||"Provider",providerReasons[row.reason]||"上游配置不支持接入",row.detail||row.reason);
    const wsl=s?.wsl_connection??event?.result?.wsl??{warnings:details?.wslWarnings};
    const reasons={relay_unreachable:"WSL 无法访问本机中转，未修改配置。",relay_instance_mismatch:"WSL 连接到其他服务，未修改配置。",
      config_not_found:"未找到该 WSL 用户的 Codex 配置。",restore_pending:"WSL 配置尚未恢复。",...providerReasons};
    for(const row of wsl.warnings||[])add("WSL · "+row.distribution,reasons[row.reason]||"WSL 接入失败",row.detail||row.reason);
    if(!rows.length&&event?.state==="attention")add("","接入配置同步失败");
    return rows.slice(0,32);
  },
  create({status, request, openSettings, busy=()=>false, openLogs=()=>{}}) {
    const $=id=>document.getElementById(id), labels={connection:"Codex 接入",context_tokens:"有效上下文",
      compact_tokens:"自动压缩阈值",capacity_retry:"容量不足重试",image_analysis_mode:"图片解析",
      entry_port:"入口端口",sharing:"共享额度来源",wsl:"WSL 配置",storage_dir:"数据目录"};
    const card=$("restart-notice"),badge=$("restart-notice-open");
    let ledger={revision:0,items:[]},storageKey="",authoritative=false,ackLegacy=false,details=null,working=false,error="",storageError="",backendError="",renderKey="";
    function save() {
      try {localStorage.setItem(storageKey,JSON.stringify({ledger,ackLegacy,details}));storageError="";}
      catch(_) {storageError="待生效记录无法保存或读取，请勿据此判断配置已生效。";}
    }
    function identify(s) {
      const key="inspector.restart-notice.v1:"+(s?.data_dir||"")+":"+(s?.profile?.config_path||"");
      if(key===storageKey)return;
      storageKey=key;ledger={revision:0,items:[]};ackLegacy=false;details=null;error="";backendError="";storageError="";renderKey="";
      try {
        const saved=JSON.parse(localStorage.getItem(key)||"null");
        if(saved?.ledger&&Array.isArray(saved.ledger.items)) {
          ledger={revision:Number(saved.ledger.revision)||0,items:saved.ledger.items.filter(row=>row&&Object.hasOwn(labels,row.key)&&["chatgpt","wsl"].includes(row.target))};
          ackLegacy=!!saved.ackLegacy;details=saved.details||null;
        }
      } catch(_) {storageError="待生效记录无法保存或读取，请勿据此判断配置已生效。";}
    }
    function adopt(value) {
      if(!value||!Array.isArray(value.items)||!Number.isInteger(value.revision))return;
      if(value.revision<ledger.revision&&authoritative)return;
      const next={revision:value.revision,items:value.items.filter(row=>row&&Object.hasOwn(labels,row.key)&&["chatgpt","wsl"].includes(row.target))};
      const changed=JSON.stringify(next)!==JSON.stringify(ledger);ledger=next;
      backendError=value.error||"";
      if(authoritative&&status())status().restart_notice=value;
      if(changed)save();render();
    }
    function mark(key,target="chatgpt",scope="",before,after,changed=false) {
      if(authoritative)return;
      const index=ledger.items.findIndex(row=>row.key===key&&row.target===target&&(row.scope||"")===scope);
      const previous=index<0?null:ledger.items[index];
      if(changed&&before===after)return;
      const baseline=previous&&"before" in previous?previous.before:before;
      if(index>=0)ledger.items.splice(index,1);
      if(!changed||baseline!==after)ledger.items.push({key,target,scope,...(changed?{before:baseline,after}:{})});
      ledger.revision++;save();
    }
    function ingest(s=status()) {
      if(!s)return;
      identify(s);
      const wasAuthoritative=authoritative;authoritative=!!s.restart_notice;
      if(authoritative) {
        // A server's revision starts independently of an older page's fallback.
        if(!wasAuthoritative)ledger={revision:0,items:[]};
        adopt(s.restart_notice);
      } else {
        if(s.connection_reload_required&&!ackLegacy&&!ledger.items.some(row=>row.key==="connection"))mark("connection");
        if(s.entry_port?.pending&&!ledger.items.some(row=>row.key==="entry_port"))
          mark("entry_port","chatgpt","",s.entry_port.current_port,s.entry_port.configured_port,true);
      }
      render();
    }
    function change(key,before,after) {identify(status());error="";mark(key,"chatgpt","",before,after,true);render();}
    function connection(which,result={}) {
      identify(status());error="";
      const targets=Array.isArray(result.wsl?.targets)?result.wsl.targets:[];
      details={which,proxy:!!result.loopback_proxy?.changed,providers:result.provider_count,
        warnings:(Array.isArray(result.warnings)?result.warnings:[]).filter(row=>row?.reason==="unsupported_upstream")
          .slice(0,20).map(row=>({provider:String(row.provider||"").slice(0,160),file:String(row.file||"").slice(0,500),detail:String(row.detail||row.reason||"").slice(0,500)})),
        targets:targets.slice(0,32).map(row=>({distribution:String(row.distribution||"").slice(0,160),active:!!row.active})),
        wslWarnings:(Array.isArray(result.wsl?.warnings)?result.wsl.warnings:[]).slice(0,32)
          .map(row=>({distribution:String(row.distribution||"").slice(0,160),reason:String(row.reason||"").slice(0,100),detail:String(row.detail||"").slice(0,500)}))};
      if(result.restart_notice)adopt(result.restart_notice);
      else {
        const wslChanged=targets.some(row=>row.changed);
        const nativeChanged=!!(result.loopback_proxy?.changed||result.backup||result.backups?.length);
        if(result.local_reload_required??(nativeChanged||result.reload_required&&!wslChanged))mark("connection");
        for(const row of targets)if(row.changed)mark("wsl","wsl",String(row.distribution||""));
      }
      save();render();
    }
    function sharing(result) {
      if(result.restart_notice)adopt(result.restart_notice);
      else if(result.reload_required){identify(status());mark("sharing");render();}
    }
    function pendingLabel(row) {return t(labels[row.key])+(row.scope?" · "+row.scope:"");}
    function pendingField(row) {
      if(row.key!=="capacity_retry")return row.key;
      if(["capacity_retry_enabled","capacity_retry_max_retries"].includes(row.field))return row.field;
      const {before,after}=row;
      if(Number.isInteger(before)&&Number.isInteger(after)&&before!==after)return "capacity_retry_max_retries";
      if((before===null&&Number.isInteger(after))||(Number.isInteger(before)&&after===null))return "capacity_retry_enabled";
      return null;
    }
    function restartLabel(items,portPending=false) {
      const chatgpt=portPending||items.some(row=>row.target==="chatgpt"),optimizer=portPending||items.some(row=>row.target==="optimizer");
      return t(chatgpt&&optimizer?"ChatGPT 与本软件待重启":chatgpt?"ChatGPT 待重启":optimizer?"本软件待重启":"WSL 待重新连接");
    }
    function pendingTooltip(items,portPending) {
      const groups=new Map(),targets={chatgpt:"待重启 ChatGPT 生效设置：{items}",optimizer:"本软件：{items}",wsl:"WSL：{items}",port:"本软件 → ChatGPT App：{items}"};
      for(const row of items) {
        const target=row.key==="entry_port"&&portPending?"port":row.target;
        if(!groups.has(target))groups.set(target,new Set());
        groups.get(target).add(pendingLabel(row));
      }
      const separator=uiI18n.language==="en"?", ":"、";
      return Array.from(groups,([target,names])=>t(targets[target]).replace("{items}",()=>[...names].join(separator))).join("\n");
    }
    function render() {
      const items=[...ledger.items],s=status();
      if(s?.storage_paths_pending)items.push({key:"storage_dir",target:"optimizer",scope:""});
      const chatgpt=items.some(row=>row.target==="chatgpt"),wsl=items.some(row=>row.target==="wsl"),optimizer=items.some(row=>row.target==="optimizer");
      const portPending=!!s?.entry_port?.pending,problems=InspectorRestartNotice.connectionProblems(s,details),warning=problems.length>0;
      const log=s?.active_log_dir||s?.log_dir||s?.data_dir||"";
      const failure=error||backendError||storageError,active=items.length>0||!!failure;
      const autoDetect=!!s?.restart_notice?.auto_detect_supported;
      const nextKey=JSON.stringify([ledger.revision,items,details,s?.provider_warnings,s?.profile?.active,s?.wsl_connection,s?.connection_resume,autoDetect,portPending,problems,log,failure,working,!!busy(),uiI18n.language]);
      if(nextKey===renderKey)return;renderKey=nextKey;
      card.hidden=!active;
      card.dataset.state=active?"pending":"ready";
      badge.dataset.visible=String(active||warning);badge.tabIndex=active||warning?0:-1;
      badge.dataset.state=warning||failure?"error":"pending";
      const badgeLabel=warning?t("接入异常"):failure?t("状态异常"):items.length?restartLabel(items,portPending):"";
      $("restart-notice-label").textContent=badgeLabel;
      $("restart-notice-title").textContent=t(
        portPending?"先重启本软件，再重启 ChatGPT App":
        optimizer&&chatgpt?"重启本软件与 ChatGPT App 后生效":
        optimizer?"重启本软件后生效":
        chatgpt&&wsl?"重启 ChatGPT 并重新连接 WSL 后生效":
        chatgpt?"以下设置项在重启 ChatGPT 后生效":
        wsl?"重新连接 WSL 会话后生效":failure?"待生效状态暂不可用":"配置生效状态");
      badge.title=warning||failure?[...problems.map(row=>[row.scope,t(row.reason),row.detail].filter(Boolean).join(" · ")),
        failure?t(failure):"",log?t("日志目录：{path}").replace("{path}",()=>log):"",t("点击打开日志目录")].filter(Boolean).join("\n"):
        (items.length?pendingTooltip(items,portPending):"");
      badge.setAttribute("aria-label",[badgeLabel,badge.title,t(warning||failure?"打开日志目录":"查看待生效配置或接入错误")].filter(Boolean).join(" · "));
      $("restart-notice-items").replaceChildren(...items.map(row=>{
        const item=document.createElement("li");
        item.textContent=pendingLabel(row);
        return item;
      }));
      const message=$("restart-notice-message");
      const [messageStart,messageEnd=""]=chatgpt
        ?t(autoDetect&&!portPending&&!failure
          ?"检测到 ChatGPT 重启后自动清除提醒。未检测到时可手动确认 {confirm}"
          :"重启后可选择 {confirm}，清除提醒").split("{confirm}")
        :[t(wsl?"完成上述操作后再确认；确认只清除提醒。":optimizer?"重启本软件后，状态会自动更新。":"暂无待生效项目；新请求使用已保存的配置。")];
      $("restart-notice-message-start").textContent=failure?t(failure)+(chatgpt?" "+messageStart:""):messageStart;
      $("restart-notice-message-end").textContent=messageEnd+(chatgpt&&wsl&&!failure?" · "+t("WSL 仍需手动确认"):"");
      message.classList.toggle("error",!!failure);
      for(const target of ["chatgpt","wsl"]) {
        const button=$("restart-notice-"+target),needed=target==="chatgpt"?chatgpt:wsl;
        button.hidden=!needed;button.disabled=working||busy()||target==="chatgpt"&&portPending;
      }
      const pendingKeys=new Set(items.map(row=>row.key));
      const pendingFields=new Set(items.map(pendingField));
      for(const wrapper of document.querySelectorAll("[data-settings-field]")) {
        wrapper.classList.toggle("restart-pending-field",pendingFields.has(wrapper.dataset.settingsField));
      }
      $("capacity-retry-label").classList.toggle("restart-pending-label",pendingKeys.has("capacity_retry"));
      $("connection-settings").classList.toggle("restart-pending-field",["connection","sharing","wsl"].some(key=>pendingKeys.has(key)));
    }
    async function acknowledge(target) {
      if(working||busy()||target==="chatgpt"&&status()?.entry_port?.pending)return;
      working=true;error="";render();
      try {
        if(authoritative)adopt(await request("/api/restart-notice",{target,revision:ledger.revision}));
        else {ledger.items=ledger.items.filter(row=>row.target!==target);ledger.revision++;ackLegacy=true;save();}
      } catch(failure) {error=failure.message||String(failure);}
      finally {working=false;render();}
    }
    badge.addEventListener("click",async()=>{
      if(InspectorRestartNotice.connectionProblems(status(),details).length||error||backendError||storageError){await openLogs();return;}
      await openSettings();render();
      card.focus();
    });
    const localToggle = $("request-compression"), viewToggle = $("view-toggle");
    const badgeSizeObserver = new ResizeObserver(() => {
      const gap = parseFloat(getComputedStyle(badge.parentElement).columnGap) || 0;
      const available = localToggle.getBoundingClientRect().left - viewToggle.getBoundingClientRect().right - gap - 4;
      badge.style.setProperty("--restart-badge-max-width", `${Math.max(24, available)}px`);
    });
    for (const element of [badge.closest(".app-header"), viewToggle, localToggle, $("window-controls")]) {
      badgeSizeObserver.observe(element);
    }
    globalThis.addEventListener("pagehide", () => badgeSizeObserver.disconnect(), {once:true});
    for(const target of ["chatgpt","wsl"])$("restart-notice-"+target).addEventListener("click",()=>acknowledge(target));
    return {ingest,adopt,change,connection,sharing,render,acknowledge};
  }
};

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
let breakdownComparison=null;
const expandedBreakdownRows=new Set();
const nf = new Intl.NumberFormat("zh-CN");
const storageUsageFormat = new Intl.NumberFormat("zh-CN", {minimumFractionDigits:2, maximumFractionDigits:2});
let workspaceView="records",tokenTrendView=null,restoreSharingView=false;
let trendActivation=null;
let requestBodyCache=null, requestBodyExportUrl="", requestBodySequence=0;
let treeQueryTimer=null,requestBodyView=null,requestBodyHighlight=null;
let requestBodyTextMode=true;
let contextSettings = null;
const contextCustomValues = new Map();
let settingsEditing = false, settingsSaving = false, settingsOpening = false, settingsRevision = 0;
let linkSaving = "", linkRevision = 0;
let settingsBaseline = null, settingsLanguage = null;
let interfaceLanguageRevision = 0;
let settingsSaveProgress = {total:0,steps:[]};
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
const restartNotices = InspectorRestartNotice.create({status:()=>state.status,request:api,openSettings:()=>openSettings(),
  busy:()=>settingsSaving||linkSaving||compressionSaving||exitSaving||connectSaving||connectionResumeBusy(),
  openLogs:()=>openLogDirectory().catch(fail)});
globalThis.inspectorRestartNotices=restartNotices;
let connectionResumeSeen = "";
let exitRequest = null, exitSaving = false, exitFinished = false, exitRevision = 0;
let serviceRestartRequest = null, serviceRestartSaving = false, serviceRestartRevision = 0, serviceRestartAction = "";
let connectSource = null, connectSaving = false, connectChecking = false;
let firstRunWindow = null, firstRunChecking = false;
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
let sessionLabelWaiting = false, sessionLabelRevision = null, sessionMetadataRefreshQueued = false;
const sessionConnectionObserver=new ResizeObserver(()=>renderSessionConnections());
const OTHER_SESSION_GROUP="group:other";
const auxiliaryLabels={image_analysis:"图片解析",next_message_suggestion:"消息建议",title_generation:"自动标题",session_index:"会话索引",ambient_suggestions:"任务推荐",subagent:"子任务"};
const requestPurposeCache=new Map();
function auxiliarySessionKind(row) {return Object.hasOwn(auxiliaryLabels,row?.session_kind)?row.session_kind:row?.title_source==="index_task"?"session_index":row?.title_source==="subagent"?"subagent":null;}
function sessionTitle(row) {return typeof row?.title==="string"&&row.title.trim()?row.title:null;}
function knownSessionTitle(row) {return row?.title_source==="input"?null:sessionTitle(row);}
const SIDEBAR_SESSION_TITLE_LIMIT = 32;
const sidebarTitleSegmenter = typeof Intl.Segmenter === "function" ? new Intl.Segmenter() : null;
function sidebarSessionTitle(title) {
  const oneLine = title.replace(/\s+/g, " ").trim();
  if (oneLine.length <= SIDEBAR_SESSION_TITLE_LIMIT) return oneLine;
  const characters = sidebarTitleSegmenter
    ? Array.from(sidebarTitleSegmenter.segment(oneLine), ({segment}) => segment)
    : Array.from(oneLine);
  if (characters.length <= SIDEBAR_SESSION_TITLE_LIMIT) return oneLine;
  return characters.slice(0, SIDEBAR_SESSION_TITLE_LIMIT - 1).join("").trimEnd() + "…";
}
function sessionIdentifier(row) {
  const id=row?.thread_id;
  return typeof id==="string"&&id.trim()&&id!==OTHER_SESSION_GROUP?id:null;
}
function preserveSessionTitles(rows) {
  // Capture records can load while native names are temporarily unreadable.
  // Retain known names only for the same explicit session ID.
  const previous=new Map(sessionSummaries.map(row=>[row.thread_id,row]));
  return rows.map(row=>{
    if(typeof row.thread_id!=="string"||!row.thread_id)return row;
    const known=previous.get(row.thread_id),title=knownSessionTitle(row);
    if(!knownSessionTitle(known))return row;
    if(title&&!(row.title_source==="subagent"&&!["subagent","index_task"].includes(known.title_source)))return row;
    const merged={...row,title:known.title,title_cached:true};
    const kind=auxiliarySessionKind(row);if(kind)merged.session_kind=kind;
    if(known.title_source)merged.title_source=known.title_source;else delete merged.title_source;
    return merged;
  });
}
function sessionGroups() {
  if(!sessionMetadataAt)return [];
  const capturedIds=new Set(state.sessions);
  const rows=new Map(sessionSummaries.map(row=>[row.thread_id,row]));
  for(const id of capturedIds)if(!rows.has(id))rows.set(id,{thread_id:id});
  const imageParents=new Set([...rows.values()].filter(row=>auxiliarySessionKind(row)==="image_analysis"&&
    typeof row.parent_thread_id==="string"&&row.parent_thread_id&&row.parent_thread_id!==row.thread_id).map(row=>row.parent_thread_id));
  for(const id of imageParents)if(!rows.has(id))rows.set(id,{thread_id:id,packet_count:0});
  const groups=new Map();
  for(const member of rows.values()) {
    if(member.packet_count===0&&!capturedIds.has(member.thread_id))continue;
    const memberId=sessionIdentifier(member);
    let current=memberId?member:null,owner=null;const visited=new Set();
    for(let depth=0;current&&depth<32;depth++) {
      if(visited.has(current.thread_id))break;
      visited.add(current.thread_id);
      const kind=auxiliarySessionKind(current);
      const named=knownSessionTitle(current);
      if(sessionIdentifier(current)&&((kind==="subagent"&&named)||
        (!kind&&(named||current.has_conversation===true||current.codex_exec===true||
          imageParents.has(current.thread_id))))) {owner=current;break;}
      current=rows.get(current.parent_thread_id);
    }
    const id=owner?.thread_id||OTHER_SESSION_GROUP;
    if(!groups.has(id)) {
      const title=owner?sessionTitle(owner)||id:t("其他");
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
function sessionGroupMatchesQuery(group,query) {
  const memberSearch=group.members.map(member=>{
    const kindLabel=auxiliaryLabels[auxiliarySessionKind(member)];
    return [member.thread_id,member.title,kindLabel&&t(kindLabel),member.origin?.address].join(" ");
  });
  const id=group.thread_id===OTHER_SESSION_GROUP?"":group.thread_id;
  return [group.title,id,group.origin?.address,...memberSearch].join("\n").toLowerCase().includes(query);
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
  const jobs=rows.filter(row=>!knownSessionTitle(row)&&!auxiliarySessionKind(row)&&row.latest_packet?.id&&!requestPurposeCache.has(row.latest_packet.id)).slice(0,8);
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
    const purpose=!knownSessionTitle(row)&&requestPurposeCache.get(row.latest_packet?.id);
    return purpose?{...row,session_kind:purpose}:row;
  });
}
let sessionLegacyCursor = 0;
const dateFormat = new Intl.DateTimeFormat("zh-CN",{month:"2-digit",day:"2-digit"});
const minuteFormat = new Intl.DateTimeFormat("zh-CN",{hour:"2-digit",minute:"2-digit",hourCycle:"h23"});
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
async function sessionPacketPage(query,signal) {
  const selectedKind=query.get("kind");
  if(["generation","image_analysis","other"].includes(selectedKind)) {
    // Older relays group auxiliary POSTs with generation; refine by the displayed type.
    const scan=new URLSearchParams(query),matches=[],limit=Number(query.get("limit")||100);
    scan.set("limit","300");
    if(selectedKind==="other"){scan.delete("kind");scan.set("include_get","1");}
    else scan.set("kind","generation");
    for(;;) {
      signal?.throwIfAborted();
      const page=await rawSessionPacketPage(scan,signal);
      signal?.throwIfAborted();
      matches.push(...page.filter(packet=>requestFilterKind(packet)===selectedKind));
      if(matches.length>=limit||page.length<300)return matches.slice(0,limit);
      scan.set("after",page.at(-1).id);
    }
  }
  return rawSessionPacketPage(query,signal);
}
async function rawSessionPacketPage(query,signal) {
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
function originalBytes(p) { return Number.isFinite(p.decoded_bytes) && p.decoded_bytes >= 0 ? p.decoded_bytes : null; }
function compact(n) { if (n === undefined || n === null) return "—"; return n >= 1000000 ? (n/1000000).toFixed(2)+" MB" : n >= 1000 ? (n/1000).toFixed(1)+" KB" : n+" B"; }
let recordScope = null, recordScopePending = false;
function renderRecordScope() {
  const empty = state.status?.storage?.count === 0;
  const scope = empty ? null : recordScope;
  const count = empty ? 0 : scope?.recorded_sessions;
  setText($("recorded-session-count"), count === undefined ? "—" : t("{count} 个会话").replace("{count}", nf.format(count)));
  const range = $("recorded-time-range");
  const first = scope?.first_recorded_at, last = scope?.last_recorded_at;
  const title = t("记录时间范围");
  if (!Number.isFinite(first) || !Number.isFinite(last) || first < 0 || last < first || last > 8.64e12) {
    setText(range, "—");
    range.title = title;
    return;
  }
  const start = new Date(first * 1000), end = new Date(last * 1000);
  const startLabel = dateFormat.format(start) + " " + minuteFormat.format(start);
  const endLabel = dateFormat.format(end) + " " + minuteFormat.format(end);
  setText(range, startLabel === endLabel ? startLabel : startLabel + " – " + endLabel);
  range.title = title + " · " + timestampFormat.format(start) + (first === last ? "" : " – " + timestampFormat.format(end));
}
async function refreshRecordScope(storage) {
  const revision = storage?.revision;
  if (!storage?.count || recordScopePending || (Number.isSafeInteger(revision) && recordScope?.revision === revision)) return;
  recordScopePending = true;
  try {
    const summary = await api("/api/tray-status", undefined, AbortSignal.timeout(2500));
    const {recorded_sessions, first_recorded_at, last_recorded_at} = summary;
    if (!Number.isSafeInteger(recorded_sessions) || recorded_sessions < 0) return;
    recordScope = {revision, recorded_sessions, first_recorded_at, last_recorded_at};
    renderRecordScope();
  } catch {
    // Keep the last known scope during brief relay outages.
  } finally {
    recordScopePending = false;
  }
}
function renderStorageInfo() {
  const {count, stored} = state.status?.storage ?? {};
  setText($("packet-limit-usage"), Number.isSafeInteger(count) && count >= 0 ? nf.format(count) : "—");
  setText($("storage-limit-usage"), Number.isFinite(stored) && stored >= 0
    ? storageUsageFormat.format(stored / 1_000_000) : "—");
  renderRecordScope();
}
function renderRecordLimit(input) {
  const value = input.valueAsNumber;
  const formatted = input.parentElement.querySelector(".record-limit-formatted");
  setText(formatted, input.validity.valid && value >= 1000 ? nf.format(value) : "");
}
function signed(n) { return (n > 0 ? "+" : "") + bytes(n); }
function kind(n) { return names[n] || n; }
function hasModelTokenUsage(p) {
  return p.method==="POST" && !p.is_compaction && p.request_kind!=="tool_search" &&
    (!p.path || /\/responses\/*(?:\?.*)?$/.test(p.path));
}
function isToolSearch(p) { return p?.request_kind==="tool_search" || /\/alpha\/search\/*(?:\?.*)?$/.test(p?.path||""); }
function requestKind(p) {
  if(p.request_kind==="image_generation" || /\/images\/generations\/*(?:\?.*)?$/.test(p.path||""))return t("图片生成");
  if(p.request_kind==="image_edit" || /\/images\/edits\/*(?:\?.*)?$/.test(p.path||""))return t("图片编辑");
  if(isToolSearch(p))return t("工具搜索");
  if(p.is_compaction)return t("上下文压缩");
  const purpose=purposeFromMetadata(p)||requestPurposeCache.get(p.id)||auxiliarySessionKind(sessionSummaries.find(row=>row.thread_id===p.thread_id));
  return purpose&&purpose!=="subagent"?t(auxiliaryLabels[purpose]):p.method==="POST"?t("对话生成"):String(p.method||t("未知")).slice(0,10);
}
const requestKinds = uiI18n.labels({generation:"对话生成",compaction:"上下文压缩",image_analysis:"图片解析",image_generation:"图片生成",image_edit:"图片编辑",other:"其他请求"});
function requestFilterKind(packet) {
  const label=requestKind(packet);
  return Object.keys(requestKinds).find(value=>value!=="other"&&requestKinds[value]===label)||"other";
}
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
  setFilterOptions("kind",["generation","compaction","image_analysis",...["image_generation","image_edit"].filter(value=>options.kinds.includes(value)),"other"].map(value=>({value,label:requestKinds[value]})));
  setFilterOptions("status",options.statuses.map(value=>({value,label:statusLabel(value)})));
  state.sessions=options.threads;
  state.filterRevision=revision;
  renderStorageInfo();renderSessions();
}
async function watchSessionLabels(revision) {
  if(!Number.isSafeInteger(revision)||document.hidden)return;
  sessionLabelRevision=revision;
  if(sessionLabelWaiting)return;
  sessionLabelWaiting=true;
  let changed=false;
  try {
    while(sessionLabelRevision!==null&&!document.hidden) {
      const after=sessionLabelRevision;sessionLabelRevision=null;
      const update=await api("/api/session-labels/wait?after="+after,undefined,AbortSignal.timeout(20000));
      changed=update.changed===true;
      if(changed)break;
      if(update.pending===true)sessionLabelRevision??=after;
    }
  } catch(_) {} finally {
    sessionLabelWaiting=false;sessionLabelRevision=null;
  }
  if(changed)await refreshSessionSummaries(true);
}
async function refreshSessionSummaries(fromLabels=false) {
  if(!state.status||(!fromLabels&&Date.now()-sessionMetadataAt<5000))return;
  if(sessionMetadataLoading) {
    if(fromLabels)sessionMetadataRefreshQueued=true;
    return;
  }
  const membership=()=>JSON.stringify(sessionGroups().map(group=>[group.thread_id,group.members.map(row=>row.thread_id).sort()]));
  const previousMembers=membership();
  sessionMetadataLoading=true;
  try {
    if(fromLabels || sessionMetadataSupported!==false || Date.now()-sessionMetadataProbeAt>=30000) {
      sessionMetadataProbeAt=Date.now();
      try {
        const result=await api("/api/session-summaries",undefined,AbortSignal.timeout(5000));
        if(result.state==="available" && Array.isArray(result.sessions)) {
          sessionSummaries=preserveSessionTitles(await enrichSessionPurposes(result.sessions));
          sessionMetadataSupported=true;
          watchSessionLabels(result.label_revision).catch(fail);
          return;
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
    sessionSummaries=(await enrichSessionPurposes(results)).map(row=>knownSessionTitle(row)?{...row,title_cached:true}:row);
  } finally {
    sessionMetadataAt=Date.now();sessionMetadataLoading=false;
    if(sessionMetadataRefreshQueued) {
      sessionMetadataRefreshQueued=false;
      queueMicrotask(()=>refreshSessionSummaries(true).catch(fail));
    }
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
    const item=nodes.get("thread:"+id),heading=item.querySelector(".session-heading,.session-id").getBoundingClientRect();
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
function showCopyFeedback(status,result) {
  status.dataset.copyFeedback=result;
  status.textContent=t(result==="success"?"已复制":"复制失败");
}
function clearCopyFeedback(status) {
  clearTimeout(status.copyFeedbackTimer);
  delete status.dataset.copyFeedback;
  status.textContent="";
}
async function copyWithFeedback(text,status) {
  const request=Symbol();
  status.copyRequest=request;
  let result="success";
  try {await navigator.clipboard.writeText(text);}
  catch(_) {result="error";}
  if(!status.isConnected||status.copyRequest!==request)return;
  clearCopyFeedback(status);
  showCopyFeedback(status,result);
  status.copyFeedbackTimer=setTimeout(()=>clearCopyFeedback(status),result==="success"?900:1600);
}
function sessionCopy(field,text,previousFeedback) {
  const button=el("button","session-copy"),label=t(field==="id"?"复制会话 ID":"复制会话名称");
  button.type="button";button.setAttribute("aria-label",label);
  button.dataset.sessionCopy=field;
  const icon=document.createElementNS("http://www.w3.org/2000/svg","svg"),path=document.createElementNS(icon.namespaceURI,"path");
  const check=document.createElementNS(icon.namespaceURI,"path");
  icon.setAttribute("viewBox","0 0 24 24");icon.setAttribute("aria-hidden","true");
  path.setAttribute("d","M9 9h11v11H9zM15 6V4H4v11h2");
  check.setAttribute("d","M5 12l4 4L19 6");
  check.classList.add("session-copy-check");
  icon.append(path,check);button.append(icon);
  const feedback=el("span","session-copy-feedback");
  feedback.setAttribute("role","status");
  if(previousFeedback)showCopyFeedback(feedback,previousFeedback);
  button.onclick=event=>copyWithFeedback(text,event.currentTarget.nextElementSibling);
  return [button,feedback];
}
function bindSessionTitleTooltip(list,popup) {
  const titleSelector=".session-title[data-full-title]",popover=typeof popup.showPopover==="function";
  const sidebar=list.closest(".sessions-sidebar");
  if(!popover)popup.removeAttribute("popover");
  let title=null,timer=null;
  function hide() {
    clearTimeout(timer);title=null;
    if(popover&&popup.matches(":popover-open"))popup.hidePopover();
    popup.hidden=true;
  }
  function show(label) {
    clearTimeout(timer);
    if(label===title&&!popup.hidden&&(!popover||popup.matches(":popover-open")))return;
    hide();
    title=label;
    timer=setTimeout(()=>{
      if(!label.dataset.fullTitle||!label.getClientRects().length){hide();return;}
      popup.textContent=label.dataset.fullTitle;
      popup.hidden=false;
      if(popover)popup.showPopover();
      const margin=8;
      const left=Math.max(margin,Math.min(sidebar.getBoundingClientRect().right+4,innerWidth-popup.offsetWidth-margin));
      const top=Math.max(margin,Math.min(label.getBoundingClientRect().top-2,innerHeight-popup.offsetHeight-margin));
      popup.style.inset=`${top}px auto auto ${left}px`;
    },300);
  }
  function leave() {
    clearTimeout(timer);
    timer=setTimeout(hide,180);
  }
  list.addEventListener("pointerover",event=>{
    const label=event.target.closest(titleSelector);
    if(label&&event.pointerType!=="touch")show(label);
  });
  list.addEventListener("pointerout",event=>{
    if(event.target===title&&!popup.contains(event.relatedTarget))leave();
  });
  popup.addEventListener("pointerenter",()=>clearTimeout(timer));
  popup.addEventListener("pointerleave",leave);
  list.addEventListener("focusin",event=>{
    const label=event.target.matches(".session-item:focus-visible")&&event.target.querySelector(titleSelector);
    if(label)show(label);
  });
  list.addEventListener("focusout",event=>{if(title&&event.target.contains(title))hide();});
  list.addEventListener("scroll",hide,{passive:true});
  window.addEventListener("resize",hide);
  document.addEventListener("keydown",event=>{if(event.key==="Escape"&&title)hide();});
  return hide;
}
const hideSessionTitleTooltip=bindSessionTitleTooltip($("session-list"),$("session-title-tooltip"));
function renderSessions() {
  const list=$("session-list");
  if(!state.status)return;
  const trends=$("records-view").dataset.view==="trends";
  const groups=sessionGroups().filter(group=>!trends||group.thread_id!==OTHER_SESSION_GROUP);
  const summaries=new Map(groups.map(group=>[group.thread_id,group])),connections=sessionConnections(groups,state.filters.thread);
  const ids=groups.map(group=>group.thread_id),selected=state.filters.thread;
  ids.sort((a,b)=>(summaries.get(b)?.updated_at||0)-(summaries.get(a)?.updated_at||0));
  const query=$("session-search").value.trim().toLowerCase();
  const signature=JSON.stringify([ids,selected,groups,sessionMetadataSupported,!!sessionMetadataAt,query]);
  if(signature===state.sessionVersion)return;
  state.sessionVersion=signature;
  const scrollTop=list.scrollTop, bounds=list.getBoundingClientRect();
  const items=Array.from(list.querySelectorAll(".session-item"));
  const visible=item=>{const rect=item.getBoundingClientRect();return rect.bottom>bounds.top&&rect.top<bounds.bottom;};
  // Keep a visible selected session in place when metadata changes the ordering.
  const selectedItem=items.find(item=>item.dataset.session===selected);
  const anchor=selectedItem&&visible(selectedItem)?selectedItem:items.find(visible);
  const anchorId=anchor?.dataset.session, anchorOffset=anchor?anchor.getBoundingClientRect().top-bounds.top:0;
  const focused=list.contains(document.activeElement)?document.activeElement:null;
  const focusedId=focused?.closest(".session-row")?.dataset.session;
  const copyFocused=focused?.dataset.sessionCopy;
  const copyFeedback=new Map(Array.from(list.querySelectorAll(".session-copy-feedback[data-copy-feedback]"),status=>[
    status.parentElement.dataset.session+" "+status.previousElementSibling.dataset.sessionCopy,status.dataset.copyFeedback
  ]));
  const fragment=el("div","session-items");
  for(const id of ids) {
    const otherGroup=id===OTHER_SESSION_GROUP;
    const value=otherGroup?OTHER_SESSION_GROUP:id?"thread:"+id:"unknown", data=summaries.get(id)||{};
    const title=data.title,idOnly=!otherGroup&&title===id;
    const origin=data.origin?.address;
    if(query&&!sessionGroupMatchesQuery(data,query))continue;
    const row=el("div",idOnly?"session-row session-row-id-only":"session-row");row.dataset.session=value;
    tokenTrendView?.styleSessionRow(row);
    const item=el("button","session-item"+(otherGroup?" other-session-group":"")+(value===selected?" selected":""));
    item.type="button";item.dataset.session=value;
    const label=[title];
    if(!otherGroup&&id&&!idOnly)label.push(id);
    if(origin)label.push(origin);
    if(data.title_cached)label.push(t("会话名称暂不可用，显示上次加载的名称"));
    for(const link of connections) {
      if(link.child===id)label.push(t("父会话名称")+"："+summaries.get(link.parent).title);
      if(link.parent===id)label.push(t("子任务")+"："+summaries.get(link.child).title);
    }
    item.setAttribute("aria-label",label.join("\n"));
    if(value===selected)item.setAttribute("aria-current","page");
    if(!idOnly) {
      const heading=el("div","session-heading"),name=el("span","session-title",sidebarSessionTitle(title));
      if(!otherGroup)name.dataset.fullTitle=title;
      heading.append(name);
      item.append(heading);
    }
    if(!otherGroup&&id)item.append(el("code","session-id",id));
    const updated=Number.isFinite(data.updated_at)?new Date(data.updated_at*1000):null;
    const meta=el("div","session-meta");
    if(!otherGroup) {
      const tokens=data.token_window, input=sortInputTokens(tokens);
      const tokenText=input===null?"—":(tokens.estimated?"≈ ":"")+nf.format(input);
      meta.append(el("span","session-token","Token "+tokenText));
      if(origin)meta.append(el("span","session-origin has-address",origin));
    }
    meta.append(el("span","session-updated",updated?dateFormat.format(updated)+" "+clockFormat.format(updated):"—"));
    item.append(meta);
    bindSelectableRow(item,()=>{
      if(hasTextSelectionWithin($("session-list")))window.getSelection().removeAllRanges();
      tokenTrendView?.revealSession(value);
      return selectSession(value);
    });
    row.append(item);
    if(!otherGroup) {
      if(!idOnly)row.append(...sessionCopy("title",title,copyFeedback.get(value+" title")));
      if(id)row.append(...sessionCopy("id",id,copyFeedback.get(value+" id")));
    }
    fragment.append(row);
  }
  if(!fragment.childElementCount) {
    if(!sessionMetadataAt) {
      fragment.classList.add("session-loading");
      const spinner=el("span","session-spinner");spinner.setAttribute("aria-hidden","true");
      const status=el("span","sr-only",t("加载中…"));status.setAttribute("role","status");
      fragment.append(spinner,status);
    } else {
      const empty=el("p","session-empty",query?t("未找到匹配的会话"):t("暂无会话"));
      empty.setAttribute("role","status");fragment.append(empty);
    }
  }
  sessionConnectionObserver.disconnect();
  list.setAttribute("aria-busy",String(!sessionMetadataAt));
  reconcileContent(list,fragment);
  renderSessionConnections();
  const sessionItems=list.querySelector(".session-items");
  if(sessionItems)sessionConnectionObserver.observe(sessionItems);
  const updatedItems=Array.from(list.querySelectorAll(".session-item"));
  const nextAnchor=updatedItems.find(item=>item.dataset.session===anchorId);
  const focusedItem=updatedItems.find(item=>item.dataset.session===focusedId);
  const nextFocus=copyFocused?focusedItem?.parentElement.querySelector(`[data-session-copy="${copyFocused}"]`):focusedItem;
  $("session-metadata-note").hidden=sessionMetadataSupported!==false;
  if(nextFocus&&nextFocus!==document.activeElement)nextFocus.focus({preventScroll:true});
  const currentScroll=list.scrollTop;
  const restoredScroll=nextAnchor
    ?currentScroll+nextAnchor.getBoundingClientRect().top-list.getBoundingClientRect().top-anchorOffset
    :scrollTop;
  // Keep an in-progress smooth scroll when the refreshed rows have the same position.
  if(Math.abs(restoredScroll-currentScroll)>=.5)list.scrollTop=restoredScroll;
  tokenTrendView?.scheduleRender();
}
async function selectSession(value) {
  if(value===state.filters.thread)return;
  cancelTokenRead();tokenWindow=null;
  state.filters.thread=value;
  state.listController?.abort();state.listRequest++;
  state.detailRequest++;
  state.selected=null;state.detail=null;state.detailVersion="";state.text=null;
  breakdownComparison=null;expandedBreakdownRows.clear();
  requestBodyCache=null;requestBodyView=null;requestBodySequence++;clearJsonFocus();clearJsonActions();
  state.packets=[];state.older=false;state.canLoadMore=false;state.listRevision=null;
  $("packet-detail").hidden=true;$("empty-state").hidden=false;
  $("packet-list").scrollTop=0;
  renderSessions();renderList();
  await loadPackets();
}
function message(text, error=false) {
  const notice=$("notice");uiI18n.text($("notice-text"),text);notice.className="notice"+(error?" error":"");
  delete notice.dataset.recoverEndpoint;delete notice.dataset.recoverSequence;
  notice.dataset.feedbackTarget=$("settings-dialog").open?"settings-inline-feedback":"app-inline-feedback";
  notice.hidden=false;
}
function fail(err) {
  message(err.message || String(err), true);
  if(err.message==="forwarding_service_unavailable"&&err.requestSequence) {
    const notice=$("notice");notice.dataset.recoverEndpoint=err.requestKey;notice.dataset.recoverSequence=String(err.requestSequence);
  }
}
let apiRequestSequence=0;
async function api(path, data, signal) {
  const method=data===undefined?"GET":"POST",requestKey=method+" "+path.split("?")[0],requestSequence=++apiRequestSequence;
  const r=await fetch(path,{method,headers:data===undefined?{}:{"Content-Type":"application/json"},body:data===undefined?undefined:JSON.stringify(data),signal});
  const value=await r.json();
  if(!r.ok)throw Object.assign(Error(value.error||("HTTP "+r.status)),{status:r.status,requestKey,requestSequence});
  const notice=$("notice");
  if(notice.dataset.recoverEndpoint===requestKey&&requestSequence>Number(notice.dataset.recoverSequence)) {
    notice.hidden=true;delete notice.dataset.recoverEndpoint;delete notice.dataset.recoverSequence;
  }
  return value;
}
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
function panel(title,{headingTag="h3"}={}) {
  const n=el("section","detail-section");
  n.dataset.renderKey="panel:"+title;
  n.append(el(headingTag,"",title));
  return n;
}
function table(headings, rows) {
  const wrap=el("div","table-scroll"), grid=el("table"), head=grid.createTHead().insertRow(), body=grid.createTBody();
  for(const heading of headings) {
    const cell=el("th","",heading);
    cell.scope="col";
    head.append(cell);
  }
  for(const cells of rows) {
    const row=body.insertRow();
    for(const cell of cells)row.insertCell().append(cell);
  }
  wrap.append(grid);
  return wrap;
}
function link(text, fn) { const n=el("button","link-button",text); n.onclick=()=>Promise.resolve().then(fn).catch(fail); return n; }
function fact(n,label,value) {
  const row=el("div","fact-row"),description=n.localName==="dl";
  row.dataset.renderKey="fact:"+label;
  row.append(el(description?"dt":"span","",label),el(description?"dd":"span","",value));
  n.append(row);
  return row;
}
function showTab(tab) {
  const basic=!!state.detail?.details_evicted;
  let refocus=false;
  if(basic&&tab==="tree") {
    const focused=document.activeElement;
    refocus=$("request-tab-"+tab)===focused||$("tab-"+tab).contains(focused);
    tab="headers";
  }
  state.tab=tab;
  for(const button of $("request-info-tabs").querySelectorAll("[data-tab]")) {
    const key=button.dataset.tab,active=key===tab;
    button.hidden=basic&&key==="tree";
    button.setAttribute("aria-selected",String(active));
    button.tabIndex=active?0:-1;
    $("tab-"+key).hidden=!active;
  }
  if(tab==="tree")scrollRequestTextOnFirstShow();
  if(tab==="overview"&&state.detail)loadBreakdownComparison(state.detail).catch(fail);
  if(refocus)$("request-tab-headers").focus();
}
function packetStatus(p) {
  if(p.state==="relay_error" || p.status>=400)return "failed";
  if(["client_disconnected","interrupted"].includes(p.state))return "interrupted";
  if(p.state==="streaming")return "streaming";
  if(p.state==="complete")return "complete";
  return "waiting";
}
function autoRetryCount(p) {
  return [p.capacity_retry_count,p.bad_request_retry_count].reduce((sum,n)=>sum+(Number.isInteger(n)&&n>0?n:0),0);
}
function packetProtection(p) {
  return {
    predictive:p.usage_override_applied===true,
    overflow:p.overflow_compaction_calls>0 || p.overflow_cache_hit===true ||
      typeof p.relay_error==="string" && p.relay_error.startsWith("overflow_"),
    retry:p.native_retry_advised===true||autoRetryCount(p)>0,
    optimization:p.image_isolation_applied===true||Number.isSafeInteger(p.image_isolation_removed)&&p.image_isolation_removed>0
  };
}
const protectionLabels={predictive:"大小预测",overflow:"超限压缩",retry:"容量重试",optimization:"解图优化"};
function renderProtectionMarkers(flags) {
  const markers=el("span","packet-protection-markers"),labels=[];
  for(const [key,label] of Object.entries(protectionLabels))if(flags[key]) {
    const marker=el("span","packet-protection-marker");marker.dataset.mechanism=key;marker.dataset.renderKey="protection-marker:"+key;
    marker.title=t(label)+" · "+t("已触发");marker.setAttribute("aria-hidden","true");markers.append(marker);labels.push(t(label));
  }
  if(!labels.length)return null;
  markers.setAttribute("role","img");markers.setAttribute("aria-label",labels.join(" · ")+" · "+t("已触发"));return markers;
}
function imageOptimizationRows(packets) {
  const seen=new Map(),marked=new Set();
  const chronological=[...packets].sort((a,b)=>a.created-b.created||String(a.id).localeCompare(String(b.id)));
  for(const packet of chronological) {
    if(!packetProtection(packet).optimization)continue;
    const session=packet.thread_id||"";
    if(!seen.has(session))seen.set(session,new Set());
    const images=seen.get(session);
    for(const path of requestImagePaths(packet,null)) {
      const key=/^(?:[A-Za-z]:[/\\]|\\\\|\/\/)/.test(path)?path.replace(/\\/g,"/").replace(/\/+/g,"/").toLowerCase():path;
      if(!images.has(key)){images.add(key);marked.add(packet.id);}
    }
  }
  return marked;
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
  const signature=JSON.stringify([state.selected,state.status.budget_bytes,state.older,state.canLoadMore,state.sort,state.direction,state.filters,state.packets.map(p=>[p.id,p.created,p.method,p.is_compaction,requestKind(p),p.state,p.status,p.duration_ms,p.decoded_bytes,p.outbound_wire_bytes,p.outbound_content_encoding,p.thread_id,p.overflow_compaction_calls,p.overflow_cache_hit,p.overflow_original_wire_bytes,p.usage_override_applied,p.capacity_retry_count,p.bad_request_retry_count,p.native_retry_advised,p.native_retry_delay_ms,p.relay_error,p.upstream_usage?.input_tokens,p.image_isolation_applied,p.image_isolation_removed,p.image_paths,p.image_path_rewrites])]);
  if(signature===state.listVersion)return;
  state.listVersion=signature;
  const root=$("packet-rows"), fragment=document.createDocumentFragment(); renderSort();
  listTokenObserver.disconnect();visibleTokenRows.clear();listTokenCells.clear();
  const optimizedImages=imageOptimizationRows(state.packets);
  for (const p of state.packets) {
    const flags={...packetProtection(p),optimization:optimizedImages.has(p.id)},protection=Object.entries(protectionLabels).filter(([key])=>flags[key]).map(([,label])=>t(label));
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
    if(flags.retry)row.title+="\n"+(p.native_retry_advised===true?t("已向 Codex 发送重试提示"):t("自动重试：{count} 次").replace("{count}",nf.format(autoRetryCount(p))));
    if(protection.length)after.title+=t("\n已触发请求保护：")+protection.join(" · ");
    for (const [index,value] of [time,type,before].entries()) {
      const td=el("td",index===1?"packet-kind":"");
      if(value instanceof Node)td.append(value);else td.textContent=value;
      row.append(td);
    }
    const compressed=el("td","packet-compressed"),markers=renderProtectionMarkers(flags);
    compressed.title=after.title;compressed.append(after);if(markers)compressed.append(markers);row.append(compressed);
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
    else if (state.selected) { const p=state.packets.find(p=>p.id===state.selected); if(p) {const ver=packetDetailVersion(p);if(ver!==state.detailVersion) await selectPacket(state.selected,true);} else await selectPacket(state.selected,true); }
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
  const revision=settingsRevision, connectionRevision=linkRevision, modeRevision=compressionRevision, quittingRevision=exitRevision, restartRevision=serviceRestartRevision;
  let status;
  try {status=await api("/api/status");}
  catch(error) {
    try {const result=await api("/api/service-restart");if(restartRevision===serviceRestartRevision)renderServiceRestart(result.request);}catch {}
    throw error;
  }
  if(settingsSaving||compressionSaving||exitSaving||serviceRestartSaving||revision!==settingsRevision||connectionRevision!==linkRevision||modeRevision!==compressionRevision||quittingRevision!==exitRevision||restartRevision!==serviceRestartRevision)return;
  state.status=status;
  if(!settingsEditing&&["zh","en"].includes(status.ui_language)&&uiI18n.language!==status.ui_language)
    await setInterfaceLanguage(status.ui_language);
  renderStatus(fill);
  void refreshRecordScope(status.storage);
}
function renderConnectionActions() {
  const s=state.status;if(!s)return;
  renderCompressionMode();
}
function renderStatus(fill=false) {
  const s=state.status;if(!s)return;
  renderSharingNavigation(s);
  $("desktop-build").hidden=!s.ui_live_reload;
  $("ui-refresh").hidden=!s.ui_live_reload;
  renderConnectionActions();
  $("empty-connect").hidden=!!s.profile.active;
  renderStorageInfo();
  if(fill) fillSettings();else {fillEntryPort();renderCompressionMode();renderLanguageSelection();}
  renderSessions();renderList();
  renderProtectionPanelState();
  renderExitRequest();
  renderServiceRestart(s.ui_restart_request);
  restartNotices.ingest(s);
  renderConnectionResume();
  renderConnectPrompt();
  offerFirstConnection().catch(fail);
}
function storageLimitSettings(status) {
  const bytesPerMB=1_000_000;
  const min=Math.ceil((status.storage_limit_min_bytes??17*bytesPerMB)/bytesPerMB);
  const max=Math.floor((status.storage_limit_max_bytes??8590*bytesPerMB)/bytesPerMB);
  const size=status.max_storage_bytes??status.storage?.max_bytes??500*bytesPerMB;
  const value=Math.min(max,Math.max(min,Math.ceil(size/bytesPerMB)));
  return {value,min,max,toBackend:value=>Number(value)*bytesPerMB};
}
function fillSettings() {
  const s=state.status,p=s.profile;
  const storage=storageLimitSettings(s),storageInput=$("storage-limit");
  const sharingTabSupported=typeof s.sharing_tab_enabled==="boolean",sharingTab=$("sharing-tab-enabled");
  storageInput.min=storage.min;storageInput.max=storage.max;
  $("packet-limit").max=s.packet_limit_max??20000;
  fillEntryPort();
  if(!settingsEditing) {
    storageInput.value=storage.value;$("packet-limit").value=s.max_packets;
    $("start-on-login").checked=!!s.start_on_login;
    sharingTab.checked=s.sharing_tab_enabled===true;
    $("sharing-port").value=s.sharing_port??47846;
    $("exit-port").value=s.egress_port??47847;
  }
  for(const id of ["storage-limit","packet-limit"]){
    const input=$(id);
    input.disabled=settingsSaving;
    renderRecordLimit(input);
  }
  $("sharing-settings").hidden=!s.sharing_settings_supported;
  for(const id of ["sharing-port","exit-port"])$(id).disabled=settingsSaving||!s.sharing_settings_supported;
  $("sharing-tab-setting").hidden=!sharingTabSupported;
  sharingTab.disabled=settingsSaving||!sharingTabSupported;
  renderSharingTabHint();
  $("start-on-login").disabled=settingsSaving||!s.start_on_login_supported;
  fillCapacityRetry();
  fillBadRequestRetry();
  fillProxySettings();
  fillImageAnalysisMode();
  $("startup-hint").textContent=t(s.start_on_login_supported?(s.startup_state_known===false?"无法读取开机启动状态，请重新保存设置。":"登录 Windows 后自动启动。"):"开机启动仅支持已安装的 Windows 版本。");
  $("runtime-settings-message").hidden=!!s.capacity_retry_supported;
  $("runtime-settings-message").textContent=s.capacity_retry_supported?"":t("当前中转暂不支持此功能。");
  renderConnectionActions();
  renderCompressionMode();renderLanguageSelection();fillZstdLevel();fillStorageDirectories();fillPacketProtection();syncContextControls();
  updateSettingsSaveState(false);
}
function renderSharingTabHint() {
  uiI18n.text($("sharing-tab-hint"),$("sharing-tab-enabled").checked
    ?"显示额度与网络共享页面":"隐藏额度与网络共享页面");
}
function selectedProxyMode() {
  return $("proxy-mode").querySelector("input:checked")?.value??"auto";
}
function proxySettingsDraft() {
  if(!state.status.proxy_settings_supported)return "";
  const mode=selectedProxyMode();
  return JSON.stringify(mode==="auto"?{mode}:{mode,address:$("proxy-address").value});
}
function fillProxySettings(force=false) {
  const s=state.status,proxy=s.proxy_settings,supported=!!s.proxy_settings_supported&&!!proxy;
  $("proxy-settings").hidden=!supported;
  if(!supported)return;
  if(!settingsEditing||force) {
    for(const input of $("proxy-mode").querySelectorAll("input"))input.checked=input.value===proxy.mode;
    $("proxy-address").value=proxy.manual.address;
  }
  const manual=selectedProxyMode()==="manual",detected=proxy.detected;
  $("proxy-manual").hidden=!manual;$("proxy-detected").hidden=manual;
  for(const input of $("proxy-mode").querySelectorAll("input"))input.disabled=settingsSaving;
  $("proxy-address").disabled=settingsSaving||!manual;
  const testing=$("proxy-test").getAttribute("aria-busy")==="true";
  $("proxy-test").disabled=settingsSaving||testing;$("proxy-test").textContent=t(testing?"测试中…":"测试");
  const scheme=s.profile?.upstream?.toLowerCase().startsWith("http://")?"http":"https";
  const value=detected[scheme]??detected.https??detected.http;
  const address=value?.invalid?t("代理地址无效"):value?.address||t("直接连接");
  $("proxy-detected-address").value=address;$("proxy-detected-address").title=address;
  const warnings=[];
  if(detected.pac_url||detected.auto_discovery)warnings.push(t("检测到 PAC/自动发现。转发依照 HTTP/HTTPS 代理配置。")+(detected.pac_url?" "+detected.pac_url:""));
  if(detected.unsupported_all_proxy)warnings.push(t("ALL_PROXY 未用于转发，请配置 HTTP/HTTPS 代理。"));
  $("proxy-detection-warning").textContent=warnings.join(" ");$("proxy-detection-warning").hidden=manual||!warnings.length;
}
function setProxyTestStatus(status="",elapsedMs) {
  const labels={ok:"已连通",rejected:"响应异常",failed:"连接失败",timeout:"测试超时",stale:"需重测",error:"测试失败"};
  const output=$("proxy-test-result");
  const elapsed=$("proxy-test-elapsed");
  const showElapsed=status==="ok"&&Number.isFinite(elapsedMs)&&elapsedMs>=0;
  output.classList.toggle("success",status==="ok");
  output.classList.toggle("error",Boolean(status)&&status!=="ok"&&status!=="stale");
  uiI18n.text($("proxy-test-state"),status?labels[status]??labels.error:"");
  elapsed.textContent=showElapsed?`${elapsedMs.toLocaleString()} ms`:"";
  elapsed.hidden=!showElapsed;
}
async function testProxySettings() {
  const button=$("proxy-test");
  if(settingsSaving||button.getAttribute("aria-busy")==="true")return;
  if(!$("proxy-address").reportValidity())return;
  const draft=proxySettingsDraft();
  button.setAttribute("aria-busy","true");
  setProxyTestStatus();
  fillProxySettings();
  let status,elapsedMs;
  try {
    const result=await api("/api/proxy-test",JSON.parse(draft),AbortSignal.timeout(15000));
    status=result.state??"error";
    elapsedMs=result.elapsed_ms;
  } catch(error) {
    status=error?.name==="TimeoutError"?"timeout":"error";
  } finally {
    button.setAttribute("aria-busy","false");
    fillProxySettings();
  }
  if($("settings-dialog").open)setProxyTestStatus(draft===proxySettingsDraft()?status:"stale",elapsedMs);
}
function imageAnalysisMode(status=state.status) {
  return ["native","disabled","optimized"].includes(status?.image_analysis_mode)?status.image_analysis_mode:
    status?.image_isolation_enabled?"optimized":"native";
}
function selectedImageAnalysisMode() {
  return $("image-analysis-mode").checked?"optimized":"native";
}
function imageAnalysisModeSupported(mode,status=state.status) {
  const supported=status?.image_analysis_modes;
  return Array.isArray(supported)?supported.includes(mode):
    !!status?.image_isolation_supported||["native","disabled","optimized"].includes(status?.image_analysis_mode);
}
function legacyImageAnalysisPending() {
  return state.status?.image_analysis_mode==="disabled"&&imageAnalysisModeSupported("native");
}
function fillImageAnalysisMode() {
  const input=$("image-analysis-mode"),nativeSupported=imageAnalysisModeSupported("native");
  const canSwitch=nativeSupported&&imageAnalysisModeSupported("optimized");
  if(!settingsEditing)input.checked=imageAnalysisMode()==="optimized";
  input.disabled=settingsSaving||!canSwitch;
  let hint=input.checked
    ?"图片由独立会话解析，主请求较小。"
    :"图片由主会话解析，主请求较大。";
  if(!input.checked&&legacyImageAnalysisPending())hint="保存后恢复原生图片解析。";
  else if(!canSwitch)hint=nativeSupported?"当前中转仅支持原生图片解析。":"当前中转暂不支持此功能。";
  uiI18n.text($("image-isolation-hint"),hint);
}
function fillZstdLevel() {
  const s=state.status,input=$("zstd-level"),supported=Number.isInteger(s.zstd_level);
  $("auto-zstd-level-hint").textContent=Number.isInteger(s.auto_image_zstd_level)?t("对话按大小选档，图片请求使用{level}级；开启后禁用手动选择。").replace("{level}",s.auto_image_zstd_level):t("根据请求大小自动选择合适的压缩等级，开启后禁用手动选择。");
  const selected=settingsEditing?Number(input.value):(s.zstd_level??3);
  const automatic=$("packet-size-protection").checked&&$("auto-zstd-level").checked;
  $("manual-compression-levels").disabled=automatic;
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
  const connected=s.profile?.active===true;
  const unknown=typeof s.profile?.active!=="boolean"||!!s.profile?.config_error;
  const busy=compressionSaving||settingsSaving||exitSaving||connectSaving||connectChecking||!!linkSaving||connectionResumeBusy();
  for(const button of document.querySelectorAll("#request-compression button")) {
    const disconnect=button.dataset.compression==="disconnected";
    button.setAttribute("aria-pressed",String(!unknown&&(disconnect?!connected:connected)));
    button.disabled=busy||unknown||(disconnect?!connected:connected);
  }
  $("request-compression").setAttribute("aria-busy",String(busy));
  setText($("compression-hint"),t(connectionResumeBusy()?"正在检查接入配置…":unknown?"本机接入状态暂不可用。":
    connected?"由本软件管理 zstd 压缩，保留完整内容。":"直连原上游，由 Codex 管理原生压缩。"));
  $("compression-confirm-dialog").setAttribute("aria-busy",String(compressionSaving));
  for(const id of ["compression-confirm-cancel","compression-confirm-apply"])$(id).disabled=compressionSaving;
  setText($("compression-confirm-apply"),t(compressionTarget==="disconnected"?(compressionSaving?"正在取消接入…":"确认断开"):(compressionSaving?"正在切换…":"确认断开")));
  if(!connected&&!compressionSaving&&$("compression-confirm-dialog").open)cancelCompressionChange();
}
function showConnectionFeedback(which,result={}) {
  restartNotices.connection(which,result);
}
function requestCompressionChange(mode) {
  if(mode==="connected")return requestConnection("header");
  const s=state.status,dialog=$("compression-confirm-dialog");
  if(!s?.profile?.active||compressionSaving||settingsSaving||settingsOpening||settingsEditing||linkSaving||exitSaving||
    connectSaving||connectChecking||connectionResumeBusy()||document.querySelector("dialog[open]"))return;
  const disconnect=mode==="disconnected";
  if(!disconnect||s.profile.config_error)return;
  compressionTarget=mode;
  $("compression-confirm-title").textContent=t("取消接入");
  $("compression-confirm-description").textContent=t("将恢复本机及已接入 WSL 的原始出口，并开启 Codex 原生压缩。");
  $("compression-confirm-hint").textContent=t("重启 ChatGPT App 生效。");
  renderCompressionMode();
  $("compression-confirm-message").hidden=true;
  globalThis.InspectorConnectionControls?.hide();
  dialog.showModal();
}
function cancelCompressionChange() {
  if(compressionSaving)return;
  $("compression-confirm-dialog").close();compressionTarget=null;
}
async function confirmCompressionChange() {
  const s=state.status,mode=compressionTarget,disconnect=mode==="disconnected";
  if(!s||!s.profile?.active||compressionSaving||settingsSaving||linkSaving)return;
  if(!disconnect||s.profile.config_error)return;
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
function connectionResumeBusy() {
  return ["pending","checking"].includes(state.status?.connection_resume?.state);
}
function rememberConnectionResume() {
  const event=state.status?.connection_resume;
  if(event?.id&&!connectionResumeBusy()) {
    connectionResumeSeen=event.id;
    try {localStorage.setItem("connection-resume-seen",event.id);}catch {}
  }
}
function renderConnectionResume() {
  const event=state.status?.connection_resume;
  if(!connectionResumeSeen)try {connectionResumeSeen=localStorage.getItem("connection-resume-seen")||"";}catch {}
  if(!event?.id||!["complete","attention"].includes(event.state))return;
  if(event.id===connectionResumeSeen)return;
  const result=event.result||{};
  if(result.changed||event.state==="attention"||result.restart_notice?.items?.length) {
    const wslChanged=result.wsl?.targets?.some(row=>row.changed);
    restartNotices.connection("activate",{...result,
      local_reload_required:result.local_reload_required??(!!result.changed&&!wslChanged)});
  }
  rememberConnectionResume();
}
function renderConnectPrompt() {
  const s=state.status;if(!s)return;
  const dialog=$("connect-confirm-dialog");
  if(s.ui_desktop&&s.ui_window_opened!==desktopOpenRevision) {
    desktopOpenRevision=s.ui_window_opened;
    exitFinished=false;
  }
  dialog.setAttribute("aria-busy",String(connectSaving));
  for(const key of ["cancel","apply"])$("connect-confirm-"+key).disabled=connectSaving;
  setText($("connect-confirm-apply"),t(connectSaving?"正在接入…":"确认接入"));
  if(dialog.open&&!connectSaving&&(s.profile?.active||connectSource==="startup"&&!firstRunContextCurrent()))cancelConnectPrompt();
}
function firstRunContextCurrent() {
  const s=state.status;
  return s.ui_window_opened===firstRunWindow&&!s.ui_exit_request&&!s.ui_restart_request&&!exitFinished;
}
async function offerFirstConnection() {
  const s=state.status;
  if(!s.ui_first_run_pending||firstRunWindow!==null||s.ui_exit_request||s.ui_restart_request||exitFinished||
    connectChecking||connectSaving||linkSaving||settingsOpening||settingsEditing||settingsSaving||compressionSaving||exitSaving||
    connectionResumeBusy()||document.querySelector("dialog[open]"))return;
  firstRunWindow=s.ui_window_opened;
  firstRunChecking=true;
  let result;
  try {result=await api("/api/first-run",{});}
  finally {firstRunChecking=false;}
  if(result.show&&firstRunContextCurrent())await requestConnection("startup");
}
async function requestConnection(source="header") {
  const s=state.status,dialog=$("connect-confirm-dialog");
  if(s?.profile?.active!==false||s.profile.config_error||firstRunChecking||connectSaving||connectChecking||linkSaving||settingsSaving||
    settingsOpening||compressionSaving||exitSaving||connectionResumeBusy()||document.querySelector("dialog[open]"))return;
  if(settingsEditing)return;
  connectChecking=true;renderConnectionActions();
  try {await refreshStatus();}
  finally {connectChecking=false;renderConnectionActions();}
  const current=state.status;
  if(current.profile?.active!==false||current.profile.config_error||current.ui_exit_request||current.ui_restart_request||exitSaving||exitFinished||
    (source==="startup"&&!firstRunContextCurrent())||document.querySelector("dialog[open]"))return;
  connectSource=source;
  $("connect-confirm-cancel").textContent=t(source==="startup"?"暂不接入":"取消");
  $("connect-confirm-hint").textContent=(current.loopback_proxy_repair_supported?t("接入时自动检查并补齐本机代理例外。"):"")+t("接入后关闭原生压缩，由本软件管理 zstd。配置变更后需重启 ChatGPT App。");
  $("connect-confirm-message").hidden=true;renderConnectPrompt();
  globalThis.InspectorConnectionControls?.hide();
  dialog.showModal();
}
function cancelConnectPrompt() {
  if(connectSaving)return;
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
  if(!s||exitFinished||serviceRestartSaving||$("service-restart-dialog").open)return;
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
function renderServiceRestart(request=serviceRestartRequest) {
  const dialog=$("service-restart-dialog");
  if(!request) {
    if(!serviceRestartSaving){serviceRestartRequest=null;if(dialog.open)dialog.close();}
    return;
  }
  if(settingsSaving||settingsOpening||compressionSaving||linkSaving||connectSaving||exitSaving||$("exit-confirm-dialog").open)return;
  if($("connect-confirm-dialog").open)cancelConnectPrompt();
  const fresh=serviceRestartRequest?.id!==request.id;
  serviceRestartRequest=request;
  dialog.setAttribute("aria-busy",String(serviceRestartSaving));
  $("service-restart-cancel").disabled=serviceRestartSaving;
  $("service-restart-apply").disabled=serviceRestartSaving||!!request.error;
  uiI18n.text($("service-restart-apply"),serviceRestartSaving&&serviceRestartAction==="restart"?"正在重启…":"重启服务");
  if(request.error){uiI18n.text($("service-restart-message"),request.error);$("service-restart-message").hidden=false;}
  else if(fresh)$("service-restart-message").hidden=true;
  if(!dialog.open)dialog.showModal();
}
async function confirmServiceRestart(action) {
  if(!serviceRestartRequest||serviceRestartSaving||action==="restart"&&serviceRestartRequest.error)return;
  serviceRestartSaving=true;serviceRestartAction=action;serviceRestartRevision++;renderServiceRestart();
  $("service-restart-message").hidden=true;
  try {
    const result=await api("/api/service-restart",{request_id:serviceRestartRequest.id,action});
    if(action==="cancel"?!result.cancelled:!result.restarting)throw Error("未能确认服务重启，请稍后重试。");
    if(state.status)state.status.ui_restart_request=null;
    serviceRestartRequest=null;$("service-restart-dialog").close();
  } catch(error) {
    uiI18n.text($("service-restart-message"),error.message);$("service-restart-message").hidden=false;
  } finally {serviceRestartSaving=false;serviceRestartAction="";serviceRestartRevision++;renderServiceRestart();}
}
function fillStorageDirectories() {
  const s=state.status,supported=!!s.storage_dir_supported,input=$("storage-dir");
  if(!settingsEditing)input.value=s.storage_dir||s.data_dir||"";
  input.placeholder=s.data_dir||"";
  input.disabled=settingsSaving||!supported;
  $("storage-paths-hint").hidden=!s.storage_paths_pending;
  $("storage-paths-hint").textContent=s.storage_paths_pending?t("目录已保存，重启本软件后生效，原目录数据保留。"):"";
  fillDirectoryOpenButtons();
}
function renderLinuxStorageTargets(remote) {
  const status=state.status;
  const wsl=status.wsl_connection||{},targets=new Map();
  for(const row of remote?.targets||[]) {
    if(row.kind==="ssh")targets.set(row.id,{name:row.name,storage:row.image_storage});
  }
  for(const row of [...(wsl.deferred||[]),...(wsl.detected||[]),...(wsl.targets||[])]) {
    if(row.id)targets.set(row.id,{name:"WSL · "+row.distribution,storage:row.image_storage});
  }
  const list=$("linux-storage-targets"),rows=[];
  list.hidden=!status.linux_data_directories||!targets.size;
  for(const [identity,{name,storage}] of targets) {
    const input=el("input"),label=el("label","",name),row=el("div","storage-directory-row");
    input.id="linux-data-dir-"+rows.length;input.name=input.id;label.htmlFor=input.id;
    label.title=name;
    input.type="text";input.maxLength=1024;input.spellcheck=false;input.autocomplete="off";
    input.setAttribute("autocapitalize","none");input.setAttribute("autocorrect","off");
    input.dataset.linuxStorageTarget=identity;
    input.placeholder=storage?.data_dir===""&&storage.directory?.endsWith("/images")
      ? storage.directory.slice(0,-"/images".length)||"/"
      : "~/.cache/codex-request-optimizer";
    input.value=status.linux_data_directories?.[identity]||"";
    input.addEventListener("input",()=>input.setCustomValidity(""));
    row.append(label,input);
    if(storage?.error) {
      const message=el("p","help error",t(storage.error));message.id=input.id+"-message";
      row.append(message);input.setAttribute("aria-describedby",message.id);
    }
    rows.push(row);
  }
  list.replaceChildren(...rows);
}
function linuxStorageDraft() {
  const values=new Map(Object.entries(state.status.linux_data_directories||{}));
  for(const input of $("linux-storage-targets").querySelectorAll("input")) {
    const value=input.value.trim(),key=input.dataset.linuxStorageTarget;
    if(value)values.set(key,value);
    else values.delete(key);
  }
  return JSON.stringify(Object.fromEntries([...values].sort(([a],[b])=>a.localeCompare(b))));
}
function fillDirectoryOpenButtons() {
  $("storage-dir-open").disabled=settingsSaving||!state.status.open_directory_supported||directoryOpening.has("storage-dir");
}
async function openLogDirectory() {
  const s=state.status;
  await api("/api/open-directory",{path:s?.active_log_dir||s?.log_dir||s?.data_dir||""});
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
  else if(port.pending){label="先重启优化器，再重启 ChatGPT App";kind="pending";}
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
function syncContextControls() {
  const unavailable=settingsSaving||!contextSettings;
  for(const key of ["context","compact"]) {
    const toggle=$(key+"-custom");
    toggle.disabled=unavailable;
    $(key+"-tokens").disabled=unavailable||!toggle.checked;
  }
  renderContextPlaceholders();
}
function fillContextSettings(data) {
  contextSettings=data;
  const maximum=data.max_context_tokens/1000;
  for(const [key,value] of [["context",data.context_tokens],["compact",data.compact_tokens]]) {
    const input=$(key+"-tokens");
    input.max=String(maximum);
    if(!settingsEditing) {
      $(key+"-custom").checked=value!=null;
      input.value=value==null?"":String(value/1000);
      if(value!=null)contextCustomValues.set(key,input.value);
    }
  }
  syncContextControls();
}
function fillCapacityRetry() {
  const s=state.status,enabled=$("capacity-retry"),count=$("capacity-retry-count"),delay=$("capacity-retry-delay");
  if(!settingsEditing) {
    enabled.checked=!!s.capacity_retry_enabled;
    count.value=s.capacity_retry_max_retries??15;
    delay.value=s.capacity_retry_delay_seconds??30;
  }
  enabled.disabled=settingsSaving||!s.capacity_retry_supported;
  const editable=!enabled.disabled&&enabled.checked&&s.capacity_retry_mode==="native";
  count.disabled=!editable||!Number.isInteger(s.capacity_retry_max_retries);
  delay.disabled=!editable||!Number.isInteger(s.capacity_retry_delay_seconds);
}
$("capacity-retry").addEventListener("change",fillCapacityRetry);
function fillBadRequestRetry() {
  const s=state.status,enabled=$("bad-request-retry"),count=$("bad-request-retry-count");
  if(!settingsEditing){enabled.checked=!!s.bad_request_retry_enabled;count.value=s.bad_request_retry_max_retries??3;}
  enabled.disabled=settingsSaving||!s.bad_request_retry_supported;
  count.disabled=enabled.disabled||!enabled.checked;
}
$("bad-request-retry").addEventListener("change",fillBadRequestRetry);
function renderContextPlaceholders() {
  for(const key of ["context","compact"])$(key+"-tokens").placeholder=$(key+"-custom").checked?"":t("默认");
}
async function loadContextSettings() {
  contextMessage("");
  try {fillContextSettings(await api("/api/context-settings"));}
  catch(error) {contextSettings=null;syncContextControls();contextMessage(()=>t("上下文配置读取失败：")+t(error.message),true);}
}
for(const key of ["context","compact"]) {
  const input=$(key+"-tokens"),toggle=$(key+"-custom");
  input.addEventListener("input",()=>contextMessage(""));
  toggle.addEventListener("change",()=>{
    if(toggle.checked)input.value=contextCustomValues.get(key)||"";
    else {
      if(input.value&&input.validity.valid)contextCustomValues.set(key,input.value);
      input.value="";
    }
    syncContextControls();contextMessage("");
  });
}
let updateStatus=null, updateBusy=false, updateAction="";
let updateRevision=0,updateStatusLoading=false;
function readSettingsDraft() {
  const draft={
    entry_port:$("entry-port").value,
    proxy_settings:state.status.proxy_settings_supported?proxySettingsDraft():"",
    sharing_port:$("sharing-port").value,
    egress_port:$("exit-port").value,
    zstd_level:$("zstd-level").value,
    image_zstd_level:$("image-zstd-level").value,
    packet_size_protection_enabled:$("packet-size-protection").checked,
    auto_zstd_level_enabled:$("packet-size-protection").checked&&$("auto-zstd-level").checked,
    budget:$("budget").value,
    start_on_login:$("start-on-login").checked,
    sharing_tab_enabled:$("sharing-tab-enabled").checked,
    capacity_retry_enabled:$("capacity-retry").checked,
    capacity_retry_max_retries:$("capacity-retry-count").value,
    capacity_retry_delay_seconds:$("capacity-retry-delay").value,
    bad_request_retry_enabled:$("bad-request-retry").checked,
    bad_request_retry_max_retries:$("bad-request-retry").checked?$("bad-request-retry-count").value:String(state.status.bad_request_retry_max_retries??3),
    image_analysis_mode:selectedImageAnalysisMode(),
    storage_dir:$("storage-dir").value,
    linux_data_directories:state.status.linux_data_directories?linuxStorageDraft():"{}",
    max_storage_mb:$("storage-limit").value,
    max_packets:$("packet-limit").value,
    language:settingsLanguage??uiI18n.language
  };
  if(settingsBaseline&&!draft.packet_size_protection_enabled)draft.budget=settingsBaseline.budget;
  if(settingsBaseline&&!draft.capacity_retry_enabled) {
    draft.capacity_retry_max_retries=settingsBaseline.capacity_retry_max_retries;
    draft.capacity_retry_delay_seconds=settingsBaseline.capacity_retry_delay_seconds;
  }
  for(const key of ["context","compact"])draft[key+"_tokens"]=$(key+"-custom").checked?$(key+"-tokens").value:null;
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
  const log=$("settings-save-log"),icons={running:"…",done:"✓",error:"×"};
  log.replaceChildren(...settingsSaveProgress.steps.map(step=>{
    const item=el("li");item.dataset.state=step.state;
    const icon=el("span","settings-step-icon",icons[step.state]);icon.setAttribute("aria-hidden","true");
    item.append(icon,el("span","settings-step-label",t(step.label)));
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
  const canSave=dirty||(settingsEditing&&legacyImageAnalysisPending());
  $("settings-save").disabled=settingsSaving||!!linkSaving||!canSave;
  $("settings-restore").disabled=settingsSaving||!dirty;$("settings-close").disabled=settingsSaving;
  if(clearMessage&&settingsEditing&&!settingsSaving)setSettingsMessage(dirty?"有未保存的修改":"");
  if(state.status)renderEntryPortStatus();
}
function trackSettingsRestart(field,before,after) {
  restartNotices.change(field,before,after);
}
function finishSettingsSave() {
  resetSettingsSaveProgress();
  setSettingsMessage("设置已保存。");
  restartNotices.ingest();
}
async function saveSettings() {
  if(!settingsEditing||settingsSaving||linkSaving||!settingsBaseline)return;
  const draft=readSettingsDraft(),changed=new Set(changedSettings(draft)),s=state.status;
  if(legacyImageAnalysisPending())changed.add("image_analysis_mode");
  if(!changed.size)return;
  for(const input of $("settings-dialog").querySelectorAll(".settings-content input:not(:disabled)")) {
    const target=input.dataset.linuxStorageTarget;
    if(target&&changed.has("linux_data_directories")) {
      const value=input.value.trim(),previous=s.linux_data_directories?.[target];
      const valid=!value||value===previous||(value.startsWith("/")&&!/[\u0000-\u001f\\]/.test(value));
      input.setCustomValidity(valid?"":"数据目录须为 Linux 绝对路径。");
    }
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
    ["sharing_tab_enabled","sharing_tab_enabled",draft.sharing_tab_enabled,typeof s.sharing_tab_enabled==="boolean"],
    ["capacity_retry_enabled","capacity_retry_enabled",draft.capacity_retry_enabled,s.capacity_retry_supported],
    ["capacity_retry_max_retries","capacity_retry_max_retries",Number(draft.capacity_retry_max_retries),Number.isInteger(s.capacity_retry_max_retries)],
    ["capacity_retry_delay_seconds","capacity_retry_delay_seconds",Number(draft.capacity_retry_delay_seconds),Number.isInteger(s.capacity_retry_delay_seconds)],
    ["bad_request_retry_enabled","bad_request_retry_enabled",draft.bad_request_retry_enabled,s.bad_request_retry_supported],
    ["bad_request_retry_max_retries","bad_request_retry_max_retries",Number(draft.bad_request_retry_max_retries),s.bad_request_retry_supported],
    ["image_analysis_mode","image_analysis_mode" in s?"image_analysis_mode":"image_isolation_enabled",
      "image_analysis_mode" in s?draft.image_analysis_mode:draft.image_analysis_mode==="optimized",
      imageAnalysisModeSupported(draft.image_analysis_mode,s)],
    ["storage_dir","storage_dir",draft.storage_dir===s.data_dir?"":draft.storage_dir,s.storage_dir_supported],
    ["linux_data_directories","linux_data_directories",JSON.parse(draft.linux_data_directories),!!s.linux_data_directories],
    ["max_storage_mb","max_storage_bytes",storage.toBackend(draft.max_storage_mb),true],
    ["max_packets","max_packets",Number(draft.max_packets),true]
  ].filter(([field,,,supported])=>supported&&changed.has(field));
  const contextChanged=contextSettings&&["context_tokens","compact_tokens"].some(key=>changed.has(key));
  const portChanged=s.entry_port_supported&&s.entry_port&&changed.has("entry_port");
  const proxyChanged=s.proxy_settings_supported&&changed.has("proxy_settings");
  const controls=new Map([...$("settings-dialog").querySelectorAll("input,select,button")].map(node=>[node,node.disabled]));
  let saved=0,error=null;
  settingsSaveProgress={total:Number(!!entries.length)+Number(!!contextChanged)+Number(!!portChanged)+Number(!!proxyChanged)+Number(changed.has("language")),steps:[]};
  settingsSaving=true;settingsRevision++;
  for(const node of controls.keys())node.disabled=true;
  $("settings-dialog").setAttribute("aria-busy","true");renderSettingsSaveProgress();
  try {
    if(proxyChanged) {
      await runSettingsSaveStep("出口代理",async()=>{
        state.status.proxy_settings=await api("/api/proxy-settings",JSON.parse(draft.proxy_settings));
        fillProxySettings(true);
        settingsBaseline.proxy_settings=proxySettingsDraft();saved++;
      });
    }
    if(entries.length) {
      const label=entries.some(([field])=>["storage_dir","linux_data_directories"].includes(field))?"存储位置":
        entries.some(([field])=>field==="image_analysis_mode")?"图片与运行设置":
        entries.some(([field])=>field==="start_on_login")?"开机启动与运行设置":"运行设置";
      await runSettingsSaveStep(label,async()=>{
        const values=Object.fromEntries(entries.map(([,key,value])=>[key,value]));
        if(!s.retention_policy&&Number.isInteger(s.max_detail_packets)&&entries.some(([field])=>["max_packets","max_storage_mb"].includes(field)))
          values.max_detail_packets=Number(draft.max_packets);
        const result=await api("/api/settings",values);
        state.status={...state.status,...result};
        restartNotices.ingest();
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
        if(contextSettings.restart_notice)restartNotices.adopt(contextSettings.restart_notice);
        if(contextSettings.changed||contextSettings.reload_required)
          for(const field of ["context_tokens","compact_tokens"])trackSettingsRestart(field,previous[field],contextSettings[field]);
        settingsBaseline.context_tokens=draft.context_tokens;settingsBaseline.compact_tokens=draft.compact_tokens;
        saved++;
      });
    }
    if(portChanged) {
      await runSettingsSaveStep("入口端口",async()=>{
        const previous=state.status.entry_port.configured_port;
        state.status.entry_port=await api("/api/entry-port",{port:Number(draft.entry_port)});
        if(state.status.entry_port.restart_notice)restartNotices.adopt(state.status.entry_port.restart_notice);
        else trackSettingsRestart("entry_port",previous,state.status.entry_port.configured_port);
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
    if(changedSettings().length||legacyImageAnalysisPending())throw Error("有未保存的修改");
  } catch(failure) {error=failure;}
  finally {
    settingsSaving=false;settingsRevision++;
    for(const [node,disabled] of controls)if(node.isConnected)node.disabled=disabled;
    $("settings-dialog").setAttribute("aria-busy","false");
    renderSettingsSaveProgress();
    fillSettings();renderUpdates();renderStatus();updateSettingsSaveState(false);
    if(error)setSettingsMessage(()=>t(saved?"部分设置已保存，剩余修改未保存：":"保存失败：")+t(error.message),true);
    else finishSettingsSave();
  }
}
function renderUpdates() {
  const u=updateStatus;
  const supported=u?.supported!==false&&state.status?.updates_supported!==false;
  const currentVersion=u?.current_version||state.status?.version;
  setText($("updates-versions"),currentVersion||"");
  const displayedState=updateBusy?(updateAction==="check"?"checking":"downloading"):u?.state;
  const checking=displayedState==="checking",installing=["downloading","upgrading"].includes(displayedState);
  const busy=checking||installing,available=displayedState==="available";
  const showLatest=u?.latest_version&&(available||checking&&!updateBusy&&u.periodic_check);
  const checkButton=$("updates-check"),installButton=$("updates-install");
  const disabled=settingsSaving||busy||!supported;
  checkButton.disabled=disabled;
  checkButton.setAttribute("aria-busy",String(supported&&checking));
  installButton.hidden=!supported||!(available||installing);
  installButton.disabled=disabled;
  installButton.setAttribute("aria-busy",String(supported&&installing));
  const labels={idle:"可检查 GitHub 上的最新版本。",checking:"正在检查更新…",current:"已是最新版本。",available:"发现新版本，可以升级。",downloading:"正在下载并校验升级包…",upgrading:"正在升级，旧请求继续完成…",complete:"升级已完成。"};
  const message=$("updates-message");
  message.hidden=supported&&(!displayedState||displayedState==="idle");
  setText(message,!supported?t("Linux 版请通过替换安装包更新。"):displayedState==="error"?t(u.error):showLatest?t("最新版本：{version}").replace("{version}",u.latest_version):t(labels[displayedState]||labels.idle));
  message.title=message.textContent;
  message.classList.toggle("error",supported&&displayedState==="error");
}
async function loadUpdates() {
  if(updateStatusLoading||updateBusy)return;
  const revision=updateRevision;updateStatusLoading=true;
  try {const result=await api("/api/updates");if(revision===updateRevision&&!updateBusy)updateStatus=result;}
  catch(error){
    if(revision===updateRevision&&!updateBusy&&["downloading","upgrading"].includes(updateStatus?.state))
      updateStatus={...updateStatus,state:"error",error:error.message};
  }
  finally {updateStatusLoading=false;renderUpdates();}
}
async function performUpdate(which) {
  if(updateStatus?.supported===false||state.status?.updates_supported===false)return;
  if(settingsSaving||updateBusy||["checking","downloading","upgrading"].includes(updateStatus?.state))return;
  if(which==="install"&&updateStatus?.state!=="available")return;
  updateRevision++;updateBusy=true;updateAction=which;renderUpdates();
  try {
    updateStatus=await api("/api/updates",{action:which});
    // Older services can acknowledge an install before its first state write.
    if(which==="install"&&updateStatus.state==="available")updateStatus={...updateStatus,state:"downloading"};
  }
  catch(error){if(which==="install")updateStatus={...updateStatus,state:"error",error:error.message};}
  finally {updateBusy=false;updateAction="";renderUpdates();}
}
async function openSettings() {
  if(settingsOpening||compressionSaving||exitSaving||connectSaving||connectChecking||firstRunChecking||$("connect-confirm-dialog").open||$("exit-confirm-dialog").open||$("compression-confirm-dialog").open||$("settings-dialog").open)return;
  settingsOpening=true;
  try {
    await Promise.all([refreshStatus(true),loadContextSettings(),loadUpdates()]);
    if(state.status.linux_data_directories)renderLinuxStorageTargets(await api("/api/connections").catch(()=>null));
    else $("linux-storage-targets").hidden=true;
    settingsLanguage=uiI18n.language;
    settingsBaseline=readSettingsDraft();settingsEditing=true;
    resetSettingsSaveProgress();
    renderLanguageSelection();renderConnectionActions();renderUpdates();updateSettingsSaveState();
    $("settings-dialog").showModal();restartNotices.render();
  } finally {settingsOpening=false;}
}
function packetDetailVersion(p) {
  return JSON.stringify([p.state,p.status,p.duration_ms,p.outbound_wire_bytes,p.outbound_content_encoding,
    p.relay_content_encoding,p.relay_zstd_level,p.transport_content_encoding,p.transport_zstd_level,
    p.transport_input_bytes,p.transport_wire_bytes,p.inline_image_bytes,p.image_isolation_decoded_before,p.image_isolation_decoded_after,
    p.usage_override_request_bytes,p.usage_override_applied,p.capacity_retry_count,p.bad_request_retry_count,p.native_retry_advised,
    p.native_retry_delay_ms,p.capacity_retry_diagnostic?.updated,p.details_evicted,
    p.upstream_usage,p.reported_usage,p.overflow_compaction_calls,p.overflow_cache_hit,p.overflow_original_wire_bytes,
    p.relay_wire_bytes,p.image_isolation_applied,p.image_isolation_removed,p.image_paths,p.image_path_rewrites,
    p.image_upload_optimized,p.image_upload_before_bytes,p.image_upload_images]);
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
    breakdownComparison=null;expandedBreakdownRows.clear();
    requestBodyCache=null;requestBodyView=null;requestBodySequence++;clearJsonFocus();clearJsonActions();
    $("packet-detail").hidden=true;$("empty-state").hidden=false;renderList();return;
  }
  if(requestId!==state.detailRequest || (refresh && state.selected!==id) || (expectedListRequest!==null && expectedListRequest!==state.listRequest)) return;
  const changed=state.selected!==id, previousDetail=state.detail, detailsChanged=!!m.details_evicted!==!!previousDetail?.details_evicted;
  state.selected=id;state.detail=m;state.detailVersion=packetDetailVersion(m);
  if(changed||detailsChanged)breakdownComparison=null;
  if(changed)expandedBreakdownRows.clear();
  showTab(state.tab);
  $("empty-state").hidden=true; $("packet-detail").hidden=false; renderList();
  if(changed || tokenVersion(m)!==tokenVersion(previousDetail||{})) {cancelTokenRead();tokenWindow=cachedTokenWindow(m);}
  else if(tokenWindow?.state==="unavailable") tokenWindow=null;
  loadTokenWindow();
  renderOverview(m); renderHeaders(m);
  if(changed||detailsChanged) {
    state.path="";state.text=null;$("body-results").hidden=true;
    await loadTree("");
  }
}
function renderProtectionPanelState() {
  const panel=$("packet-protection-panel");if(!panel)return;
  const enabled={size:state.status?.packet_size_protection_enabled===true,retry:state.status?.capacity_retry_enabled===true||state.status?.bad_request_retry_enabled===true,
    optimization:state.status?.image_analysis_mode?state.status.image_analysis_mode==="optimized":state.status?.image_isolation_enabled===true};
  for(const row of panel.querySelectorAll("[data-protection]")) {
    const inactive=!enabled[row.dataset.protection];
    row.classList.toggle("protection-inactive",inactive);
    const hint={retry:"可在设置中开启自动重试",optimization:"可在设置中开启解图优化"};
    row.title=inactive?t(hint[row.dataset.protection]||"可在设置中开启请求大小保护"):"";
  }
}
function requestBasicsPanel(m) {
  const section=panel(t("基本信息"),{headingTag:"h2"}),facts=el("dl","request-overview-facts");
  fact(facts,t("模型"),m.model||t("未标记"));
  const endings={completed:"完成",failed:"失败",incomplete:"未完成",cancelled:"已取消",interrupted:"已中断"};
  const ended=m.response_summary?.status,code=Number.isInteger(m.status)&&m.status>=100&&m.status<=599?m.status:null;
  const relayFailed=["relay_error","client_disconnected","interrupted"].includes(m.state);
  const result=relayFailed?states[m.state]:code>=400?t("失败"):Object.hasOwn(endings,ended)?t(endings[ended]):m.state==="complete"?t("已结束"):states[m.state]||t("尚未收到");
  const resultRow=fact(facts,t("结果"),result+(code!==null?" · HTTP "+code:""));
  if(relayFailed||code>=400||ended==="failed")resultRow.lastChild.classList.add("danger");
  else if(ended==="completed"||!Object.hasOwn(endings,ended)&&m.state==="complete")resultRow.lastChild.classList.add("request-result-success");
  fact(facts,t("总耗时"),Number.isFinite(m.duration_ms)&&m.duration_ms>=0?nf.format(Math.round(m.duration_ms/100)/10)+" "+t("秒"):"—");
  const errorReason=m.network_error?.message||m.relay_error;
  if(typeof errorReason==="string"&&errorReason)fact(facts,t("错误原因"),t(errorReason)).classList.add("request-error-detail");
  if(m.source!=="live")fact(facts,t("数据来源"),t("历史重建（大小为估算）"));
  if(m.details_evicted)fact(facts,t("请求详情"),t("已回收"));
  const session=sessionSummaries.find(row=>row.thread_id===m.thread_id);
  const specialKind=[purposeFromMetadata(m),auxiliarySessionKind(session)].find(kind=>kind&&kind!=="subagent");
  if(specialKind) {
    const name=["index_task","subagent","input"].includes(session?.title_source)?null:sessionTitle(session);
    if(name)fact(facts,t("独立会话名称"),name);
    fact(facts,t("独立会话 ID"),m.thread_id||"—");
    if(specialKind==="image_analysis") {
      const body=requestBodyView&&requestBodyView.id===m.id?requestBodyView.body:null;
      const paths=requestImagePaths(m,body);
      const value=fact(facts,t("图片路径"),paths.length?"":"—").lastChild;
      if(paths.length) {
        value.classList.add("image-path-value");
        value.append(renderImagePaths(paths,{remote:!!(m.ssh_connection_key||m.wsl_connection_key)}));
      }
    }
  }
  section.append(facts);
  return section;
}
function requestForwardingPanel(m) {
  const section=panel(t("转发处理"),{headingTag:"h2"}),facts=el("dl","request-overview-facts");
  const compressed=compressedBytes(m),summaryTriggered=overflowApplied(m);
  fact(facts,t("原始请求"),bytes(originalBytes(m))).title=t("原始请求大小（图片处理、传输压缩前）");
  fact(facts,t("转发请求"),bytes(compressed)).title=t("转发请求大小（图片处理、传输压缩后，超限摘要处理前）");
  const hasTransport=typeof m.transport_content_encoding==="string";
  // Older summary records only retain the final body's encoding and grade.
  // Those values cannot describe the pre-summary packet shown beside them.
  const encoding=hasTransport?m.transport_content_encoding:summaryTriggered?null:m.relay_content_encoding||m.outbound_content_encoding;
  const level=hasTransport?m.transport_zstd_level:summaryTriggered?null:m.relay_zstd_level;
  const method=typeof encoding==="string"?encoding.trim().toLowerCase():"";
  const isCompressed=!!method&&method!=="identity";
  let compression=isCompressed?method:"-";
  if(method==="zstd"&&Number.isInteger(level)&&level>=1&&level<=22) {
    compression=t("zstd · {level} 级").replace("{level}",level);
  }
  fact(facts,t("请求压缩"),compression);
  if(isCompressed) {
    // Older resized requests did not record the uncompressed body of the selected resize.
    const legacyInput=m.image_upload_optimized?null:m.image_isolation_decoded_after??(m.image_isolation_applied?null:originalBytes(m));
    const before=m.transport_input_bytes??legacyInput,after=m.transport_wire_bytes??compressed;
    if(Number.isSafeInteger(before)&&before>0&&Number.isSafeInteger(after)&&after>0) {
      fact(facts,t("压缩比例"),(100*after/before).toFixed(1)+"%");
    }
  }
  const imageSize=m.inline_image_bytes;
  if(Number.isSafeInteger(imageSize)&&imageSize>0) {
    fact(facts,t("图片大小"),bytes(imageSize));
  } else if(imageSize==null&&m.image_isolation_removed>0&&Number.isSafeInteger(m.image_isolation_decoded_before)&&
      Number.isSafeInteger(m.image_isolation_decoded_after)&&m.image_isolation_decoded_after>=0&&
      m.image_isolation_decoded_before>m.image_isolation_decoded_after) {
    // Older records only have the net change from replacing images with paths.
    fact(facts,t("图片大小"),"≈ "+bytes(m.image_isolation_decoded_before-m.image_isolation_decoded_after));
  }
  if(m.image_upload_optimized===true) {
    const images=Array.isArray(m.image_upload_images)?m.image_upload_images.filter(image=>image&&typeof image==="object"&&!Array.isArray(image)):[];
    const changes=[];
    for(const [index,image] of images.entries()) {
      if(![image.original_width,image.original_height,image.width,image.height].every(value=>Number.isSafeInteger(value)&&value>0))continue;
      const name=images.length>1||image.mask?(image.mask?t("编辑蒙版"):t("参考图 {index}").replace("{index}",index+1))+" · ":"";
      changes.push(`${name}${image.original_width} × ${image.original_height} → ${image.width} × ${image.height}`);
    }
    if(changes.length) {
      const row=fact(facts,t("图片压缩"),changes.join("\n"));
      row.lastChild.classList.add("image-compression-details");
    }
  }
  if(summaryTriggered) {
    const estimate=requestTokenUsage(m);
    const actual=m.upstream_usage?.input_tokens;
    fact(facts,t("输入 Token（估算）"),sortInputTokens(estimate)!==null?"≈ "+nf.format(estimate.input_tokens):"—").title=t("超限摘要前输入 Token（估算）");
    fact(facts,t("上游实际输入 Token"),Number.isSafeInteger(actual)&&actual>=0?nf.format(actual):"—");
  }
  section.append(facts);
  return section;
}
function requestProtectionPanel(m) {
  const flags=packetProtection(m);
  const title=t("请求保护"),steps=panel(title,{headingTag:"h2"});
  steps.id="packet-protection-panel";
  steps.setAttribute("aria-label",title);
  const item=(key,category)=>{
    const triggered=flags[key];
    const section=el("section","protection-item");section.dataset.renderKey="protection:"+key;
    section.dataset.protection=category;section.dataset.mechanism=key;section.dataset.triggered=String(triggered);
    const heading=fact(section,t(protectionLabels[key]),triggered?"✓":"—"),status=heading.lastChild;
    heading.classList.add("protection-heading");status.classList.add("protection-status");
    status.title=t(triggered?"已触发":"未触发");status.setAttribute("role","img");status.setAttribute("aria-label",status.title);steps.append(section);
    if(!triggered)return null;
    const details=el("div","protection-details");details.dataset.renderKey="protection-details";section.append(details);return details;
  };
  const number=value=>Number.isSafeInteger(value)&&value>=0?nf.format(value):"—";
  const size=value=>Number.isSafeInteger(value)&&value>=0?bytes(value):"—";
  const predictive=item("predictive","size");
  if(predictive) {
    fact(predictive,t("原始 Token"),number(m.upstream_usage?.input_tokens));
    fact(predictive,t("改写 Token"),number(m.reported_usage?.input_tokens));
  }
  const overflow=item("overflow","size");
  if(overflow) {
    fact(overflow,t("处理前大小"),size(m.overflow_original_wire_bytes));
    const after=[m.relay_wire_bytes,m.outbound_wire_bytes].find(value=>Number.isSafeInteger(value)&&value>=0);
    fact(overflow,t("处理后大小"),size(after));
  }
  const retryCount=autoRetryCount(m),retry=item("retry","retry");
  if(retry)fact(retry,t("重试次数"),retryCount>0?nf.format(retryCount):"—");
  const optimization=item("optimization","optimization");
  if(optimization) {
    const body=requestBodyView&&requestBodyView.id===m.id?requestBodyView.body:null,paths=requestImagePaths(m,body);
    if(paths.length)optimization.append(renderImagePaths(paths,{showDirectoryRow:false,remote:!!(m.ssh_connection_key||m.wsl_connection_key)}));
    else fact(optimization,t("图片路径"),"—");
  }
  return steps;
}
function bars(section, title, rows, total, bodyAvailable, detailsFor) {
  const p=panel(title), max=total||1;
  for(const [index,r] of rows.entries()) {
    const row=el("div","bar-row");
    row.dataset.renderKey="bar:"+r.key;
    const label=bodyAvailable&&r.path!==null?link(r.name,()=>goPath(r.path)):el("span","",r.name);
    label.classList.add("bar-label");
    label.title=r.name;
    const size=el("span","bar-value",bytes(r.bytes));
    const progress=el("progress");
    progress.max=max;
    progress.value=r.bytes??0;
    progress.setAttribute("aria-hidden","true");
    const percentage=el("span","bar-value",r.bytes==null?"—":(r.bytes/max*100).toFixed(1)+"%");
    const value=r.delta===0?"":signed(r.delta??null);
    const renderDetails=value?detailsFor?.(r):null;
    const tone=r.delta>0?" bar-delta-increase":r.delta<0?" bar-delta-decrease":"";
    const delta=el(renderDetails?"button":"span","bar-value bar-delta"+tone,value);
    if(value)delta.title=t("相对上条请求");
    row.append(label,size,delta,progress,percentage);
    if(renderDetails) {
      const key=section+":"+r.key, expanded=expandedBreakdownRows.has(key);
      const details=el("ul","bar-change-details");
      details.setAttribute("role","list");
      details.id=section+"-size-change-"+index;
      details.hidden=!expanded;
      delta.type="button";
      delta.setAttribute("aria-label",r.name+" "+t("变化值")+" "+value);
      delta.setAttribute("aria-expanded",String(expanded));
      delta.setAttribute("aria-controls",details.id);
      delta.onclick=()=>{
        if(expandedBreakdownRows.has(key))expandedBreakdownRows.delete(key);
        else expandedBreakdownRows.add(key);
        renderOverview(state.detail);
      };
      if(expanded)details.append(renderDetails());
      row.append(details);
    }
    p.append(row);
  }
  return p;
}
function inputGroupRows(m) {
  if(!m?.input_groups||typeof m.input_groups!=="object"||Array.isArray(m.input_groups))return null;
  const legacy=requestBodyView&&requestBodyView.id===m.id&&!m.details_evicted
    ?requestBodyView.reasoningEncryptedBytes:null;
  const encrypted=m.reasoning_encrypted_bytes??(m.encrypted_content_bytes===0?0:legacy);
  const groups=[];
  for(const [category,size] of Object.entries(m.input_groups)) {
    if(!Number.isSafeInteger(size)||size<0)continue;
    if(category==="reasoning"&&Number.isSafeInteger(encrypted)&&encrypted>=0&&encrypted<=size) {
      groups.push({key:"reasoning/encrypted",category,component:"encrypted",name:t("推理密文"),bytes:encrypted,path:"/input"},
                  {key:"reasoning/plain",category,component:"plain",name:t("推理非密文"),bytes:size-encrypted,path:"/input"});
    } else {
      groups.push({key:"kind/"+category,category,name:kind(category),bytes:size,path:"/input"});
    }
  }
  return groups;
}
function inputBreakdown(m, comparison) {
  const rows=inputGroupRows(m), previous=comparison?.previous, before=inputGroupRows(previous);
  if(!rows)return [];
  if(before) {
    const beforeByKey=new Map(before.map(r=>[r.key,r.bytes]));
    const currentCategories=new Set(rows.map(r=>r.category));
    for(const r of before) {
      if(!currentCategories.has(r.category))rows.push({...r,bytes:0,path:null});
    }
    for(const r of rows) {
      const existed=Object.hasOwn(previous.input_groups,r.category);
      const previousSize=!existed?0:!r.component?previous.input_groups[r.category]:beforeByKey.get(r.key);
      r.delta=Number.isSafeInteger(previousSize)?r.bytes-previousSize:null;
    }
  }
  const itemsByCategory=Map.groupBy(comparison?.items??[],item=>item.kind);
  for(const r of rows) {
    const items=itemsByCategory.get(r.category)??[];
    if(r.component) {
      r.items=items.map(item=>({...item,...(item.components?.[r.component]??{delta:null,change:null})}))
        .filter(item=>item.delta!==0);
    } else if(Number.isSafeInteger(r.delta))r.items=items;
  }
  return rows.sort((a,b)=>b.bytes-a.bytes);
}
function fieldBreakdown(m, comparison) {
  const current=Array.isArray(m.top_fields)?m.top_fields:[];
  const previous=comparison?.previous;
  const data=comparison?.data;
  const before=Array.isArray(previous?.top_fields)?new Map(previous.top_fields.map(r=>[r.name,r.bytes])):null;
  const compared=Array.isArray(data?.fields)&&data.fields_available!==false&&data.state!=="details_unavailable"&&(!previous||before);
  const changes=new Map((compared?data.fields:[]).map(r=>[r.name,r]));
  const rows=current.map(r=>({...r,key:"field/"+r.name,path:"/"+r.name.replaceAll("~","~0").replaceAll("/","~1"),
    delta:before?r.bytes-(before.get(r.name)??0):compared?r.bytes-(changes.get(r.name)?.before??r.bytes):null}));
  if(Array.isArray(m.top_fields)) {
    const present=new Set(current.map(r=>r.name));
    for(const [name,size] of before??[...changes.values()].map(r=>[r.name,r.before])) {
      if(!present.has(name)&&size>0)rows.push({key:"field/"+name,name,bytes:0,path:null,delta:-size});
    }
  }
  const currentEnvelope=m.json_envelope_bytes, previousEnvelope=previous?.json_envelope_bytes;
  rows.push({key:"envelope",name:t("字段名、标点及空白"),bytes:currentEnvelope,path:"",
    delta:Number.isSafeInteger(currentEnvelope)&&Number.isSafeInteger(previousEnvelope)?currentEnvelope-previousEnvelope:null});
  return rows.sort((a,b)=>(b.bytes??0)-(a.bytes??0));
}
function inputItemChanges(items, bodyAvailable) {
  const labels={
    added:t("新增"),removed:t("移除"),grow:t("增大"),shrink:t("缩小"),changed:t("内容改变")
  };
  const fragment=document.createDocumentFragment();
  for(const item of items) {
    const row=el("li","bar-row");
    const entry=el("div","breakdown-item-entry"), index="#"+item.index;
    entry.append(bodyAvailable&&item.path?link(index,()=>goPath(item.path)):el("span","",index));
    if(item.name)entry.append(el("span","",item.name));
    const tone=item.delta>0?" bar-delta-increase":item.delta<0?" bar-delta-decrease":"";
    const status=el("span","bar-value"+tone), change=el("span","bar-value bar-delta"+tone);
    if(item.change)status.append(el("span","sr-only",t("变化")+" "),labels[item.change]);
    if(item.delta!==0)change.append(el("span","sr-only",t("变化值")+" "),signed(item.delta));
    row.append(entry,status,change);
    fragment.append(row);
  }
  return fragment;
}
function inputChangeDetails(row, current, comparison, bodyAvailable) {
  if(row.component&&comparison?.reasoningLookup&&!comparison.reasoningLookup.loading)
    loadReasoningChangeSizes(current,comparison).catch(fail);
  if(row.items?.length)return inputItemChanges(row.items,bodyAvailable);
  const summaryOnly=current.details_evicted||["summary_only","details_unavailable"].includes(comparison?.data?.state);
  const message=summaryOnly?"请求详情已回收，仅保留基本信息。":"暂无可比较的条目。";
  return el("li","muted",t(message));
}
function renderOverview(m) {
  const root=$("overview");
  if(m.inspection!=="json") {reconcileContent(root,el("p","muted",t("正文未展开：")+(t(m.inspection_error)||t("正文为空"))+t("。转发仍按原始正文进行。")));return;}
  const breakdown=el("div","size-breakdown"), bodyAvailable=!m.details_evicted;
  const fields=fieldBreakdown(m,breakdownComparison), input=inputBreakdown(m,breakdownComparison);
  const inputSize=m.top_fields?.find(r=>r.name==="input")?.bytes;
  breakdown.append(
    bars("fields",t("字段构成"),fields,m.decoded_bytes,bodyAvailable),
    bars("input",t("input 构成"),input,inputSize,bodyAvailable,
      row=>row.component||row.items?.length?()=>inputChangeDetails(row,m,breakdownComparison,bodyAvailable):null)
  );
  reconcileContent(root,breakdown);
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
function requestTextMessages(body) {
  const input=body?.input??body?.messages;
  const items=typeof input==="string"?[{role:"user",content:input}]:input;
  if(!Array.isArray(items))return [];
  // Codex can send workspace context and image markers as standalone user text parts.
  const nativeUserContext=/^(?:# AGENTS\.md instructions[^\r\n]*\s+<INSTRUCTIONS>[\s\S]*<\/INSTRUCTIONS>|<environment_context>[\s\S]*<\/environment_context>|<image(?: name=[^<>\r\n]+)? path="[^"\r\n]+">(?:\s*<\/image>)?|<\/image>)$/u;
  const messages=[];
  for(const item of items) {
    const role=item?.role;
    if((role!=="user"&&role!=="assistant")||(item.type!=null&&item.type!=="message"))continue;
    const content=item.content,parts=Array.isArray(content)?content.map(part=>{
      if(role==="assistant"&&part?.type==="refusal")return part.refusal;
      if(["input_text","output_text","text"].includes(part?.type))return part.text;
    }):[content];
    const text=parts.filter(part=>{
      if(typeof part!=="string")return false;
      const trimmed=part.trim();
      return trimmed!==""&&(role!=="user"||!nativeUserContext.test(trimmed));
    }).join("\n");
    if(text) {
      const created=item.internal_chat_message_metadata_passthrough?.create_time;
      messages.push({role,text,created:Number.isFinite(created)&&created>0?created:null});
    }
  }
  return messages;
}
function scrollRequestTextOnFirstShow() {
  const view=requestBodyView;
  if(!view?.messages||view.id!==state.selected||view.textScrollInitialized)return;
  const root=$("request-body-text");
  // The body can finish loading while its tab is hidden.
  if(root.hidden||!root.clientHeight)return;
  root.scrollTop=root.scrollHeight;
  view.textScrollInitialized=true;
}
function setRequestBodyTextMode(enabled) {
  requestBodyTextMode=enabled;
  const view=requestBodyView,root=$("request-body-text"),available=view?.id===state.selected;
  const showText=enabled&&available;
  if(showText&&!view.messages) {
    view.messages=requestTextMessages(view.body).map(message=>({...message,author:t(message.role==="user"?"用户":"模型")}));
    const fragment=document.createDocumentFragment();
    for(const {role,author,text,created} of view.messages) {
      const message=el("div","request-message");
      message.setAttribute("data-role",role);
      const meta=el("div","request-message-meta");
      const date=created===null?null:new Date(created*1000),hasTime=date&&Number.isFinite(date.getTime());
      const time=hasTime
        ?el("time","request-message-time",dateFormat.format(date)+" "+clockFormat.format(date))
        :el("span","request-message-time","—");
      if(hasTime)time.setAttribute("datetime",date.toISOString());
      time.title=hasTime?timestampFormat.format(date):t("未记录消息时间");
      meta.append(el("strong","request-message-author",author),time);
      message.append(meta,el("p","request-message-text",text));
      fragment.append(message);
    }
    if(!fragment.firstChild)fragment.append(el("p","muted",t("没有可显示的对话文本。")));
    root.replaceChildren(fragment);
  }
  const jsonMode=$("request-body-json-mode"),chatMode=$("request-body-chat-mode");
  jsonMode.checked=!enabled;
  chatMode.checked=enabled;
  jsonMode.disabled=chatMode.disabled=!available;
  $("json-tree").hidden=showText;
  root.hidden=!showText;
  if(showText)scrollRequestTextOnFirstShow();
  if(available)setRequestBodyActions(view,showText);
}
function clearRequestBodyExport() {
  if(requestBodyExportUrl)URL.revokeObjectURL(requestBodyExportUrl);
  requestBodyExportUrl="";
  $("json-export").removeAttribute("href");
  const feedback=$("json-copy-feedback");delete feedback.copyRequest;clearCopyFeedback(feedback);
}
function setRequestBodyActions(view,showText) {
  const extension=showText?"txt":"json";
  if(view.output?.extension===extension)return;
  const text=showText
    ?view.messages.map(({author,text})=>author+"\n"+text+"\n").join("\n")
    :requestJsonText(view.body);
  view.output={extension,text};
  clearRequestBodyExport();
  const copy=$("json-copy"),exportLink=$("json-export");
  const copyLabel=t(showText?"复制文本":"复制 JSON"),exportLabel=t(showText?"导出文本":"导出 JSON");
  copy.title=copyLabel;copy.setAttribute("aria-label",copyLabel);copy.disabled=!text;
  exportLink.title=exportLabel;exportLink.setAttribute("aria-label",exportLabel);
  exportLink.setAttribute("aria-disabled",String(!text));
  if(!text)return;
  const type=showText?"text/plain;charset=utf-8":"application/json;charset=utf-8";
  requestBodyExportUrl=URL.createObjectURL(new Blob([text],{type}));
  exportLink.href=requestBodyExportUrl;exportLink.download="codex-request-"+view.id+"."+extension;
}
function clearJsonActions() {
  clearRequestBodyExport();
  $("json-actions").hidden=true;
  $("request-body-text").replaceChildren();
  const results=$("body-results");results.hidden=true;results.replaceChildren();
  setRequestBodyTextMode(requestBodyTextMode);
}
function copyRequestBody() {
  const view=requestBodyView;
  if(view?.id!==state.selected||!view?.output?.text)return;
  return copyWithFeedback(view.output.text,$("json-copy-feedback"));
}
function clearJsonFocus() {
  requestBodyHighlight?.classList.remove("json-focused");requestBodyHighlight=null;
}
function retainedReasoningItemBytes(item,path,sizes,total) {
  if(item?.type!=="reasoning"||!Number.isSafeInteger(total)||total<=0||sizes?.[path]!==total)return null;
  if(typeof item.encrypted_content!=="string")return 0;
  const ciphertext=sizes[`${path}/encrypted_content`];
  return Number.isSafeInteger(ciphertext)&&ciphertext>=2&&ciphertext<=total?ciphertext-2:null;
}
function retainedReasoningEncryptedBytes({body,source_sizes:sizes},reasoningBytes) {
  if(!Array.isArray(body?.input)||!sizes)return null;
  let total=0,encrypted=0;
  for(const [index,item] of body.input.entries()) {
    if(item?.type!=="reasoning")continue;
    const path=`/input/${index}`,size=sizes[path];
    const ciphertext=retainedReasoningItemBytes(item,path,sizes,size);
    if(ciphertext===null)return null;
    total+=size;
    encrypted+=ciphertext;
  }
  return total===reasoningBytes?encrypted:null;
}
function requestJsonText(body) {return JSON.stringify(body,null,2)+"\n";}
function requestBodyData(id) {
  if(requestBodyCache?.id===id)return requestBodyCache.promise;
  const pending={id,promise:null};
  pending.promise=api("/api/packets/"+id+"/export").then(({view,metadata})=>{
    if(!view||!Object.hasOwn(view,"body"))throw new Error("此请求没有可解析的 JSON 正文");
    const reasoningBytes=metadata?.input_groups?.reasoning;
    const reasoningEncryptedBytes=metadata?.reasoning_encrypted_bytes==null&&reasoningBytes>0
      ?retainedReasoningEncryptedBytes(view,reasoningBytes):null;
    return {body:view.body,sourceSizes:view.source_sizes,reasoningEncryptedBytes};
  }).catch(error=>{if(requestBodyCache===pending)requestBodyCache=null;throw error;});
  if(state.selected===id)requestBodyCache=pending;
  return pending.promise;
}
async function ensureJsonView(id,sequence) {
  if(state.detail?.id===id&&state.detail.details_evicted)throw new Error("请求详情已回收，仅保留基本信息。");
  if(requestBodyView?.id===id&&requestBodyView.language===uiI18n.language)return requestBodyView;
  const root=$("json-tree");root.textContent=t("加载中…");requestBodyView=null;clearJsonFocus();clearJsonActions();
  const {body,reasoningEncryptedBytes}=await requestBodyData(id);
  if(state.selected!==id||sequence!==requestBodySequence)return null;
  const nodes=new Map(),node=jsonValueNode(body,null,false,true,"",nodes);
  root.replaceChildren(node);root.scrollTop=0;root.scrollLeft=0;
  requestBodyView={id,body,nodes,root:node,language:uiI18n.language,reasoningEncryptedBytes};
  if(state.detail?.id===id) {
    if(reasoningEncryptedBytes!==null&&state.detail.reasoning_encrypted_bytes==null)renderOverview(state.detail);
    renderHeaders(state.detail);
  }
  setRequestBodyTextMode(requestBodyTextMode);
  $("json-actions").hidden=false;
  return requestBodyView;
}
function focusRequestBodyLine(container,line) {
  clearJsonFocus();requestBodyHighlight=line;line.classList.add("json-focused");
  const offset=line.getBoundingClientRect().top-container.getBoundingClientRect().top;
  container.scrollTop=Math.max(0,container.scrollTop+offset-container.clientHeight*.35);container.scrollLeft=0;
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
  node.jsonExpand?.();
  focusRequestBodyLine($("json-tree"),node.jsonLine||node);
}
async function loadTree(path) {
  clearTimeout(treeQueryTimer);
  if(!state.selected)return;
  const id=state.selected, sequence=++requestBodySequence;
  state.path=path;$("tree-query").value=path;$("body-results").hidden=true;
  $("request-body-view").hidden=false;clearJsonFocus();
  $("tree-query").disabled=!!state.detail?.details_evicted;
  if(state.detail?.details_evicted) {
    requestBodyView=null;requestBodyCache=null;clearJsonActions();
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
async function goPath(path) {setRequestBodyTextMode(false);showTab("tree");await loadTree(path);}
async function switchRequestBodyTextMode(enabled) {
  if(enabled===requestBodyTextMode)return;
  clearTimeout(treeQueryTimer);requestBodySequence++;
  setRequestBodyTextMode(enabled);
  const query=$("tree-query").value.trim();
  if(query&&!query.startsWith("/"))await searchBody(query);
  else $("body-results").hidden=true;
}
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
  if(query.startsWith("/")) {
    await goPath(query);
  } else if(!query) {
    $("body-results").hidden=true;clearJsonFocus();
    if(!requestBodyTextMode)await loadTree("");
  } else await searchBody(query);
}
function requestTextSearchHits(messages,query) {
  const needle=query.toLowerCase(),hits=[];
  for(const [index,{author,text}] of messages.entries()) {
    const offset=text.toLowerCase().indexOf(needle);
    if(offset<0)continue;
    hits.push({index,label:author+" · "+(index+1),preview:text.slice(Math.max(0,offset-45),offset+150)});
    if(hits.length===60)break;
  }
  return hits;
}
async function searchBody(query) {
  const id=state.selected,sequence=++requestBodySequence,root=$("body-results"),textMode=requestBodyTextMode;
  $("request-body-view").hidden=false;clearJsonFocus();
  try {
    const pending=ensureJsonView(id,sequence);
    root.hidden=false;root.replaceChildren(el("small","muted",t("加载中…")));
    const [view,jsonHits]=await Promise.all([pending,textMode?null:api("/api/packets/"+id+"/search?q="+encodeURIComponent(query))]);
    if(!view||state.selected!==id||sequence!==requestBodySequence||$("tree-query").value.trim()!==query)return;
    const results=textMode?requestTextSearchHits(view.messages,query):jsonHits;
    const textRoot=$("request-body-text");
    const jump=hit=>textMode?focusRequestBodyLine(textRoot,textRoot.children[hit.index]):loadTree(hit.path);
    root.replaceChildren(el("small","muted",t("找到 ")+results.length+t(" 项（最多 60 项）")));
    for(const hit of results) {
      const n=el("div","search-hit");
      n.append(link(hit.label||hit.path||t("根节点"),()=>jump(hit)),el("p","",hit.preview));root.append(n);
    }
    if(results.length) {
      if(textMode)jump(results[0]);
      else {state.path=results[0].path||"";focusJsonPath(view,state.path);}
    }
  } catch(error) {
    if(state.selected===id&&sequence===requestBodySequence) {
      root.hidden=true;fail(error);
      if(!requestBodyView)$("json-tree").textContent="";
    }
  }
}
function reasoningItemCiphertextBytes(item) {
  const size=item.reasoning_encrypted_bytes,total=item.bytes;
  return Number.isSafeInteger(total)&&Number.isSafeInteger(size)&&size>=0&&size<=total?size:null;
}
function packetDiffItems(data) {
  const measuredChange=(before,after)=>({before,after,delta:(after??0)-(before??0),
    change:before===null?"added":after===null?"removed":after>before?"grow":after<before?"shrink":"changed"});
  const componentChange=(before,after)=>before===after?{before,after,delta:0,change:null}:measuredChange(before||null,after||null);
  const slot=item=>JSON.stringify([item.index,item.kind,item.name||""]);
  const removed=data.removed||[],oldBySlot=new Map(removed.map(item=>[slot(item),item])),used=new Set(),items=[];
  const itemRow=(beforeItem,afterItem)=>{
    const item=afterItem||beforeItem,before=beforeItem?.bytes??null,after=afterItem?.bytes??null;
    const row={index:item.index,kind:item.kind,name:item.name||"",...measuredChange(before,after),
      path:afterItem?(afterItem.path||"/input/"+afterItem.index):null};
    if(item.kind==="reasoning") {
      const prior=beforeItem?reasoningItemCiphertextBytes(beforeItem):0;
      const next=afterItem?reasoningItemCiphertextBytes(afterItem):0;
      if(prior!==null&&next!==null) {
        const beforePlain=(before??0)-prior,afterPlain=(after??0)-next;
        row.components={encrypted:componentChange(prior,next),plain:componentChange(beforePlain,afterPlain)};
      }
    }
    return row;
  };
  for(const item of data.added||[]) {
    const previous=oldBySlot.get(slot(item));
    if(previous&&!used.has(previous)) {used.add(previous);items.push(itemRow(previous,item));}
    else items.push(itemRow(null,item));
  }
  for(const item of removed)if(!used.has(item))items.push(itemRow(item,null));
  return items.sort((a,b)=>a.index-b.index||Number(a.change!=="removed")-Number(b.change!=="removed"));
}
async function resolveReasoningItemSizes(items,id) {
  const needsSize=item=>item.kind==="reasoning"&&reasoningItemCiphertextBytes(item)===null;
  if(!items.some(needsSize))return items;
  let retained;
  try {retained=await requestBodyData(id);}
  catch(_) {return items;}
  return items.map(item=>{
    if(!needsSize(item))return item;
    const path=`/input/${item.index}`;
    const size=retainedReasoningItemBytes(retained.body?.input?.[item.index],path,retained.sourceSizes,item.bytes);
    return size===null?item:{...item,reasoning_encrypted_bytes:size};
  });
}
async function loadReasoningChangeSizes(current,comparison) {
  const lookup=comparison.reasoningLookup;
  if(!lookup||lookup.loading)return;
  lookup.loading=true;
  const [added,removed]=await Promise.all([
    resolveReasoningItemSizes(lookup.diff.added,current.id),
    resolveReasoningItemSizes(lookup.diff.removed,lookup.previousId)
  ]);
  if(breakdownComparison!==comparison||state.selected!==current.id)return;
  comparison.items=packetDiffItems({added,removed});
  comparison.reasoningLookup=null;
  renderOverview(state.detail);
}
function isPreviousComparisonPacket(previous,current) {
  return !!previous&&previous.thread_id===current.thread_id&&previous.source===current.source&&previous.created<current.created;
}
async function previousComparisonId(data,current,active) {
  if(data.previous_id)return data.previous_id;
  if(data.state!=="details_unavailable"||!current.thread_id||!current.source||!Number.isFinite(current.created))return null;
  // Older relays leave out the previous ID when one of the two records has only statistics.
  const query=new URLSearchParams({limit:"50",thread:current.thread_id,before:String(current.created),sort:"created",direction:"desc",include_get:"1"});
  for(let page=0;page<8;page++) {
    const rows=await api("/api/packets?"+query);
    if(!active()||!Array.isArray(rows))return null;
    const previous=rows.find(row=>isPreviousComparisonPacket(row,current));
    if(previous)return previous.id;
    const next=rows.at(-1)?.id;
    if(rows.length<50||!next||query.get("after")===next)return null;
    query.set("after",next);
  }
  return null;
}
async function loadBreakdownComparison(current) {
  if(breakdownComparison||current.inspection!=="json"||!current.thread_id||isToolSearch(current))return;
  const comparison={data:null,previous:null,items:[]};
  breakdownComparison=comparison;
  const active=()=>breakdownComparison===comparison&&state.selected===current.id;
  try {
    const data=await api("/api/packets/"+current.id+"/diff");
    if(!active()||!data)return;
    const previousId=await previousComparisonId(data,current,active);
    if(!active())return;
    let previous=null;
    if(previousId) {
      try {previous=await api("/api/packets/"+previousId);}
      catch(error) {if(error.status!==404)throw error;}
      if(previous&&!isPreviousComparisonPacket(previous,current))previous=null;
    }
    if(!active())return;
    const {state:comparisonState,fields,fields_available,decoded_delta,compressed_delta}=data;
    comparison.data={state:comparisonState,fields,fields_available,decoded_delta,compressed_delta};
    if(!current.details_evicted&&previous&&!previous.details_evicted) {
      const diff={added:data.added??[],removed:data.removed??[]};
      comparison.items=packetDiffItems(diff);
      if(comparison.items.some(item=>item.kind==="reasoning"&&!item.components))
        comparison.reasoningLookup={diff,previousId:previous.id,loading:false};
    }
    if(previous) {
      const {top_fields,input_groups,json_envelope_bytes,reasoning_encrypted_bytes,encrypted_content_bytes,decoded_bytes,
        outbound_wire_bytes,usage_override_request_bytes,overflow_original_wire_bytes}=previous;
      comparison.previous={top_fields,input_groups,json_envelope_bytes,reasoning_encrypted_bytes,encrypted_content_bytes,decoded_bytes,
        outbound_wire_bytes,usage_override_request_bytes,overflow_original_wire_bytes};
    }
    renderOverview(state.detail);
  } catch(error) {
    if(!active())return;
    breakdownComparison=null;
    if(error.status!==404)throw error;
  }
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
  const retired=/(?:^|[\\/])CodexRequestOptimizer[\\/](?:[a-f\d]{32}|codex-clipboard-[a-f\d-]{36})\.(?:png|jpe?g|webp|gif)$/i;
  const contextPath=value=>{const source=localImagePath(value),path=rewrites.get(source)||source;return path&&!retired.test(path)?path:null;};
  const sources=new Set();
  for(const value of [body?.client_metadata,body?.metadata]) {
    if(!value||typeof value!=="object"||Array.isArray(value))continue;
    const candidates=[value];
    for(const key of ["x-codex-turn-metadata","x-codex-client-metadata"]) {
      let nested=value[key];
      if(typeof nested==="string"&&nested.length<=65536)try{nested=JSON.parse(nested);}catch(_){continue;}
      if(nested&&typeof nested==="object"&&!Array.isArray(nested))candidates.push(nested);
    }
    for(const candidate of candidates)if(candidate.codex_inspector_request_kind==="image_analysis") {
      const path=localImagePath(candidate.codex_inspector_image_source_path);if(path)sources.add(path);
    }
  }
  if(sources.size===1)return [...sources].map(contextPath).filter(Boolean);
  if(meta.image_isolation_applied===true||meta.image_isolation_removed>0) {
    // Show the relay's context replacements, without merging original clipboard references.
    return [...new Set((Array.isArray(meta.image_paths)?meta.image_paths:[])
      .map(contextPath).filter(Boolean))];
  }
  const paths=new Set(), add=value=>{const path=contextPath(value);if(path&&paths.size<32)paths.add(path);};
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
const imagePathOpening=new Set();
function imagePathAction(kind,path) {
  const directory=kind==="directory",reveal=kind==="reveal",label=t(directory?"打开路径":reveal?"在文件夹中显示":"查看图片"),capability=directory?"open_directory_supported":reveal?"reveal_image_supported":"open_image_supported";
  const key=kind+":"+path,button=el("button","image-path-action");
  button.type="button";button.title=label;button.setAttribute("aria-label",label);button.dataset.renderKey="image-action:"+key;
  if(reveal&&state.status&&!state.status.reveal_image_supported)button.title=t("更新桌面程序后可在文件夹中选中图片。");
  button.disabled=!state.status?.[capability]||imagePathOpening.has(key);
  button.setAttribute("aria-busy",String(imagePathOpening.has(key)));
  const icon=document.createElementNS("http://www.w3.org/2000/svg","svg"),shape=document.createElementNS(icon.namespaceURI,"path");
  icon.setAttribute("viewBox","0 0 24 24");icon.setAttribute("aria-hidden","true");icon.setAttribute("focusable","false");
  shape.setAttribute("d",directory||reveal?"M3 20h18V7h-9l-2-3H3z":"M3 3h18v18H3zM3 16l5-5 5 5 3-3 5 5M16 8h.01");icon.append(shape);button.append(icon);
  button.onclick=async event=>{
    if(!state.status?.[capability]||imagePathOpening.has(key))return;
    const target=event.currentTarget;imagePathOpening.add(key);target.disabled=true;target.setAttribute("aria-busy","true");
    try {await api(directory?"/api/open-directory":reveal?"/api/reveal-image":"/api/open-image",{path});}
    catch(error) {fail(error);}
    finally {imagePathOpening.delete(key);target.disabled=!state.status?.[capability];target.setAttribute("aria-busy","false");}
  };
  return button;
}
function renderImagePaths(paths,{showDirectoryRow=true,remote=false}={}) {
  const images=el("div","image-paths");
  const singlePath=showDirectoryRow&&paths.length===1;
  for(const group of imagePathGroups(paths)) {
    const section=el("section","image-path-group");section.dataset.renderKey="image-folder:"+group.directory;
    const opensLocally=!remote||!group.directory.startsWith("/");
    if(showDirectoryRow&&!singlePath) {
      section.setAttribute("aria-label",t("图片目录")+"："+group.directory);
      const directory=el("code","image-path-directory",group.directory);directory.title=group.directory;
      const heading=el("div","image-path-directory-row");
      heading.append(directory);
      if(opensLocally)heading.append(imagePathAction("directory",group.directory));
      section.append(heading);
    }
    const list=el("ul","image-path-list");
    for(const {name,path} of group.files) {
      const item=el("li","image-path-item"),filename=el("code","image-path-name",singlePath?path:name);
      item.dataset.renderKey="image:"+path;
      filename.title=path;
      item.append(filename);
      if(opensLocally) {
        if(singlePath)item.append(imagePathAction("directory",group.directory));
        item.append(imagePathAction("image",path),imagePathAction("reveal",path));
      }
      list.append(item);
    }
    section.append(list);images.append(section);
  }
  return images;
}
function renderHeaders(m) {
  const root=document.createDocumentFragment();
  root.append(requestBasicsPanel(m),requestForwardingPanel(m),requestProtectionPanel(m));
  reconcileContent($("headers-content"),root);renderProtectionPanelState();
}
async function setInterfaceLanguage(language,{persist=true}={}) {
  const changed=language!==uiI18n.language;
  uiI18n.setLanguage(language,{persist});
  if(!changed)return;
  globalThis.InspectorConnectionControls?.render();
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
      showTab(state.tab);renderOverview(state.detail);renderHeaders(state.detail);
      const selected=state.selected, path=state.path, query=$("tree-query").value, searchVisible=!$("body-results").hidden;
      await loadTree(path);
      if(revision!==interfaceLanguageRevision)return;
      if(state.selected===selected && state.path===path && $("tree-query").value===path) {
        $("tree-query").value=query;
        if(searchVisible&&query.trim()&&!query.trim().startsWith("/"))await searchBody(query.trim());
      }
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
  setProxyTestStatus();
  if(state.status)fillSettings();if(contextSettings)fillContextSettings(contextSettings);renderUpdates();
  setSettingsMessage("");
  return restore.catch(fail);
}
function restoreSettings() {
  if(!settingsEditing||settingsSaving)return;
  discardSettingsDraft();
  settingsLanguage=uiI18n.language;settingsBaseline=readSettingsDraft();settingsEditing=true;
  renderLanguageSelection();renderConnectionActions();updateSettingsSaveState();
}
function closeSettings() {
  if(settingsSaving)return;
  closeZstdLevelPicker(false);
  $("settings-dialog").close();return discardSettingsDraft();
}
action("settings-close",closeSettings);action("settings-restore",restoreSettings);
action("proxy-test",testProxySettings);
$("sharing-tab-enabled").addEventListener("change",renderSharingTabHint);
$("proxy-mode").addEventListener("change",()=>{fillProxySettings();updateSettingsSaveState();});
$("proxy-settings").addEventListener("input",()=>setProxyTestStatus());
action("storage-dir-open",()=>openSettingsDirectory("storage-dir"));
$("settings-dialog").addEventListener("cancel",event=>{event.preventDefault();closeSettings();});
$("settings-dialog").addEventListener("close",()=>{
  if(!$("settings-dialog").open){closeZstdLevelPicker(false);discardSettingsDraft();}
});
for(const id of ["capacity-retry-count","capacity-retry-delay"])$(id).addEventListener("input",event=>{
  if(event.target.valueAsNumber>99)event.target.value="99";
});
$("settings-dialog").querySelector(".settings-content").addEventListener("input",event=>{
  if(event.target.matches("#packet-limit, #storage-limit"))renderRecordLimit(event.target);
  updateSettingsSaveState();
});
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
$("image-analysis-mode").addEventListener("change",()=>{fillImageAnalysisMode();});
action("settings-save",saveSettings);
action("ui-refresh",()=>{if(state.status?.ui_live_reload)location.reload();});
action("updates-check",()=>performUpdate("check"));
action("updates-install",()=>performUpdate("install"));
setInterval(()=>{if($("settings-dialog").open&&!settingsSaving&&!updateBusy&&["checking","downloading","upgrading"].includes(updateStatus?.state))loadUpdates();},2000);
setInterval(()=>{if(!document.hidden&&$("settings-dialog").open&&!settingsSaving)loadUpdates();},30000);
action("compression-connect",()=>requestCompressionChange("connected"));
action("compression-disconnect",()=>requestCompressionChange("disconnected"));
action("compression-confirm-cancel",cancelCompressionChange);action("compression-confirm-apply",confirmCompressionChange);
$("compression-confirm-dialog").addEventListener("cancel",event=>{event.preventDefault();cancelCompressionChange();});
$("compression-confirm-dialog").addEventListener("close",()=>{if(!$("compression-confirm-dialog").open)compressionTarget=null;});
action("exit-confirm-cancel",()=>confirmExit("cancel"));action("exit-confirm-direct",()=>confirmExit("direct"));action("exit-confirm-restore",()=>confirmExit("restore"));
$("exit-confirm-dialog").addEventListener("cancel",event=>{event.preventDefault();confirmExit("cancel");});
action("service-restart-cancel",()=>confirmServiceRestart("cancel"));action("service-restart-apply",()=>confirmServiceRestart("restart"));
$("service-restart-dialog").addEventListener("cancel",event=>{event.preventDefault();confirmServiceRestart("cancel");});
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
  automatic.closest(".settings-switch").title=!autoSupported?t("当前中转暂不支持按请求大小自动压缩。"):!$("packet-size-protection").checked?t("请先开启请求大小保护"):"";
  $("packet-size-message").hidden=supported;
  if(!supported)$("packet-size-message").textContent=t("当前中转暂不支持此功能。");
  fillZstdLevel();
}
$("packet-size-protection").addEventListener("change",fillPacketProtection);
$("auto-zstd-level").addEventListener("change",fillZstdLevel);
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
    if(which==="restore"&&(status.profile.active!==false||status.profile.config_error))
      throw Error("恢复未完成，请重试。");
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
const requestInfoTabs=$("request-info-tabs");
for(const button of requestInfoTabs.querySelectorAll("[data-tab]"))button.addEventListener("click",()=>showTab(button.dataset.tab));
requestInfoTabs.addEventListener("keydown",event=>{
  const tabs=Array.from(requestInfoTabs.querySelectorAll("[data-tab]:not([hidden])")),index=tabs.indexOf(event.target);
  const next={ArrowLeft:(index-1+tabs.length)%tabs.length,ArrowRight:(index+1)%tabs.length,Home:0,End:tabs.length-1}[event.key];
  if(index<0||next===undefined)return;
  event.preventDefault();showTab(tabs[next].dataset.tab);tabs[next].focus();
});
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
$("request-body-modes").addEventListener("change",event=>{
  switchRequestBodyTextMode(event.target.value==="chat").catch(fail);
});
$("json-copy").addEventListener("click",copyRequestBody);
function isTrendGeneration(packet) {
  const kinds=[purposeFromMetadata(packet),packet.request_purpose,packet.request_kind,packet.session_kind];
  return hasModelTokenUsage(packet)&&kinds.every(kind=>!kind||["turn","generation","subagent"].includes(kind));
}
class TokenTrendView {
  constructor(services) {
    this.services=services;this.active=false;this.loading=false;this.hydrating=false;
    this.packets=new Map();this.native=new Map();this.hours=1;this.windowEnd=null;this.loadedBounds=null;this.requestBounds=null;this.windowTimer=null;this.previewPackets=null;this.oldestCreated=null;this.boundsReady=false;this.paintRefresh=null;
    this.windowCenter=null;this.sessionSlide=null;this.slideNeedsRender=false;
    this.revision=null;this.lastFull=0;this.query="";this.error=false;this.limited=false;
    this.series=[];this.hover=null;this.hoverChart=null;this.hoverSeries=null;this.hoverEvent=null;this.focusEvent=null;this.frame=null;
    this.emphasis=null;this.focusSeries=null;this.highlightedSeries=null;this.sidebarColors=new Map();
    this.sidebarOutside=new Map();
    this.sessionList=$("session-list");
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
    $("session-search").addEventListener("input",event=>{
      this.query=event.currentTarget.value.trim().toLowerCase();
      this.hover=null;this.hoverChart=null;this.hoverSeries=null;this.emphasis=null;
      this.scheduleRender();
    });
    this.sessionList.addEventListener("pointerover",event=>this.setSidebarEmphasis("emphasis",event.target));
    this.sessionList.addEventListener("pointerleave",()=>this.setSidebarEmphasis("emphasis",null));
    this.sessionList.addEventListener("focusin",event=>this.setSidebarEmphasis("focusSeries",event.target.matches(":focus-visible")?event.target:null));
    this.sessionList.addEventListener("focusout",()=>this.setSidebarEmphasis("focusSeries",null));
    this.resize=new ResizeObserver(()=>this.scheduleRender());
    for(const chart of this.charts) {
      this.resize.observe(chart.plot);
      chart.svg.onpointermove=event=>this.pointAt(event,chart);
      chart.svg.onpointerleave=()=>{this.hover=null;this.hoverChart=null;this.hoverSeries=null;this.renderFocus();};
      chart.svg.onclick=event=>this.scrollSidebarToSession(this.pointAt(event,chart));
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
    if(packet.native_retry_advised===true||[packet.capacity_retry_count,packet.bad_request_retry_count].some(n=>Number.isInteger(n)&&n>0))result.push("retry");
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
  static windowPoints(points,field,start,end) {
    let before=null,after=null;const inside=[];
    for(const point of points) {
      if(point[field]===null)continue;
      if(point.packet.created<start)before=point;
      else if(point.packet.created>end){after=point;break;}
      else inside.push(point);
    }
    if(!inside.length&&(!before||!after))return [];
    return [...(before?[before]:[]),...inside,...(after?[after]:[])];
  }
  static sizeLabel(bytes) {return bytes>=1000&&bytes<1000000?Math.round(bytes/1000)+" KB":compact(bytes);}
  static sizeScale(maximum) {
    return TokenTrendView.scale(maximum||1000);
  }
  static scale(maximum) {
    const rough=(maximum||1000)/5,power=10**Math.floor(Math.log10(rough));
    const step=[1,2,2.5,5,10].map(value=>value*power).find(value=>value>=rough)||power*10;
    return {step,max:Math.max(step,Math.ceil(maximum/step)*step)};
  }
  static toOklab(color) {
    const [red,green,blue]=[1,3,5].map(offset=>{
      const channel=Number.parseInt(color.slice(offset,offset+2),16)/255;
      return channel<=.04045?channel/12.92:((channel+.055)/1.055)**2.4;
    });
    // Linear sRGB to Oklab matrices from https://bottosson.github.io/posts/oklab/
    const l=Math.cbrt(.4122214708*red+.5363325363*green+.0514459929*blue);
    const m=Math.cbrt(.2119034982*red+.6806995451*green+.1073969566*blue);
    const s=Math.cbrt(.0883024619*red+.2817188376*green+.6299787005*blue);
    return [
      .2104542553*l+.7936177850*m-.0040720468*s,
      1.9779984951*l-2.4285922050*m+.4505937099*s,
      .0259040371*l+.7827717662*m-.8086757660*s
    ];
  }
  static seriesPalette=[
    "#245fcb","#b34a09","#087752","#b23873","#7046ac","#006e83","#b52e36","#816204",
    "#566479","#4d6f18","#2f3991","#864330","#13729e","#44255d","#8b2149","#04503f"
  ].map(color=>({color,lab:TokenTrendView.toOklab(color)}));
  static farthestSwatch(available,usedLabs) {
    let bestSlot,bestDistance=-1;
    for(const slot of available) {
      const [lightness,a,b]=TokenTrendView.seriesPalette[slot].lab;
      const distance=Math.min(...usedLabs.map(([otherLightness,otherA,otherB])=>
        (lightness-otherLightness)**2+(a-otherA)**2+(b-otherB)**2));
      if(distance>bestDistance){bestSlot=slot;bestDistance=distance;}
    }
    return bestSlot;
  }
  static timeTicks(start,end,width) {
    const target=(end-start)/Math.max(2,Math.min(8,Math.floor(width/100)));
    const step=[1,2,5,10,15,30,60,120,300,600,1200,1800,3600,7200,10800,21600,43200].find(value=>value>=target)||43200;
    const day=new Date(start*1000);day.setHours(0,0,0,0);const origin=day.getTime()/1000,ticks=[];
    for(let stamp=origin+Math.ceil((start-origin)/step)*step;stamp<=end;stamp+=step)ticks.push(stamp);
    return ticks;
  }
  static compactTimeScale(start,end,activity,coverage={start,end}) {
    const minimumGapSeconds=30*60;
    const intervals=activity.filter(([a,b])=>Number.isFinite(a)&&Number.isFinite(b)&&b>=a&&a-60<=coverage.end&&b+60>=coverage.start)
      .map(([a,b])=>[Math.max(coverage.start,a-60),Math.min(coverage.end,b+60)]).sort((a,b)=>a[0]-b[0]);
    // Keep one global time coordinate: each confirmed long idle interval retains one minute.
    const gaps=[],addGap=(a,b)=>{
      const from=Math.max(start,a),to=Math.min(end,b);
      if(b-a>=minimumGapSeconds&&to>from)gaps.push({start:from,end:to,length:60*(to-from)/(b-a)});
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
    const offset=(value,extendStart=false)=>{
      if(extendStart&&value<start)return value-start;
      const stamp=Math.max(start,Math.min(end,value)),part=locate(stamp,"end");
      return part.a+(stamp-part.start)/(part.end-part.start)*(part.b-part.a);
    };
    const at=(value,extendStart=false)=>{
      if(extendStart&&value<0)return start+value;
      const position=Math.max(0,Math.min(virtual,value)),part=locate(position,"b");
      return part.start+(position-part.a)/(part.b-part.a)*(part.end-part.start);
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
    const extendStart=startOffset<0;
    const fraction=stamp=>(scale.offset(stamp,extendStart)-startOffset)/duration;
    const timeAt=value=>scale.at(startOffset+Math.max(0,Math.min(1,value))*duration,extendStart);
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
    // Centering can add room before the earliest request. The right edge ends at now.
    const endOffset=Number.isFinite(this.windowCenter)?Math.min(scale.length,scale.offset(this.windowCenter,true)+duration/2):
      Math.max(duration,Math.min(scale.length,Number.isFinite(this.windowEnd)?scale.offset(this.windowEnd):scale.length));
    const startOffset=endOffset-duration;
    return {start:scale.at(startOffset,true),end:scale.at(endOffset),startOffset,endOffset,duration,scale};
  }
  covers(bounds,view=this.viewport()) {
    const earliest=view.scale?.start??view.start;
    return !!bounds&&Math.max(view.start,earliest)>=bounds.since-.01&&
      (bounds.before===null||view.end<bounds.before+.01);
  }
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
    // A revealed session at the right edge rejoins the live view.
    if(view.endOffset===view.scale.length)this.windowCenter=null;
    if(Number.isFinite(this.windowEnd))this.windowEnd=view.end;
    this.range.disabled=!this.boundsReady;this.range.setAttribute("aria-busy",String(!this.boundsReady));
    this.range.max="1";this.range.value=String(travel>0?Math.max(0,Math.min(1,view.startOffset/travel)):1);
    const width=this.range.clientWidth||180;
    this.range.style.setProperty("--trend-thumb-width",Math.min(width,Math.max(12,width*duration/view.scale.length))+"px");
    const label=t("{hours} 小时").replace("{hours}",nf.format(Math.round(this.hours*100)/100));
    this.range.setAttribute("aria-valuetext",this.time(view.start)+" – "+this.time(view.end)+" · "+label+(view.scale.gaps.length?" · "+t("已跳过空档"):""));
  }
  navigate(end,scale=this.timeScale()) {
    this.stopSessionSlide();
    const duration=Math.min(scale.length,this.hours*3600);
    // Allow manual panning back through any space added before the earliest request.
    const centeredEnd=Number.isFinite(this.windowCenter)?scale.offset(this.windowCenter,true)+duration/2:null;
    const minEnd=Math.min(duration,centeredEnd??duration);
    end=Math.max(minEnd,Math.min(scale.length,end));
    this.windowCenter=end<duration?scale.at(end-duration/2,true):null;
    this.windowEnd=this.windowCenter!==null||end>=scale.length-.001?null:scale.at(end);
    this.hover=null;this.hoverChart=null;this.hoverSeries=null;this.updateRange();this.scheduleRender();this.ensureWindow();
  }
  revealSession(session) {
    const stamp=this.sidebarOutside.get(session);
    if(!this.active||!Number.isFinite(stamp))return;
    this.stopSessionSlide();
    const apply=()=>{
      this.windowCenter=stamp;this.windowEnd=null;
      this.hover=null;this.hoverChart=null;this.hoverSeries=null;
      this.render(true);this.ensureWindow();
    };
    if(!document.startViewTransition||!globalThis.CSS?.supports("selector(:active-view-transition-type(trend-earlier))")||
      !matchMedia("(prefers-reduced-motion: no-preference)").matches){apply();return;}

    const direction=stamp<this.viewport().start?"trend-earlier":"trend-later";
    this.slideNeedsRender=false;
    const slide=document.startViewTransition({types:[direction],update:()=>{if(this.sessionSlide===slide)apply();}});
    this.sessionSlide=slide;
    const finish=()=>{
      if(this.sessionSlide!==slide)return;
      this.sessionSlide=null;
      if(this.slideNeedsRender)this.scheduleRender();
    };
    slide.finished.then(finish,finish);
  }
  stopSessionSlide() {
    this.sessionSlide?.skipTransition();this.sessionSlide=null;
  }
  setRange(hours,fraction=.5,anchorTime=null) {
    if(!this.boundsReady||!Number.isFinite(hours))return;
    const view=this.viewport(),next=Math.max(1,Math.min(this.rangeLimit(),hours));if(Math.abs(next-this.hours)<1e-9)return;
    fraction=Number.isFinite(fraction)?Math.max(0,Math.min(1,fraction)):.5;
    const anchor=Number.isFinite(anchorTime)?view.scale.offset(anchorTime,Number.isFinite(this.windowCenter)):view.startOffset+view.duration*fraction,duration=Math.min(view.scale.length,next*3600);
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
    this.active=active;this.hover=null;this.hoverChart=null;this.hoverSeries=null;this.hoverEvent=null;this.focusEvent=null;this.emphasis=null;this.focusSeries=null;
    if(!active){this.stopSessionSlide();this.cancelPending();this.highlightSidebar(null);this.guide.replaceChildren();this.timeTooltip.hidden=true;for(const chart of this.charts)chart.tooltip.hidden=true;}
    else {
      this.render();const ticket={};this.paintRefresh=ticket;
      requestAnimationFrame(()=>setTimeout(()=>{if(this.active&&this.paintRefresh===ticket){this.paintRefresh=null;this.refresh();}},0));
    }
  }
  scheduleRender() {if(!this.active||this.frame!==null)return;this.frame=requestAnimationFrame(()=>{this.frame=null;this.render();});}
  async refresh(force=false) {
    const status=this.services.status();
    if(!status||!this.active||document.hidden||this.loading||this.paintRefresh)return;
    const revision=status.storage?.revision,now=Date.now();
    let full=force||!this.lastFull||now-this.lastFull>=30000||!this.covers(this.loadedBounds);
    if(!full&&revision===this.revision){this.render();this.hydrate();return;}
    const controller=new AbortController();this.controller=controller;this.loading=true;this.error=false;
    clearTimeout(this.windowTimer);this.windowTimer=null;
    const timeout=setTimeout(()=>controller.abort(),60000),incoming=new Map();
    const limit=status.trend_packets_supported?300:50,maximum=20000;let published=0;
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
    const sessions=this.services.sessions().filter(session=>session.thread_id!==OTHER_SESSION_GROUP),ownerByThread=new Map();
    for(const session of sessions) {
      for(const member of session.members)ownerByThread.set(member.thread_id,session);
    }
    const groups=new Map(),inRange=new Set();let unknown=0;
    const view=this.viewport(),now=view.end,since=view.start;
    for(const packet of (this.previewPackets||this.packets).values()) {
      if(!Number.isFinite(packet.created))continue;
      const inView=packet.created>=since&&packet.created<=now;
      const session=ownerByThread.get(packet.thread_id);
      if(inView&&session)inRange.add(session.thread_id);
      if(!packet.is_compaction&&!this.services.isGeneration(packet))continue;
      if(!session){if(inView)unknown++;continue;}
      const id=session.thread_id;
      if(!groups.has(id))groups.set(id,{id,title:session.title,session,points:[],latest:0});
      const group=groups.get(id);if(inView)group.latest=Math.max(group.latest,packet.created);
      group.points.push({packet,input:packet.is_compaction?null:this.input(packet),size:TokenTrendView.size(packet),events:TokenTrendView.events(packet)});
    }
    const series=[];
    for(const group of groups.values()) {
      group.points.sort((a,b)=>a.packet.created-b.packet.created||a.packet.id.localeCompare(b.packet.id));
      group.lines=Object.fromEntries(["input","size"].map(field=>[field,TokenTrendView.windowPoints(group.points,field,since,now)]));
      group.points=group.points.filter(point=>point.packet.created>=since&&point.packet.created<=now);
      if(group.points.length||group.lines.input.length||group.lines.size.length){series.push(group);inRange.add(group.id);}
    }
    const outside=new Map(),covered=this.covers(this.loadedBounds,view);
    for(const session of sessions) {
      const stamp=session.latest_packet?.created;
      if(Number.isFinite(stamp)&&!inRange.has(session.thread_id)&&(stamp<since||stamp>now&&covered))
        outside.set("thread:"+session.thread_id,stamp);
    }
    series.sort((a,b)=>b.latest-a.latest||a.id.localeCompare(b.id));
    const matched=series.filter(group=>!this.query||sessionGroupMatchesQuery(group.session,this.query));
    return {series,matched,visible:matched,outside,unknown,now,view};
  }
  assignSeriesColors(series) {
    const previous=new Map(this.series.map(group=>[group.id,group.colorSlot]));
    // Reserve retained colors before assigning any new session, even when it sorts first.
    const occupied=new Set(series.map(group=>previous.get(group.id)).filter(slot=>slot!==undefined));
    const palette=TokenTrendView.seriesPalette,available=new Set(),usedLabs=[];
    for(const [slot,swatch] of palette.entries()) {
      if(occupied.has(slot))usedLabs.push(swatch.lab);
      else available.add(slot);
    }
    let fallback=palette.length;
    for(const group of series) {
      let slot=previous.get(group.id);
      if(slot===undefined) {
        if(available.size) {
          slot=TokenTrendView.farthestSwatch(available,usedLabs);
          available.delete(slot);usedLabs.push(palette[slot].lab);
        } else {
          while(occupied.has(fallback))fallback++;
          slot=fallback++;
        }
      }
      group.colorSlot=slot;
      group.color=palette[slot]?.color??`hsl(${((slot-palette.length)*137.508)%360} 68% 28%)`;
    }
  }
  svgNode(tag,attrs={},text) {const node=document.createElementNS("http://www.w3.org/2000/svg",tag);for(const [name,value]of Object.entries(attrs))node.setAttribute(name,String(value));if(text!==undefined)node.textContent=text;return node;}
  time(value,withSeconds=false) {
    const date=new Date(value*1000),locale=uiI18n.language==="en"?"en-GB":"zh-CN";
    return date.toLocaleString(locale,{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",...(withSeconds?{second:"2-digit"}:{})});
  }
  setSidebarEmphasis(field,target) {
    if(!this.active)return;
    const session=target?.closest(".session-row")?.dataset.session;
    const series=this.sidebarColors.has(session)?session.slice(7):null;
    if(this[field]===series)return;
    this[field]=series;
    this.renderFocus();
  }
  styleSessionRow(row) {
    const color=this.active?this.sidebarColors.get(row.dataset.session):null;
    if(color)row.style.setProperty("--trend-series-color",color);
    else row.style.removeProperty("--trend-series-color");
    row.toggleAttribute("data-trend-series",!!color);
    row.toggleAttribute("data-trend-outside",this.active&&this.sidebarOutside.has(row.dataset.session));
    row.toggleAttribute("data-trend-highlight",!!color&&row.dataset.session==="thread:"+this.highlightedSeries);
  }
  highlightSidebar(series) {
    if(series===this.highlightedSeries)return;
    this.highlightedSeries=series;
    this.sessionList.toggleAttribute("data-trend-emphasis",!!series);
    for(const row of this.sessionList.querySelectorAll(".session-row[data-trend-series]")) {
      row.toggleAttribute("data-trend-highlight",!!series&&row.dataset.session==="thread:"+series);
    }
  }
  scrollSidebarToSession(series) {
    if(!this.active||!series)return;
    const list=this.sessionList,session="thread:"+series;
    const row=Array.from(list.querySelectorAll(".session-row")).find(node=>node.dataset.session===session);
    if(!row)return;
    const listTop=list.getBoundingClientRect().top+list.clientTop,listHeight=list.clientHeight;
    const bounds=row.getBoundingClientRect();
    const visible=bounds.top>=listTop&&bounds.bottom<=listTop+listHeight;
    const top=visible?0:bounds.top-listTop-(listHeight-bounds.height)/2;
    const behavior=matchMedia("(prefers-reduced-motion: no-preference)").matches?"smooth":"instant";
    list.scrollBy({top,behavior});
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
  render(duringSlide=false) {
    if(!this.active)return;
    if(this.sessionSlide&&!duringSlide){this.slideNeedsRender=true;return;}
    this.updateRange();const model=this.model();this.assignSeriesColors(model.series);this.series=model.series;
    this.sidebarColors=new Map(model.visible.map(group=>["thread:"+group.id,group.color]));
    this.sidebarOutside=model.outside;
    for(const row of this.sessionList.querySelectorAll(".session-row"))this.styleSessionRow(row);
    if(!model.visible.some(group=>group.id===this.hoverSeries&&(group.lines.input.length||group.lines.size.length)))this.hoverSeries=null;
    if(!this.sidebarColors.has("thread:"+this.emphasis))this.emphasis=null;
    if(!this.sidebarColors.has("thread:"+this.focusSeries))this.focusSeries=null;
    const pointSeries=this.hover?model.visible.find(group=>group.points.some(point=>point.packet.id===this.hover&&(point.input!==null||point.size!==null)))?.id:null;
    const emphasis=this.eventKey()?null:this.emphasis||this.hoverSeries||pointSeries||this.focusSeries;
    const {start,end}=model.view;
    const axis=this.chartAxis(model.view,Math.max(1,this.charts[0].plot.clientWidth-132));
    for(const chart of this.charts)this.renderChart(chart,model,start,end,emphasis,axis);
    this.renderTimeAxis();
    this.renderFocus();
  }
  renderChart(chart,model,start,end,emphasis,axis) {
    const width=chart.plot.clientWidth,height=chart.plot.clientHeight;
    if(!width||!height){chart.points=[];chart.linePoints=[];chart.eventNodes=[];chart.bounds=null;return;}
    chart.eventKey=this.eventKey();chart.svg.classList.toggle("trend-event-filtering",!!chart.eventKey);chart.eventNodes=[];
    const left=104,right=28,top=30,bottom=12,plotWidth=Math.max(1,width-left-right),plotHeight=Math.max(1,height-top-bottom);
    const known=model.visible.flatMap(group=>group.lines[chart.field]);
    const budget=this.services.status()?.budget_bytes,limit=chart.key==="size"&&Number.isSafeInteger(budget)&&budget>0?budget:null;
    const maximum=Math.max(limit??0,...known.map(point=>point[chart.field])),scale=chart.key==="size"?TokenTrendView.sizeScale(maximum):TokenTrendView.scale(maximum);
    const x=value=>left+axis.fraction(value)*plotWidth,y=value=>top+plotHeight-value/scale.max*plotHeight;
    const ticks=axis.ticks;
    chart.bounds={width,height,left,right,top,bottom,start,end,x,ticks,axis};
    const root=document.createDocumentFragment();chart.svg.setAttribute("viewBox",`0 0 ${width} ${height}`);
    const clipId=chart.prefix+"-viewport",clip=this.svgNode("clipPath",{id:clipId}),clipDefs=this.svgNode("defs");
    clip.append(this.svgNode("rect",{x:left,y:top,width:plotWidth,height:plotHeight}));clipDefs.append(clip);root.append(clipDefs);
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
        band.append(this.svgNode("title",{},t("无请求时间段：{start} – {end}").replace("{start}",this.time(gap.start,true)).replace("{end}",this.time(gap.end,true))),
          this.svgNode("rect",{x:from,y:top,width:to-from,height:plotHeight,class:"trend-fold-shade"}),
          this.svgNode("rect",{x:from,y:top,width:to-from,height:plotHeight,fill:`url(#${id})`}),
          this.svgNode("path",{d:`M${from},${top}V${height-bottom}M${to},${top}V${height-bottom}`,class:"trend-fold-edge"}));root.append(band);
      }
    }
    chart.points=[];chart.linePoints=[];
    for(const group of model.visible) {
      // Set brightness before attachment: layout reads can otherwise start a fade from full opacity.
      const highlight=emphasis?(group.id===emphasis?" emphasized":" dimmed"):"";
      const layer=this.svgNode("g",{class:"trend-series-line"+highlight,"data-series":group.id,"clip-path":`url(#${clipId})`}),events=this.svgNode("g");let path="",pen=false,lastDot=null,lastPoint=null;
      for(const source of group.lines[chart.field]) {
        const value=source[chart.field],inView=source.packet.created>=start&&source.packet.created<=end;
        const point={...source,value,series:group,inView,x:x(source.packet.created),y:y(value)};
        path+=(pen?"L":"M")+point.x.toFixed(2)+","+point.y.toFixed(2);pen=true;chart.linePoints.push(point);
        if(!inView)continue;chart.points.push(point);
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
        mark.append(this.svgNode("title",{},t("无请求时间段：{start} – {end}").replace("{start}",this.time(gap.start,true)).replace("{end}",this.time(gap.end,true))),
          this.svgNode("rect",{x:center-11,y:0,width:22,height:13,rx:3}),this.svgNode("path",{d:`M${center-offset+1},2l-2,7M${center+offset+1},2l-2,7`}));root.append(mark);
      }
    }
    this.timeAxis.replaceChildren(root);
  }
  pointAt(event,chart) {
    if(!chart.bounds)return;
    const rect=chart.svg.getBoundingClientRect();if(!rect.width||!rect.height)return;
    const x=event.clientX-rect.left,y=event.clientY-rect.top,scaleX=rect.width/chart.bounds.width,scaleY=rect.height/chart.bounds.height;
    if(x<chart.bounds.left*scaleX||x>(chart.bounds.width-chart.bounds.right)*scaleX||y<chart.bounds.top*scaleY||y>(chart.bounds.height-chart.bounds.bottom)*scaleY) {
      this.hover=null;this.hoverSeries=null;this.hoverChart=chart.key;this.renderFocus();return;
    }
    let nearest=null,distance=28*28,line=null,lineDistance=8*8,previous=null,lastX=0,lastY=0;
    for(const point of chart.linePoints) {
      const px=point.x*scaleX,py=point.y*scaleY,value=(px-x)**2+(py-y)**2;
      if(point.inView&&value<distance){nearest=point;distance=value;}
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
    return series;
  }
  renderFocus() {
    if(!this.active)return;
    const event=this.eventKey(),anchor=event?null:this.charts.map(chart=>chart.points.find(value=>value.packet.id===this.hover)).find(Boolean),emphasis=event?null:this.emphasis||this.hoverSeries||anchor?.series.id||this.focusSeries;
    for(const {key,button} of this.eventLegend)button.classList.toggle("highlighted",key===event);
    this.highlightSidebar(emphasis||null);
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
      for(const line of chart.svg.querySelectorAll("[data-series]")) {
        line.classList.toggle("dimmed",!!emphasis&&line.dataset.series!==emphasis);
        line.classList.toggle("emphasized",line.dataset.series===emphasis);
      }
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
tokenTrendView=new TokenTrendView({api,status:()=>state.status,sessions:sessionGroups,peek:listTokens,read:readListTokens,isGeneration:isTrendGeneration});
function activateTrendsAfterSlide(pane) {
  if(trendActivation)return;
  const ticket={};trendActivation=ticket;
  const activate=()=>{
    if(trendActivation!==ticket)return;
    trendActivation=null;
    tokenTrendView.setActive(true);
  };
  const afterPaint=()=>{
    if(trendActivation===ticket)requestAnimationFrame(()=>setTimeout(activate,0));
  };
  // Rendering large cached charts can block the frames that move the view.
  const slide=pane?.getAnimations().find(animation=>animation.transitionProperty==="transform");
  if(slide)slide.finished.then(afterPaint,afterPaint);
  else afterPaint();
}
function renderSharingNavigation(status) {
  const tab=$("view-sharing");
  // Keep sharing accessible with older backends that lack this preference.
  const visible=status.sharing_tab_enabled!==false;
  const moveFocus=!visible&&(document.activeElement===tab||$("sharing-view").contains(document.activeElement));
  tab.hidden=!visible;
  if(restoreSharingView||(!visible&&workspaceView==="sharing"))setWorkspaceView(visible?"sharing":"records");
  if(moveFocus)$("view-records").focus();
}
function setWorkspaceView(view) {
  if(view==="sharing"&&$("view-sharing").hidden)return;
  restoreSharingView=false;
  hideSessionTitleTooltip();
  const wasSharing=workspaceView==="sharing";
  workspaceView=["trends","sharing"].includes(view)?view:"records";
  const workspace=$("records-view"),sharing=workspaceView==="sharing",trends=workspaceView==="trends";
  workspace.inert=sharing;
  if(!sharing){
    // Settle the next view behind the sharing panel before it slides out.
    workspace.classList.toggle("skip-view-slide",wasSharing);
    const layoutChanged=workspace.dataset.view!==workspaceView;
    workspace.dataset.view=workspaceView;
    if(layoutChanged&&state.status)renderSessions();
  }
  if(workspaceView==="records"&&state.tab==="tree")scrollRequestTextOnFirstShow();
  $("token-trend-view").inert=!trends;
  for(const pane of workspace.querySelectorAll(":scope > .requests-sidebar, :scope > .detail"))pane.inert=trends;
  if($("sharing-view"))$("sharing-view").inert=!sharing;
  $("view-records").setAttribute("aria-pressed",String(workspaceView==="records"));$("view-trends").setAttribute("aria-pressed",String(workspaceView==="trends"));
  $("view-sharing")?.setAttribute("aria-pressed",String(sharing));
  try {localStorage.setItem("packet-inspector-view",workspaceView);} catch(_) {}
  if(trends)activateTrendsAfterSlide(wasSharing?$("sharing-view"):$("token-trend-view"));
  else {trendActivation=null;tokenTrendView.setActive(false);}
  globalThis.InspectorSharing?.setActive(sharing);
  if(sharing&&!globalThis.InspectorSharing&&$("sharing-message")) {
    $("sharing-message").textContent=t("当前后台不支持共享，请更新软件后使用。");$("sharing-message").hidden=false;
    if($("sharing-cards"))$("sharing-cards").hidden=true;
  }
  if(workspaceView!=="records"){cancelTokenRead();state.listController?.abort();}else if(state.status){loadPackets(false,true).catch(fail);loadTokenWindow();loadListTokens();}
}
$("view-records").onclick=()=>setWorkspaceView("records");$("view-trends").onclick=()=>setWorkspaceView("trends");
$("view-sharing")?.addEventListener("click",()=>setWorkspaceView("sharing"));
try {
  const saved=localStorage.getItem("packet-inspector-view");
  restoreSharingView=saved==="sharing";
  if(saved==="trends")setWorkspaceView(saved);
} catch(_) {}
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
globalThis.InspectorConnectionControls=InspectorConnections.create({request:api});
async function initializePage() {
  const startup=document.body.dataset.desktopStartup;
  const progress=$("desktop-startup-status");
  const workspace=document.querySelector(".workspace-stage");
  const controls=startup?[$("view-toggle"),document.querySelector(".header-actions"),workspace]:[];
  state.polling=true;
  try {
    if(startup) {
      for(const control of controls)control.inert=true;
      if(progress)progress.hidden=false;
      if(startup==="service") {
        if(progress)uiI18n.text(progress,"正在启动中转…");
        let service;
        do {service=await api("/api/desktop-startup");} while(!service.ready);
      }
      if(progress)uiI18n.text(progress,"正在加载会话…");
    }
    await Promise.all([refreshStatus(true),refreshFilters()]);
    if(workspaceView==="trends")void tokenTrendView.refresh();
    await refreshSessionSummaries();
  } finally {
    if(startup) {
      if(progress)progress.hidden=true;
      delete document.body.dataset.desktopStartup;
      for(const control of controls)control.inert=false;
    }
    state.polling=false;
  }
}
initializePage().catch(fail);
