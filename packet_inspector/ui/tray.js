"use strict";
(() => {
  let visible=false,timer=null,controller=null,sequence=0,busy=false,stateSync=0;
  const buttons=[document.getElementById("tray-open"),document.getElementById("tray-exit")];
  const api=()=>window.pywebview?.api;
  const setText=(node,text)=>{if(node.textContent!==text)node.textContent=text;};
  function render(status={}) {
    if(status.language&&status.language!==uiI18n.language)uiI18n.setLanguage(status.language);
    const linked={connected:t("已接入"),disconnected:t("未接入")}[status.connection_state]||t("接入状态未知");
    const relay={running:t("中转正常"),switching:t("中转切换中")}[status.relay_state];
    setText(document.getElementById("tray-status"),relay?linked+" · "+relay:t("状态暂不可用"));
    document.getElementById("tray-dot").dataset.state=relay?status.connection_state:"unavailable";
    const numbers=new Intl.NumberFormat(uiI18n.language==="en"?"en-US":"zh-CN",{maximumFractionDigits:0});
    for(const node of document.querySelectorAll("[data-stat]")) {
      const key=node.dataset.stat,value=status[key],known=key==="average_compact_input_tokens"
        ?Number.isSafeInteger(status.compaction_samples)&&status.compaction_samples>0&&Number.isFinite(value)&&value>=0
        :Number.isSafeInteger(value)&&value>=0;
      setText(node,known?numbers.format(value):"—");
    }
    const range=document.getElementById("tray-record-range"),first=status.first_recorded_at,last=status.last_recorded_at;
    if(Number.isFinite(first)&&Number.isFinite(last)&&first>=0&&last>=first&&last<=8.64e12) {
      const start=new Date(first*1000),end=new Date(last*1000),pad=value=>String(value).padStart(2,"0");
      const short=date=>pad(date.getMonth()+1)+"/"+pad(date.getDate())+" "+pad(date.getHours())+":"+pad(date.getMinutes());
      setText(range,short(start)+(first===last?"":" – "+short(end)));
      range.title=t("记录时间范围")+": "+start.toLocaleString()+" – "+end.toLocaleString();
    } else {setText(range,"—");range.title=t("记录时间范围");}
    measure();
  }
  function measure() {
    const bounds=document.body.getBoundingClientRect();
    if(api()?.resize_panel)api().resize_panel(Math.ceil(bounds.width),Math.ceil(bounds.height)).catch(()=>{});
  }
  function cancel() {clearTimeout(timer);timer=null;sequence++;controller?.abort();controller=null;}
  async function refresh() {
    if(!visible||document.hidden)return;
    const request=++sequence,current=new AbortController();controller=current;const timeout=setTimeout(()=>current.abort(),2500);
    try {
      const response=await fetch("/api/tray-status",{cache:"no-store",signal:current.signal});
      if(!response.ok)throw Error("status unavailable");
      const status=await response.json();if(visible&&request===sequence)render(status);
    } catch(_) {if(visible&&request===sequence)render({language:uiI18n.language});}
    finally {clearTimeout(timeout);if(request===sequence){controller=null;if(visible&&!document.hidden)timer=setTimeout(refresh,1000);}}
  }
  function setVisible(value) {cancel();visible=!!value;if(visible)timer=setTimeout(refresh,0);}
  async function sync() {
    const request=++stateSync;
    try {const state=await api().panel_state();if(request===stateSync)setVisible(state.visible);} catch(_) {}
  }
  async function action(name) {
    if(busy||!api()?.[name])return;
    busy=true;for(const button of buttons)button.disabled=true;
    const error=document.getElementById("tray-error");error.hidden=true;
    try {const result=await api()[name]();if(result?.ok===false)throw Error(result.error);}
    catch(_) {setText(error,t("无法完成操作，请稍后重试。"));error.hidden=false;measure();}
    finally {busy=false;for(const button of buttons)button.disabled=false;}
  }
  buttons[0].addEventListener("click",()=>action("open_main"));buttons[1].addEventListener("click",()=>action("exit_app"));
  document.addEventListener("keydown",event=>{
    if(event.key==="Escape"){event.preventDefault();api()?.dismiss();}
    if(event.key==="ArrowDown"||event.key==="ArrowUp"){
      event.preventDefault();const current=buttons.indexOf(document.activeElement),delta=event.key==="ArrowDown"?1:-1;
      buttons[(current+delta+buttons.length)%buttons.length].focus();
    }
  });
  document.addEventListener("contextmenu",event=>event.preventDefault());
  document.addEventListener("visibilitychange",()=>{cancel();if(visible&&!document.hidden)refresh();});
  window.addEventListener("beforeunload",cancel);
  window.addEventListener("pywebviewready",async()=>{measure();try{await api().panel_ready();sync();}catch(_) {}});
  if(typeof ResizeObserver!=="undefined")new ResizeObserver(measure).observe(document.body);
  window.trayPanel={setVisible,render,sync};
})();
