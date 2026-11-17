"use strict";

globalThis.InspectorConnections = {
  create({request, status, localBusy, refreshLocal, localActivity=()=>{}}) {
    const get=id=>document.getElementById(id), tr=value=>uiI18n.t(value);
    const panel=get("connection-panel"), controls=get("connection-controls"), trigger=get("connection-devices-open");
    const dialog=get("devices-confirm-dialog"), list=get("connection-devices-list");
    const nativePopover="popover" in HTMLElement.prototype;
    const nodes=new Map(), confirmNodes=new Map(), pending=new Map();
    let rows=[],loaded=false,loading=false,submitting=false,catalogError="",revision="",timer=null,closeTimer=null,hover=false;
    let plan=null,finished=false,disposed=false;
    const text=(node,value)=>{if(node.textContent!==value)node.textContent=value;};
    const name=row=>row.kind==="local"?tr("本机"):row.name;
    const isOpen=()=>nativePopover?panel.matches(":popover-open"):!panel.hidden;
    const busy=row=>!!row.busy||(row.id==="local"&&localBusy());
    const anyBusy=()=>rows.some(busy)||submitting;
    const desired=action=>action==="activate"?"connected":"disconnected";
    function stateText(row) {
      if(row.busy)return tr(row.action==="activate"?"正在接入…":row.action==="restore"?"正在断开…":"正在检查…");
      if(row.error||row.state==="error")return tr("接入异常");
      const value=tr(row.state==="connected"?"已接入":row.state==="disconnected"?"已断开":"待检查");
      if(row.reload_required)return value+" · "+tr(row.kind==="local"?"请重启 ChatGPT App":"请重新连接远程 Codex");
      return value+(row.warning_count?" · "+tr("部分配置未完成，请检查此设备的接入详情。"):"");
    }
    function element(tag,cls="") {const node=document.createElement(tag);node.className=cls;return node;}
    function rowNodes(row) {
      if(nodes.has(row.id))return nodes.get(row.id);
      const root=element("div","connection-device"),info=element("div","connection-device-info");
      const title=element("strong"),state=element("span","connection-device-state"),error=element("span","connection-error");
      const buttons=element("div","language-toggle connection-device-actions");buttons.setAttribute("role","group");
      const actions={};
      for(const action of ["restore","activate"]) {
        const button=element("button");button.type="button";
        button.addEventListener("click",()=>confirm(action,[row.id]));buttons.append(button);actions[action]=button;
      }
      info.append(title,state,error);root.append(info,buttons);list.append(root);
      const value={root,title,state,error,buttons,actions};nodes.set(row.id,value);return value;
    }
    function render() {
      localActivity(rows.some(row=>row.id==="local"&&row.busy)||pending.has("local"));
      const active=rows.length&&rows.every(row=>row.state==="connected"&&!row.error);
      const disconnected=rows.length&&rows.every(row=>row.state==="disconnected"&&!row.error);
      for(const [id,action,pressed] of [["compression-connect","activate",active],["compression-disconnect","restore",disconnected]]) {
        const button=get(id);button.setAttribute("aria-pressed",String(!!pressed));
        button.disabled=!loaded||anyBusy()||localBusy()||!!pressed;
        button.title=tr(action==="activate"?"接入全部设备":"断开全部设备");
      }
      get("request-compression").setAttribute("aria-busy",String(!loaded||anyBusy()));
      get("request-compression").title=tr("控制本机和全部服务器；悬停可单独切换。");
      text(get("compression-hint"),tr("控制本机和全部服务器；悬停可单独切换。"));
      for(const [id,value] of nodes)if(!rows.some(row=>row.id===id)){value.root.remove();nodes.delete(id);}
      for(const row of rows) {
        const value=rowNodes(row);text(value.title,name(row));value.title.title=name(row);
        text(value.state,stateText(row));value.root.dataset.state=row.error?"error":row.state;
        value.root.setAttribute("aria-busy",String(!!row.busy));
        text(value.error,row.error?tr(row.error):row.warning_count?tr("部分配置未完成，请检查此设备的接入详情。") : "");
        value.error.hidden=!value.error.textContent;
        value.buttons.setAttribute("aria-label",name(row));
        for(const [action,button] of Object.entries(value.actions)) {
          text(button,tr(action==="activate"?"接入":"断开"));
          button.setAttribute("aria-pressed",String(row.state===desired(action)&&!row.error));
          button.disabled=busy(row)||submitting||(row.state===desired(action)&&!row.error);
        }
      }
      const message=catalogError||(!loaded?tr("正在读取设备列表…"):"");
      text(get("connection-devices-error"),tr(message));get("connection-devices-error").hidden=!message;
      get("connection-devices-refresh").disabled=loading||anyBusy();
      renderConfirmation();
    }
    function renderConfirmation() {
      if(!plan)return;
      if(catalogError){text(get("devices-confirm-error"),tr(catalogError));get("devices-confirm-error").hidden=false;}
      const selected=plan.targets.map(target=>rows.find(row=>row.id===target.id)||{...target,state:"error",error:tr("设备列表已变化，请重新确认影响范围。")});
      for(const row of selected) {
        let node=confirmNodes.get(row.id);
        if(!node){node=element("li");const title=element("strong"),detail=element("span");node.append(title,detail);confirmNodes.set(row.id,node);get("devices-confirm-targets").append(node);}
        text(node.firstChild,name(row));text(node.lastChild,(row.error?tr(row.error):stateText(row)));
        node.dataset.state=row.error?"error":row.state;
      }
      const running=selected.some(row=>pending.has(row.id));
      const failed=finished&&selected.some(row=>row.error||row.state!==desired(plan.action));
      get("devices-confirm-apply").hidden=finished&&!failed;
      get("devices-confirm-apply").disabled=submitting||running||selected.some(busy)||!!catalogError;
      text(get("devices-confirm-apply"),tr(submitting||running?"正在执行…":failed?"重试失败设备":plan.action==="activate"?"确认接入":"确认断开"));
      text(get("devices-confirm-cancel"),tr(finished?"完成":running?"关闭":"取消"));
      dialog.setAttribute("aria-busy",String(submitting||running));
    }
    function place() {
      if(!isOpen()||CSS.supports("position-area: block-end span-inline-start"))return;
      const r=controls.getBoundingClientRect(),p=panel.getBoundingClientRect();
      panel.style.left=Math.max(12,Math.min(r.right-p.width,innerWidth-p.width-12))+"px";
      panel.style.top=Math.max(12,Math.min(r.bottom+6,innerHeight-p.height-12))+"px";
    }
    function hide() {
      clearTimeout(closeTimer);
      if(nativePopover){if(isOpen())panel.hidePopover();}else{panel.hidden=true;trigger.setAttribute("aria-expanded","false");}
    }
    function show() {
      clearTimeout(closeTimer);
      if(document.querySelector("dialog[open]"))return;
      if(!isOpen()) {
        if(nativePopover)panel.showPopover({source:trigger});else{panel.hidden=false;trigger.setAttribute("aria-expanded","true");}
      }
      place();poll();
    }
    function leave() {
      hover=false;clearTimeout(closeTimer);
      closeTimer=setTimeout(()=>{if(!hover&&!controls.contains(document.activeElement)&&!panel.contains(document.activeElement))hide();},180);
    }
    function schedule() {
      clearTimeout(timer);
      if(disposed)return;
      if(pending.size||rows.some(row=>row.busy))timer=setTimeout(poll,700);
      else if(isOpen()||dialog.open)timer=setTimeout(poll,15000);
    }
    async function poll(refresh=false) {
      if(loading||disposed)return;
      loading=true;
      try {
        const value=await request("/api/connections"+(refresh?"?refresh=1":""));
        if(!Array.isArray(value.targets))throw Error(tr("无法读取设备接入状态。"));
        rows=value.targets;loaded=true;catalogError=value.error||"";revision=value.revision;
        let localCompleted=false;
        for(const [id,operation] of pending) {
          const row=rows.find(target=>target.id===id);
          if(row&&!row.busy&&row.operation_id===operation){pending.delete(id);localCompleted ||= id==="local";}
          else if(!row||row.operation_id!==operation){pending.delete(id);catalogError=tr("设备状态已变化，请刷新后检查操作结果。");}
        }
        if(plan?.submitted&&!plan.targets.some(row=>pending.has(row.id)))finished=true;
        if(localCompleted){localActivity(false);await refreshLocal();}
      } catch(error) {catalogError=error.message;}
      finally {loading=false;render();place();schedule();}
    }
    async function confirm(action,identities=null) {
      if(dialog.open||document.querySelector("dialog[open]"))return;
      await poll();
      if(disposed||dialog.open||document.querySelector("dialog[open]"))return;
      const targets=rows.filter(row=>identities?identities.includes(row.id):true);
      if(!targets.length||(!identities&&catalogError)) {show();return;}
      hide();finished=false;plan={action,targets:targets.map(({id,name,kind})=>({id,name,kind})),revision,submitted:false};
      confirmNodes.clear();get("devices-confirm-targets").replaceChildren();
      const all=identities===null;
      text(get("devices-confirm-title"),tr(action==="activate"?(all?"接入全部设备":"接入所选设备"):(all?"断开全部设备":"断开所选设备")));
      text(get("devices-confirm-description"),tr(action==="activate"?
        "将修改以下设备的 Codex 配置，请求通过各设备上运行的中转软件发送。":
        "将恢复以下设备的原始出口并开启 Codex 原生压缩，后续请求不再通过对应中转。中转服务保持运行。"));
      const scope=targets.some(row=>row.kind==="local")?tr("本机操作包含已检测到的 WSL 配置。")+" ":"";
      text(get("devices-confirm-hint"),scope+tr("配置变化后，本机请重启 ChatGPT App，服务器请重新连接对应的远程 Codex；未变化的设备无需重新加载。"));
      get("devices-confirm-error").hidden=true;render();dialog.showModal();schedule();
    }
    async function apply() {
      if(!plan||submitting||plan.targets.some(row=>pending.has(row.id)))return;
      const targets=plan.targets.filter(target=>!finished||rows.find(row=>row.id===target.id)?.error||rows.find(row=>row.id===target.id)?.state!==desired(plan.action));
      if(targets.some(target=>{const row=rows.find(row=>row.id===target.id);return !row||busy(row);}))return;
      submitting=true;get("devices-confirm-error").hidden=true;render();
      try {
        const value=await request("/api/connections",{targets:targets.map(row=>row.id),action:plan.action,revision:plan.revision});
        if(!value.accepted||typeof value.accepted!=="object")throw Error(tr("未能确认操作已开始，请刷新状态后检查。"));
        for(const [id,operation] of Object.entries(value.accepted))pending.set(id,operation);
        plan.submitted=true;finished=false;
        for(const row of rows)if(pending.has(row.id))Object.assign(row,{busy:true,action:plan.action,error:""});
      } catch(error) {text(get("devices-confirm-error"),error.message);get("devices-confirm-error").hidden=false;}
      finally {submitting=false;render();schedule();}
    }
    if(!nativePopover){panel.hidden=true;panel.classList.add("connection-panel-fallback");}
    else panel.addEventListener("toggle",()=>{trigger.setAttribute("aria-expanded",String(isOpen()));if(isOpen()){place();poll();}});
    for(const node of [controls,panel]) {
      node.addEventListener("pointerenter",event=>{if(event.pointerType==="touch")return;hover=true;show();});
      node.addEventListener("pointerleave",leave);
      node.addEventListener("focusin",show);
      node.addEventListener("focusout",leave);
    }
    trigger.addEventListener("click",show);
    document.addEventListener("pointerdown",event=>{if(!controls.contains(event.target)&&!panel.contains(event.target))hide();});
    document.addEventListener("keydown",event=>{if(event.key==="Escape"&&isOpen()){hide();event.stopPropagation();}});
    globalThis.addEventListener("resize",place);
    get("compression-connect").addEventListener("click",()=>confirm("activate"));
    get("compression-disconnect").addEventListener("click",()=>confirm("restore"));
    get("connection-devices-refresh").addEventListener("click",()=>poll(true));
    get("devices-confirm-apply").addEventListener("click",apply);
    get("devices-confirm-cancel").addEventListener("click",()=>{dialog.close();schedule();});
    globalThis.addEventListener("pagehide",()=>{disposed=true;clearTimeout(timer);clearTimeout(closeTimer);});
    render();poll();
    return {sync(){
      const local=rows.find(row=>row.id==="local"),s=status();
      if(local&&!local.busy&&!pending.has("local")&&s?.profile) {
        local.state=s.profile.config_error?"error":s.profile.active?"connected":"disconnected";
      }
      render();
    }};
  }
};
