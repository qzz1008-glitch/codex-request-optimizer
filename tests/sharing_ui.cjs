"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
class Element{
  constructor(){this.value="";this.hidden=false;this.disabled=false;this.attributes={};this.handlers={};this.children=[];this.classes=new Set();this.classList={toggle:(key,on)=>on?this.classes.add(key):this.classes.delete(key)};}
  get options(){return this.children;}
  setAttribute(key,value){this.attributes[key]=value;}
  addEventListener(key,value){this.handlers[key]=value;}
  replaceChildren(...children){this.children=children;if(!children.some(node=>node.value===this.value))this.value=children[0]?.value||"";}
  focus(){}
  select(){this.selected=true;}
}
const elements=new Map(),node=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
const idle={supported:true,protocol:3,automatic_address_supported:true,addresses:["192.168.1.20"],host:{active:false,running:false},client:{active:false},
  egress:{supported:true,host:{active:false},selection:{mode:"provider",peer_configured:false}}};
const host={...idle,host:{active:true,running:true,address:"192.168.1.20",port:47846,requests:1,in_flight:0,started_at:1900000000,expires_at:2000000000,
  token_limit:100000,usage:{input_tokens:1234,output_tokens:66,total_tokens:1300,cached_input_tokens:500,cached_responses:1,responses:1,missing_responses:0}}};
