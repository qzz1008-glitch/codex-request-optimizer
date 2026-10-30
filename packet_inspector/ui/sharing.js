"use strict";

globalThis.InspectorSharing=(()=>{
  const get=id=>document.getElementById(id),translate=value=>typeof t==="function"?t(value):value;
  let active=false,busy=false,current=null,timer=null,controller=null,recoveryShown=false;
  const message=(text,error=false)=>{const box=get("sharing-message");box.textContent=translate(text);box.hidden=!text;box.classList.toggle("error",error);};
  const text=(id,value)=>{get(id).textContent=translate(value);};
  async function request(data,signal){
    const response=await fetch("/api/sharing",{method:data?"POST":"GET",cache:"no-store",headers:data?{"Content-Type":"application/json"}:{},body:data?JSON.stringify(data):undefined,signal});
    if(response.status===404){const error=new Error(translate("当前后台不支持共享，请更新软件后使用。"));error.unsupported=true;throw error;}
    const result=await response.json();if(!response.ok)throw new Error(translate(result.error||"共享操作失败。"));return result;
  }
  function render(value){
    if(!(value.protocol>=2)){current=null;get("sharing-cards").hidden=true;message("当前后台不支持共享用量与隐私隔离，请更新软件。",true);return;}
    get("sharing-cards").hidden=false;
    current=value;const host=value.host||{},client=value.client||{},hosting=host.running===true||host.active===true,using=client.active===true;
    const address=get("sharing-address"),selected=address.value;
    const addresses=value.addresses||[];
    if([...address.options].map(option=>option.value).join()!==addresses.join()){
      address.replaceChildren(...addresses.map(value=>{const option=document.createElement("option");option.value=value;option.textContent=value;return option;}));
      if(addresses.includes(selected))address.value=selected;
    }
    if(hosting){address.value=host.address;get("sharing-port").value=host.port;}
    text("sharing-host-state",host.active?"共享中":hosting?"已暂停":"未开启");get("sharing-host-state").classList.toggle("active",host.active===true);
    text("sharing-client-state",using?"使用中":"未连接");get("sharing-client-state").classList.toggle("active",using);
    for(const id of ["sharing-address","sharing-port","sharing-hours","sharing-limit","sharing-token-limit"])get(id).disabled=busy||hosting||using;
    if(hosting&&host.token_limit!==undefined)get("sharing-token-limit").value=host.token_limit;
    get("sharing-start").hidden=hosting;get("sharing-start").disabled=busy||using||!addresses.length;
    for(const id of ["sharing-stop","sharing-rotate"]){get(id).hidden=!hosting;get(id).disabled=busy;}
    get("sharing-rotate").disabled=busy||!host.active;
    get("sharing-connect").hidden=using;get("sharing-connect").disabled=busy||hosting;
    get("sharing-disconnect").hidden=!using;get("sharing-disconnect").disabled=busy;
    get("sharing-remote-link").disabled=busy||using||hosting;
    if(value.link)get("sharing-link").value=value.link;
    if(!host.active)get("sharing-link").value="";
    get("sharing-link-area").hidden=!get("sharing-link").value;
    get("sharing-copy").disabled=busy;
    get("sharing-host-detail").textContent=host.error?translate(host.error):hosting?`${host.address}:${host.port} · ${translate("已转发")} ${host.requests||0}${host.request_limit?" / "+host.request_limit:""} · ${translate("进行中")} ${host.in_flight||0} · ${translate("到期")} ${new Date(host.expires_at*1000).toLocaleString()}`:"";
    get("sharing-client-detail").textContent=using?`${translate("提供方")} ${client.address}:${client.port}`:"";
    const usage=host.usage,format=value=>Number(value||0).toLocaleString();
    get("sharing-usage").hidden=!usage||!host.started_at;
    if(usage){
      const unknown=!usage.responses&&usage.missing_responses>0;
      text("sharing-total-tokens",unknown?"—":format(usage.total_tokens));
      text("sharing-input-tokens",unknown?"—":format(usage.input_tokens));
      text("sharing-output-tokens",unknown?"—":format(usage.output_tokens));
      text("sharing-cached-tokens",usage.cached_responses?format(usage.cached_input_tokens):"—");
      text("sharing-token-budget",host.token_limit?`${translate("上限")} ${format(host.token_limit)}`:"");
      const coverage=`${translate("已有用量")} ${format(usage.responses)} · ${translate("未取得用量")} ${format(usage.missing_responses)}`;
      text("sharing-usage-coverage",`${coverage} · ${translate(usage.missing_responses?"累计值可能不完整；缓存包含在输入内。":"含重复上下文；缓存包含在输入内。")}`);
    }
    if(value.recovery_error)message(value.recovery_error,true);
    else if(value.recovered&&!recoveryShown){message("上次共享已结束，已恢复原连接配置。请重新加载 Codex 配置或重启 ChatGPT 应用。");recoveryShown=true;}
  }
  async function refresh(){
    if(!active||busy||document.hidden)return;
    controller?.abort();const pending=new AbortController();controller=pending;
    try{render(await request(null,pending.signal));}catch(error){if(error.name!=="AbortError"){if(error.unsupported)get("sharing-cards").hidden=true;message(error.message,true);}}finally{if(controller===pending)controller=null;}
  }
  async function action(button,data,success){
    if(busy)return;busy=true;controller?.abort();button.setAttribute("aria-busy","true");if(current)render(current);else button.disabled=true;message("");
    try{const result=await request(data);if(data.action==="client_connect")get("sharing-remote-link").value="";render(result);message(success||"");}
    catch(error){message(error.message,true);}
    finally{busy=false;button.setAttribute("aria-busy","false");if(current)render(current);else button.disabled=false;}
  }
  get("sharing-host-form").addEventListener("submit",event=>{event.preventDefault();action(get("sharing-start"),{action:"host_start",address:get("sharing-address").value,port:Number(get("sharing-port").value),hours:Number(get("sharing-hours").value),request_limit:Number(get("sharing-limit").value),token_limit:Number(get("sharing-token-limit").value)},"共享已开启。将链接发给同事即可连接。");});
  get("sharing-stop").onclick=()=>action(get("sharing-stop"),{action:"host_stop"},"共享已停止，旧链接已失效。");
  get("sharing-rotate").onclick=()=>action(get("sharing-rotate"),{action:"host_rotate"},"已更换链接，旧链接已失效。");
  get("sharing-client-form").addEventListener("submit",event=>{event.preventDefault();action(get("sharing-connect"),{action:"client_connect",link:get("sharing-remote-link").value.trim()},"已连接。请重新加载 Codex 配置并新建会话；如未生效，请重启 ChatGPT 应用。");});
  get("sharing-disconnect").onclick=()=>action(get("sharing-disconnect"),{action:"client_disconnect"},"已恢复原连接配置。请重新加载 Codex 配置或重启 ChatGPT 应用。");
  get("sharing-copy").onclick=async()=>{try{await navigator.clipboard.writeText(get("sharing-link").value);message("共享链接已复制。");}catch(_){get("sharing-link").focus();get("sharing-link").select();message("请复制已选中的共享链接。");}};
  document.addEventListener("visibilitychange",()=>{if(document.hidden)controller?.abort();else if(active)refresh();});
  function setActive(value){active=value;clearInterval(timer);timer=null;if(!active){controller?.abort();return;}get("sharing-cards").hidden=false;message("");refresh();timer=setInterval(refresh,3000);}
  return {setActive};
})();
if(typeof workspaceView!=="undefined"&&workspaceView==="sharing")globalThis.InspectorSharing.setActive(true);
