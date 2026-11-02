"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
const source=fs.readFileSync("packet_inspector/ui/sharing.js","utf8");
class Element{
 constructor(){this.value="";this.hidden=false;this.disabled=false;this.dataset={};this.attributes={};this.handlers={};this.style={};this.innerHTML="";this.textContent="";this.classList={toggle(){},add(){},remove(){}};}
 setAttribute(key,value){this.attributes[key]=value;}
 addEventListener(type,handler){this.handlers[type]=handler;}
 querySelectorAll(){return [];}
 querySelector(){return null;}
}
const idle={protocol:4,credentials_supported:true,host:{running:false},client:{active:false},egress:{host:{active:false},selection:{mode:"local"}},credentials:[]};
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function ui(value,translate=value=>value,Clock=Date){
 let state=value,interval=null;
 const elements=new Map(),node=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
 const requests=[],context={console,AbortController,Date:Clock,URL,Number,Set,Map,innerWidth:1360,innerHeight:960,t:translate,
  document:{hidden:false,getElementById:node,addEventListener(){}},addEventListener(){},
  navigator:{clipboard:{writeText:async()=>{}}},setTimeout:()=>1,clearTimeout(){},
  setInterval:callback=>{interval=callback;return 1;},clearInterval:()=>{interval=null;},
  fetch:async(_url,options)=>{requests.push(options);return {status:200,ok:true,json:async()=>state};}};
 context.globalThis=context;vm.createContext(context);vm.runInContext(source,context);
 return {node,requests,
  open:async()=>{context.InspectorSharing.setActive(true);await tick();},
  close:()=>context.InspectorSharing.setActive(false),
  select:(kind,view)=>{const button=new Element();button.dataset={kind,view};node("sharing-cards").handlers.click({target:{closest:()=>button}});},
  poll:async value=>{state=value;await interval();},
  submit:async id=>node(id).handlers.submit({preventDefault(){}}),
  expect:(quota,exit)=>{for(const [kind,view] of [["quota",quota],["exit",exit]]){assert.equal(node(kind+"-issue").hidden,view!=="out",kind+" issue form");assert.equal(node(kind+"-borrow").hidden,view!=="in",kind+" borrow form");}}};
}
const row=(kind,direction,created_at,extra={})=>({id:kind+direction,kind,direction,created_at,label:kind+direction,...extra});
(async()=>{
 let clockNow=new Date(2026,10,2,12).getTime();
 class Clock extends Date{constructor(...args){super(...(args.length?args:[clockNow]));}static now(){return clockNow;}}
 const fresh=ui(idle,undefined,Clock);await fresh.open();fresh.expect("out","out");
 for(const kind of ["quota","exit"]){
  for(const suffix of ["-label","-borrow-label"]){
   assert.equal(fresh.node(kind+suffix).placeholder,suffix==="-label"?"凭据-20261102":"凭据");
   const markup=fresh.node(kind+"-config").innerHTML.match(new RegExp('<input id="'+kind+suffix+'"[^>]*>'))?.[0];
   assert.ok(markup);assert.doesNotMatch(markup,/\brequired\b/,"Blank names must pass native form validation");
  }
  if(kind==="quota")fresh.node("quota-budget").value="1";
  await fresh.submit(kind+"-issue");
  let sent=JSON.parse(fresh.requests.at(-1).body);
   assert.equal(sent.action,"credential_issue");assert.equal(sent.label,"凭据-20261102");
  fresh.node(kind+"-borrow-link").value=(kind==="quota"?"codex-share":"codex-exit")+"://10.0.0.8:47846/#secret-token.fingerprint";
  fresh.node(kind+"-borrow-link").handlers.input();
   assert.equal(fresh.node(kind+"-borrow-label").placeholder,"凭据-10.0.0.8");
  fresh.node(kind+"-borrow-label").value="   ";
  await fresh.submit(kind+"-borrow");sent=JSON.parse(fresh.requests.at(-1).body);
   assert.equal(sent.action,"credential_use");assert.equal(sent.label,"凭据-10.0.0.8");assert.doesNotMatch(sent.label,/secret-token|fingerprint/);assert.equal(fresh.node(kind+"-borrow-link").value,"");assert.equal(fresh.node(kind+"-borrow-label").placeholder,"凭据");
 }
  const namedState={...idle,credentials:[row("quota","out",1,{label:"凭据-20261102",enabled:false}),row("quota","in",2,{label:"  凭据-20261102-2  "}),row("quota","out",3,{id:"internal",label:"凭据-20261102-1",internal:true}),row("quota","in",4,{id:"ip",label:"凭据-10.0.0.8"}),row("exit","out",5,{label:"凭据-20261102-1"})]};
 const defaults=ui(namedState,undefined,Clock);await defaults.open();
 defaults.node("quota-borrow-link").value="codex-share://10.0.0.8:47846/#token.fingerprint";defaults.node("quota-borrow-link").handlers.input();
  assert.equal(defaults.node("quota-label").placeholder,"凭据-20261102-1");assert.equal(defaults.node("quota-borrow-label").placeholder,"凭据-10.0.0.8-1");assert.equal(defaults.node("exit-label").placeholder,"凭据-20261102","A used suffix does not require numbering an unused base name");
  defaults.node("quota-budget").value="1";await defaults.submit("quota-issue");assert.equal(JSON.parse(defaults.requests.at(-1).body).label,"凭据-20261102-1");
 const custom='Team A / 临时 <x> & "quoted"';
 defaults.node("quota-label").value="  "+custom+"  ";await defaults.submit("quota-issue");assert.equal(JSON.parse(defaults.requests.at(-1).body).label,custom);
 defaults.node("quota-borrow-label").value=custom;
 await defaults.submit("quota-borrow");assert.equal(JSON.parse(defaults.requests.at(-1).body).label,custom);
 defaults.node("quota-label").value="Unsent draft";
 defaults.node("quota-borrow-label").value="Unsent provider";defaults.node("quota-borrow-link").value="codex-share://10.0.0.8:47846/#token.fingerprint";
  await defaults.poll({...namedState,credentials:[...namedState.credentials,row("quota","out",6,{id:"new",label:"凭据-20261102-1"})]});
  assert.equal(defaults.node("quota-label").placeholder,"凭据-20261102-3");assert.equal(defaults.node("quota-borrow-label").placeholder,"凭据-10.0.0.8-1");assert.equal(defaults.node("quota-label").value,"Unsent draft","Refreshing default names preserves typed names");
  defaults.node("quota-borrow-link").value="codex-share://10.0.0.9:47846/#other-token.fingerprint";defaults.node("quota-borrow-link").handlers.input();assert.equal(defaults.node("quota-borrow-label").placeholder,"凭据-10.0.0.9");assert.equal(defaults.node("quota-borrow-label").value,"Unsent provider");
  defaults.node("quota-borrow-link").value="invalid credential";defaults.node("quota-borrow-link").handlers.input();assert.equal(defaults.node("quota-borrow-label").placeholder,"凭据");
 defaults.close();
  const english=ui({...idle,credentials:[row("quota","in",1,{label:"credential-20261102"})]},value=>value==="凭据"?"Credential":value,Clock);await english.open();
  assert.equal(english.node("quota-label").placeholder,"Credential-20261102-1");assert.equal(english.node("exit-borrow-label").placeholder,"Credential");english.close();
  const nextDay=ui(idle,undefined,Clock);await nextDay.open();clockNow=new Date(2026,10,3,12).getTime();await nextDay.poll(idle);assert.equal(nextDay.node("quota-label").placeholder,"凭据-20261103");nextDay.close();
 const remembered={...idle,last_used_views:{quota:"in",exit:"out"},client:{active:true,active_id:"quotain"},credentials:[row("quota","in",1),row("quota","out",20),row("exit","in",30)]};
 const current=ui(remembered);await current.open();current.expect("in","out");
 const requests=current.requests.length;current.select("quota","out");current.select("exit","in");current.expect("out","in");
 assert.equal(current.requests.length,requests,"Clicking tabs must not send preference or credential writes");
 await current.poll({...remembered,last_used_views:{quota:"in",exit:"out"}});current.expect("out","in");
 current.close();await current.open();current.expect("out","in");
 assert.ok(current.requests.every(request=>request.method==="GET"),"Reopening and polling must not count as credential operations");
 const reopened=ui(remembered);await reopened.open();reopened.expect("in","out");
 const latestOperation={...remembered,last_used_views:{quota:"out",exit:"in"}};
 const operated=ui(latestOperation);await operated.open();operated.expect("out","in");
 const active=ui({...idle,client:{active:true},egress:{selection:{mode:"peer"}},credentials:[row("quota","out",50),row("exit","out",50)]});
 await active.open();active.expect("in","in");
 const lending=ui({...idle,host:{running:true},egress:{host:{active:true}},credentials:[row("quota","in",50),row("exit","in",50)]});
 await lending.open();lending.expect("out","out");
 const legacy=ui({...idle,credentials:[row("quota","out",10,{last_seen:99999}),row("quota","in",20),row("exit","in",5),row("exit","out",30,{internal:true})]});
 await legacy.open();legacy.expect("in","in");
 const partial=ui({...idle,last_used_views:{quota:"out"},credentials:[row("quota","in",20),row("exit","in",30)]});
 await partial.open();partial.expect("out","in");
 const invalid=ui({...idle,last_used_views:{quota:"invalid",exit:{}},credentials:[row("quota","in",null),row("exit","in","not-a-date")]});
 await invalid.open();invalid.expect("out","out");
 const beforeData=ui({...idle,last_used_views:{quota:"out",exit:"out"}});
 beforeData.select("quota","in");await beforeData.open();beforeData.expect("in","out");
 const connectedAt=new Date(2026,10,2,13,7,59).getTime()/1000;
 const connectionState=(outTime,inTime)=>({...idle,last_used_views:{exit:"out"},credentials:[
  row("exit","out",1,{last_seen:outTime}),
  row("exit","in",2,{last_seen:connectedAt+86400,snapshot:{last_seen:inTime}})]});
 const connections=ui(connectionState(connectedAt,connectedAt-60));await connections.open();
 const connectedText=()=>connections.node("exit-credential-rows").innerHTML.match(/<td class="sharing-last-connected">([^<]+)<\/td>/)?.[1];
 assert.equal(connectedText(),"2026-11-02 13:07","Outgoing rows show the provider's latest authenticated connection, without seconds");
 connections.select("exit","in");
 assert.equal(connectedText(),"2026-11-02 13:06","Borrowed rows use the provider snapshot rather than the local snapshot-refresh time");
 await connections.poll(connectionState(connectedAt,connectedAt+120));
 assert.equal(connectedText(),"2026-11-02 13:09","New connection times refresh with the table");
 for(const missing of [undefined,null,"not-a-timestamp"]){
  await connections.poll(connectionState(connectedAt,missing));
  assert.equal(connectedText(),"—","Missing or invalid provider timestamps remain unknown");
 }
 connections.select("exit","out");assert.equal(connectedText(),"2026-11-02 13:07");
 connections.close();
 console.log(JSON.stringify({sharing_views:"passed",checks:["default","independent_resources","remembered_operations","same_window_state","no_tab_writes","poll_does_not_jump","cold_reopen","active_fallback","lending_fallback","legacy_creation_fallback","passive_stats_ignored","invalid_memory","early_manual_choice"]}));
})().catch(error=>{console.error(error);process.exitCode=1;});
