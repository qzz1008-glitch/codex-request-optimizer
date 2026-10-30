"use strict";

globalThis.InspectorSharing=(()=>{
  const get=id=>document.getElementById(id),translate=value=>typeof t==="function"?t(value):value;
  let active=false,busy=false,current=null,timer=null,controller=null,recoveryShown=false,exitDraft=false;
  const message=(text,error=false)=>{const box=get("sharing-message");box.textContent=translate(text);box.hidden=!text;box.classList.toggle("error",error);};
  const text=(id,value)=>{get(id).textContent=translate(value);};
  async function request(data,signal){
    const response=await fetch("/api/sharing",{method:data?"POST":"GET",cache:"no-store",headers:data?{"Content-Type":"application/json"}:{},body:data?JSON.stringify(data):undefined,signal});
    if(response.status===404){const error=new Error(translate("当前后台不支持共享，请更新软件后使用。"));error.unsupported=true;throw error;}
    const result=await response.json();if(!response.ok)throw new Error(translate(result.error||"共享操作失败。"));return result;
  }
  function render(value){
    if(!(value.protocol>=3)){current=null;get("sharing-cards").hidden=true;message("当前后台不支持独立额度与出口共享，请更新软件。",true);return;}
    get("sharing-cards").hidden=false;
    current=value;const host=value.host||{},client=value.client||{},hosting=host.running===true||host.active===true,using=client.active===true;
    text("sharing-host-state",host.active?"共享中":hosting?"已暂停":"未开启");get("sharing-host-state").classList.toggle("active",host.active===true);
    text("sharing-client-state",using?"借用中":"本机额度");get("sharing-client-state").classList.toggle("active",using);
    for(const id of ["sharing-hours","sharing-token-limit"])get(id).disabled=busy||hosting||using;
    if(hosting&&host.token_limit!==undefined)get("sharing-token-limit").value=host.token_limit;
    get("sharing-start").hidden=hosting;get("sharing-start").disabled=busy||using;
    for(const id of ["sharing-stop","sharing-rotate"]){get(id).hidden=!hosting;get(id).disabled=busy;}
    get("sharing-rotate").disabled=busy||!host.active;
    get("sharing-connect").hidden=false;get("sharing-connect").disabled=busy||hosting;text("sharing-connect",using?"更换额度来源":"连接");
    get("sharing-disconnect").hidden=!using;get("sharing-disconnect").disabled=busy;
    get("sharing-remote-link").disabled=busy||hosting;
    if(value.link)get("sharing-link").value=value.link;
    if(!host.active)get("sharing-link").value="";
    get("sharing-link-area").hidden=!get("sharing-link").value;
    get("sharing-copy").disabled=busy;
    get("sharing-host-detail").textContent=host.error?translate(host.error):hosting?`${translate("已转发")} ${host.requests||0} · ${translate("进行中")} ${host.in_flight||0} · ${translate("到期")} ${new Date(host.expires_at*1000).toLocaleString()}`:"";
    const usage=host.usage,format=value=>Number(value||0).toLocaleString();
    get("sharing-usage").hidden=!usage||!host.started_at;
    if(usage){
      const unknown=!usage.responses&&usage.missing_responses>0;
      text("sharing-total-tokens",unknown?"—":format(usage.total_tokens));
      text("sharing-input-tokens",unknown?"—":format(usage.input_tokens));
      text("sharing-output-tokens",unknown?"—":format(usage.output_tokens));
      text("sharing-cached-tokens",usage.cached_responses?format(usage.cached_input_tokens):"—");
      text("sharing-token-budget",host.token_limit?`${translate("上限")} ${format(host.token_limit)}`:"");
      get("sharing-usage-coverage").hidden=!usage.missing_responses;
      text("sharing-usage-coverage",`${translate("未取得用量")} ${format(usage.missing_responses)} · ${translate("累计值可能不完整。")}`);
    }
    renderExit(value);
    if(value.recovery_error)message(value.recovery_error,true);
    else if(value.recovered&&!recoveryShown){message("上次共享已结束，已恢复原连接配置。请重新加载 Codex 配置或重启 ChatGPT 应用。");recoveryShown=true;}
  }
  function renderExit(value){
    const supported=value.egress?.supported===true,host=value.egress?.host||{},selection=value.egress?.selection||{mode:"provider"},running=host.active===true;
    for(const id of ["exit-start","exit-stop","exit-rotate","exit-copy","exit-apply"])get(id).disabled=busy||!supported;
    get("exit-hours").disabled=busy||running||!supported;
    text("exit-host-state",running?"共享中":"未开启");get("exit-host-state").classList.toggle("active",running);
    get("exit-start").hidden=running;for(const id of ["exit-stop","exit-rotate","exit-copy"])get(id).hidden=!running;
    if(value.egress_link)get("exit-link").value=value.egress_link;
    if(!running)get("exit-link").value="";get("exit-link-area").hidden=!get("exit-link").value;
    const size=bytes=>`${(Number(bytes||0)/1000).toLocaleString(undefined,{maximumFractionDigits:1})} kB`;
    get("exit-host-detail").textContent=running?`${translate("连接")} ${host.connections||0} · ${translate("进行中")} ${host.in_flight||0} · ↑ ${size(host.sent_bytes)} · ↓ ${size(host.received_bytes)}`:"";
    if(!exitDraft)get("exit-source").value=selection.mode;
    get("exit-source").disabled=busy||!supported;get("exit-peer-link").disabled=busy||!supported;
    get("exit-peer-fields").hidden=get("exit-source").value!=="peer";
    const names={provider:"额度提供方出口",local:"本机出口",peer:"共享出口"};
    text("exit-selection-state",names[selection.mode]||names.provider);
    get("exit-selection-detail").textContent=selection.mode==="local"&&value.client?.active?translate("使用本机出口，额度由提供方扣除。"):"";
  }
  async function refresh(){
    if(!active||busy||document.hidden)return;
    controller?.abort();const pending=new AbortController();controller=pending;
    try{render(await request(null,pending.signal));}catch(error){if(error.name!=="AbortError"){if(error.unsupported)get("sharing-cards").hidden=true;message(error.message,true);}}finally{if(controller===pending)controller=null;}
  }
  async function action(button,data,success){
    if(busy)return;busy=true;controller?.abort();button.setAttribute("aria-busy","true");if(current)render(current);else button.disabled=true;message("");
    try{const result=await request(data);if(data.action==="client_connect")get("sharing-remote-link").value="";if(data.action==="egress_select"){exitDraft=false;get("exit-peer-link").value="";}render(result);message(data.action==="client_connect"&&result.reload_required===false?"额度来源已更换，后续请求使用新来源。":success||"");}
    catch(error){message(error.message,true);}
    finally{busy=false;button.setAttribute("aria-busy","false");if(current)render(current);else button.disabled=false;}
  }
  const automaticBinding=()=>current?.automatic_address_supported?{}:{address:current?.addresses?.[0]};
  get("sharing-host-form").addEventListener("submit",event=>{event.preventDefault();action(get("sharing-start"),{action:"host_start",...automaticBinding(),hours:Number(get("sharing-hours").value),token_limit:Number(get("sharing-token-limit").value)},"已开启，可复制链接分享。");});
  get("sharing-stop").onclick=()=>action(get("sharing-stop"),{action:"host_stop"},"共享已停止，旧链接已失效。");
  get("sharing-rotate").onclick=()=>action(get("sharing-rotate"),{action:"host_rotate"},"已更换链接，旧链接已失效。");
  get("sharing-client-form").addEventListener("submit",event=>{event.preventDefault();action(get("sharing-connect"),{action:"client_connect",link:get("sharing-remote-link").value.trim()},"已连接。请重新加载 Codex 配置并新建会话；如未生效，请重启 ChatGPT 应用。");});
  get("sharing-disconnect").onclick=()=>action(get("sharing-disconnect"),{action:"client_disconnect"},"已恢复原连接配置。请重新加载 Codex 配置或重启 ChatGPT 应用。");
  get("sharing-copy").onclick=async()=>{try{await navigator.clipboard.writeText(get("sharing-link").value);message("共享链接已复制。");}catch(_){get("sharing-link").focus();get("sharing-link").select();message("请复制已选中的共享链接。");}};
  get("exit-host-form").addEventListener("submit",event=>{event.preventDefault();action(get("exit-start"),{action:"egress_start",...automaticBinding(),hours:Number(get("exit-hours").value)},"已开启出口共享。");});
  get("exit-stop").onclick=()=>{exitDraft=false;action(get("exit-stop"),{action:"egress_stop"},"出口共享已停止。依赖本机回接的选择已切回额度提供方出口。");};
  get("exit-rotate").onclick=()=>action(get("exit-rotate"),{action:"egress_rotate"},"出口链接已更换。旧链接不能新建通道，已有通道可完成。");
  get("exit-copy").onclick=async()=>{if(busy)return;try{if(!get("exit-link").value)render(await request({action:"egress_link"}));await navigator.clipboard.writeText(get("exit-link").value);message("出口链接已复制。");}catch(_){get("exit-link").focus();get("exit-link").select();message("请复制已选中的出口链接。");}};
  get("exit-source").addEventListener("change",()=>{exitDraft=true;get("exit-peer-fields").hidden=get("exit-source").value!=="peer";});
  get("exit-source-form").addEventListener("submit",event=>{event.preventDefault();action(get("exit-apply"),{action:"egress_select",mode:get("exit-source").value,link:get("exit-peer-link").value.trim()},"出口已更新，新请求使用所选出口。");});
  document.addEventListener("visibilitychange",()=>{if(document.hidden)controller?.abort();else if(active)refresh();});
  function setActive(value){active=value;clearInterval(timer);timer=null;if(!active){controller?.abort();return;}get("sharing-cards").hidden=false;message("");refresh();timer=setInterval(refresh,3000);}
  return {setActive};
})();
if(typeof workspaceView!=="undefined"&&workspaceView==="sharing")globalThis.InspectorSharing.setActive(true);