const pending=[],requests=[],stored=new Map();let copied="",timers=0;
const context={console,AbortController,Date,Number,Set,Map,
 document:{hidden:false,getElementById:node,createElement:()=>new Element(),addEventListener(){}},
 localStorage:{getItem:key=>stored.get(key),setItem:(key,value)=>stored.set(key,value)},
 navigator:{clipboard:{writeText:async value=>{copied=value;}}},
 setInterval:()=>++timers,clearInterval(){},t:value=>value,
 fetch:async(url,options)=>{requests.push({url,...options,data:options.body?JSON.parse(options.body):null});const response=await(pending.shift()||{status:200,data:idle});return {status:response.status,ok:response.status<400,json:async()=>response.data};}
};
context.globalThis=context;vm.createContext(context);
vm.runInContext(fs.readFileSync("packet_inspector/ui/sharing.js","utf8"),context);
const flush=async()=>{await new Promise(resolve=>setImmediate(resolve));await new Promise(resolve=>setImmediate(resolve));};
(async()=>{
  assert.equal(requests.length,0,"A hidden sharing tab must not start authorization or network calls");
  context.InspectorSharing.setActive(true);await flush();assert.equal(requests.length,1);
  node("sharing-port").value="50001";node("exit-port").value="50002";
  node("sharing-hours").value="8";node("sharing-token-limit").value="100000";
  let finish;pending.push(new Promise(resolve=>{finish=resolve;}));
  const submit=()=>node("sharing-host-form").handlers.submit({preventDefault(){}});submit();submit();
  assert.equal(node("sharing-start").attributes["aria-busy"],"true");assert.equal(node("sharing-start").disabled,true);
  assert.equal(requests.filter(value=>value.data?.action==="host_start").length,1,"Double click must not create multiple listeners");
  assert.equal(requests.find(value=>value.data?.action==="host_start").data.token_limit,100000);
  for(const key of ["address","port","request_limit"])assert.equal(key in requests.at(-1).data,false);
  finish({status:200,data:{...host,link:"codex-share://dummy-first-link"}});await flush();
  assert.equal(node("sharing-start").attributes["aria-busy"],"false");assert.equal(node("sharing-link-area").hidden,false);
  assert.equal(node("sharing-usage").hidden,false);assert.equal(node("sharing-total-tokens").textContent,Number(1300).toLocaleString());
  assert.equal(node("sharing-input-tokens").textContent,Number(1234).toLocaleString());assert.equal(node("sharing-cached-tokens").textContent,"500");
  assert.equal(node("sharing-token-limit").disabled,true);
  assert.equal(node("sharing-port").value,"50001","Sharing polling must not overwrite settings drafts");
  assert.equal(node("sharing-connect").disabled,true,"Hosting and consuming sharing cannot form a relay loop");
  assert.equal(node("exit-start").disabled,false,"Quota lending must not disable independent physical exit sharing");
  await node("sharing-copy").onclick();assert.equal(copied,"codex-share://dummy-first-link");
  pending.push({status:200,data:{...host,link:"codex-share://dummy-replaced-link"}});node("sharing-rotate").onclick();await flush();
  assert.equal(node("sharing-link").value,"codex-share://dummy-replaced-link");
  pending.push({status:200,data:idle});node("sharing-stop").onclick();await flush();assert.equal(node("sharing-link").value,"");
  node("sharing-remote-link").value="codex-share://dummy-peer-link";
  pending.push({status:200,data:{...idle,client:{active:true,address:"192.168.1.21",port:47846}}});
  node("sharing-client-form").handlers.submit({preventDefault(){}});await flush();
  assert.equal(node("sharing-remote-link").value,"");assert.equal(node("sharing-start").disabled,true);assert.equal(node("sharing-disconnect").hidden,false);
  assert.equal(node("sharing-connect").disabled,false,"Borrowing must allow replacing one quota source");
  assert.equal(node("exit-start").disabled,false,"Borrowing quota must still allow sharing this machine's physical exit");
  assert.match(node("sharing-message").textContent,/重新加载/);
  const borrowed={...idle,client:{active:true,address:"192.168.1.22",port:47846}};
  pending.push({status:200,data:{...borrowed,reload_required:false}});node("sharing-remote-link").value="codex-share://replacement";
  node("sharing-client-form").handlers.submit({preventDefault(){}});await flush();
  assert.match(node("sharing-message").textContent,/后续请求/);assert.doesNotMatch(node("sharing-message").textContent,/重新加载/);
  node("exit-source").value="peer";node("exit-source").handlers.change();node("exit-peer-link").value="codex-exit://private-exit-link";
  pending.push({status:200,data:borrowed});context.InspectorSharing.setActive(true);await flush();
  assert.equal(node("exit-source").value,"peer","Polling must preserve an unapplied exit selection");
  const withExit={...borrowed,egress:{...idle.egress,selection:{mode:"peer",peer_configured:true,address:"192.168.1.23",port:47847}}};
  pending.push({status:200,data:withExit});node("exit-source-form").handlers.submit({preventDefault(){}});await flush();
  assert.deepEqual(requests.at(-1).data,{action:"egress_select",mode:"peer",link:"codex-exit://private-exit-link"});
  assert.equal(node("exit-peer-link").value,"");assert.equal(node("exit-selection-state").textContent,"共享出口");
  const exitHost={...withExit,egress:{...withExit.egress,host:{active:true,address:"192.168.1.20",port:47847,connections:2,in_flight:1,sent_bytes:12000,received_bytes:4000}}};
  node("exit-hours").value="8";
  pending.push({status:200,data:{...exitHost,egress_link:"codex-exit://physical-only"}});node("exit-host-form").handlers.submit({preventDefault(){}});await flush();
  assert.equal(requests.at(-1).data.action,"egress_start");assert.equal(node("exit-start").hidden,true);
  for(const key of ["address","port"])assert.equal(key in requests.at(-1).data,false);
  assert.equal(node("exit-port").value,"50002");
  assert.equal(node("exit-source").value,"peer","Sharing a local exit must preserve the independently selected peer exit");
  await node("exit-copy").onclick();assert.equal(copied,"codex-exit://physical-only");
  pending.push({status:200,data:withExit});node("exit-stop").onclick();await flush();assert.equal(node("exit-link").value,"");
  pending.push({status:200,data:idle});node("sharing-disconnect").onclick();await flush();assert.equal(node("sharing-start").disabled,false);
  assert.equal([...stored.values()].some(value=>/codex-(share|exit):\/\//.test(String(value))),false,"Quota and exit grants must not enter localStorage");
  pending.push({status:200,data:{...idle,host:{...host.host,active:false,error:"Codex 登录已变更"}}});context.InspectorSharing.setActive(true);await flush();
  assert.equal(node("sharing-stop").hidden,false,"A paused listener must remain stoppable");assert.equal(node("sharing-rotate").disabled,true);
  pending.push({status:200,data:{...host,host:{...host.host,usage:{...host.host.usage,responses:0,missing_responses:1}}}});context.InspectorSharing.setActive(true);await flush();
  assert.equal(node("sharing-total-tokens").textContent,"—","Missing upstream usage must not display fake zero totals");
  assert.match(node("sharing-usage-coverage").textContent,/可能不完整/);
  pending.push({status:200,data:{...idle,automatic_address_supported:false}});context.InspectorSharing.setActive(true);await flush();
  pending.push({status:200,data:host});node("sharing-host-form").handlers.submit({preventDefault(){}});await flush();
  assert.equal(requests.at(-1).data.address,"192.168.1.20","Older backends must still receive an automatically selected address");
  pending.push({status:200,data:{...idle,protocol:1}});context.InspectorSharing.setActive(true);await flush();
  assert.equal(node("sharing-cards").hidden,true);assert.match(node("sharing-message").textContent,/独立额度与出口/);
  pending.push({status:404,data:{error:"missing"}});context.InspectorSharing.setActive(true);await flush();assert.match(node("sharing-message").textContent,/当前后台不支持共享/);

  let trendActive=null,sharingActive=null,reloaded=0;
  context.workspaceView="records";context.state={status:{},listController:{abort(){}}};
  context.$=node;context.tokenTrendView={setActive:value=>{trendActive=value;}};
  context.InspectorSharing={setActive:value=>{sharingActive=value;}};
  context.cancelTokenRead=()=>{};context.loadTokenWindow=()=>{};context.loadListTokens=()=>{};context.fail=()=>{};
  context.loadPackets=()=>{reloaded++;return Promise.resolve();};
  node("records-view").chosenSession="preserved";
  const app=fs.readFileSync("packet_inspector/ui/app.js","utf8"),start=app.indexOf("function setWorkspaceView(view)"),end=app.indexOf('$("view-records").onclick',start);
  vm.runInContext(app.slice(start,end),context);
  context.setWorkspaceView("sharing");assert.equal(node("sharing-view").hidden,false);assert.equal(node("records-view").hidden,true);assert.equal(trendActive,false);assert.equal(sharingActive,true);assert.equal(reloaded,0);
  context.setWorkspaceView("trends");assert.equal(trendActive,true);assert.equal(sharingActive,false);assert.equal(node("token-trend-view").hidden,false);
  context.setWorkspaceView("records");assert.equal(node("records-view").hidden,false);assert.equal(node("records-view").chosenSession,"preserved");assert.equal(reloaded,1);
  context.InspectorSharing=undefined;context.setWorkspaceView("sharing");assert.equal(node("sharing-cards").hidden,true);assert.match(node("sharing-message").textContent,/当前后台不支持共享/);
  context.settingsLanguage=null;context.uiI18n={language:"zh"};context.selectedImageAnalysisMode=()=>"native";
  const draftStart=app.indexOf("function readSettingsDraft()"),draftEnd=app.indexOf("function changedSettings(",draftStart);
  vm.runInContext(app.slice(draftStart,draftEnd),context);
  node("sharing-port").value="52000";node("exit-port").value="52001";
  const draft=context.readSettingsDraft();assert.equal(draft.sharing_port,"52000");assert.equal(draft.egress_port,"52001");
  const html=fs.readFileSync("packet_inspector/ui/index.html","utf8");
  const sharingHTML=html.slice(html.indexOf('<main id="sharing-view"')).split('</main>')[0];
  assert(!/id="(?:sharing-port|exit-port|sharing-address|exit-address|sharing-limit)"/.test(sharingHTML));
  const settingsHTML=html.slice(html.indexOf('<section id="sharing-settings"')).split('</section>')[0];
  for(const id of ["sharing-port","exit-port"])assert(settingsHTML.includes(`id="${id}"`));
  console.log(JSON.stringify({sharing_ui:"passed"}));
})().catch(error=>{console.error(error);process.exitCode=1;});
