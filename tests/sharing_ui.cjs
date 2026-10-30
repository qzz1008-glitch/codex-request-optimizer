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
const idle={supported:true,protocol:2,addresses:["192.168.1.20"],host:{active:false,running:false},client:{active:false}};
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
  context.InspectorSharing.setActive(true);await flush();assert.equal(requests.length,1);assert.equal(node("sharing-address").value,"192.168.1.20");
  node("sharing-port").value="47846";node("sharing-hours").value="8";node("sharing-limit").value="0";node("sharing-token-limit").value="100000";
  let finish;pending.push(new Promise(resolve=>{finish=resolve;}));
  const submit=()=>node("sharing-host-form").handlers.submit({preventDefault(){}});submit();submit();
  assert.equal(node("sharing-start").attributes["aria-busy"],"true");assert.equal(node("sharing-start").disabled,true);
  assert.equal(requests.filter(value=>value.data?.action==="host_start").length,1,"Double click must not create multiple listeners");
  assert.equal(requests.find(value=>value.data?.action==="host_start").data.token_limit,100000);
  finish({status:200,data:{...host,link:"codex-share://dummy-first-link"}});await flush();
  assert.equal(node("sharing-start").attributes["aria-busy"],"false");assert.equal(node("sharing-link-area").hidden,false);
  assert.equal(node("sharing-usage").hidden,false);assert.equal(node("sharing-total-tokens").textContent,Number(1300).toLocaleString());
  assert.equal(node("sharing-input-tokens").textContent,Number(1234).toLocaleString());assert.equal(node("sharing-cached-tokens").textContent,"500");
  assert.equal(node("sharing-token-limit").disabled,true);
  assert.equal(node("sharing-connect").disabled,true,"Hosting and consuming sharing cannot form a relay loop");
  await node("sharing-copy").onclick();assert.equal(copied,"codex-share://dummy-first-link");
  pending.push({status:200,data:{...host,link:"codex-share://dummy-replaced-link"}});node("sharing-rotate").onclick();await flush();
  assert.equal(node("sharing-link").value,"codex-share://dummy-replaced-link");
  pending.push({status:200,data:idle});node("sharing-stop").onclick();await flush();assert.equal(node("sharing-link").value,"");
  node("sharing-remote-link").value="codex-share://dummy-peer-link";
  pending.push({status:200,data:{...idle,client:{active:true,address:"192.168.1.21",port:47846}}});
  node("sharing-client-form").handlers.submit({preventDefault(){}});await flush();
  assert.equal(node("sharing-remote-link").value,"");assert.equal(node("sharing-start").disabled,true);assert.equal(node("sharing-disconnect").hidden,false);
  assert.match(node("sharing-message").textContent,/重新加载/);
  pending.push({status:200,data:idle});node("sharing-disconnect").onclick();await flush();assert.equal(node("sharing-start").disabled,false);
  assert.equal([...stored.values()].some(value=>String(value).includes("codex-share://")),false,"Grants must not enter localStorage");
  pending.push({status:200,data:{...idle,host:{...host.host,active:false,error:"Codex 登录已变更"}}});context.InspectorSharing.setActive(true);await flush();
  assert.equal(node("sharing-stop").hidden,false,"A paused listener must remain stoppable");assert.equal(node("sharing-rotate").disabled,true);
  pending.push({status:200,data:{...host,host:{...host.host,usage:{...host.host.usage,responses:0,missing_responses:1}}}});context.InspectorSharing.setActive(true);await flush();
  assert.equal(node("sharing-total-tokens").textContent,"—","Missing upstream usage must not display fake zero totals");
  assert.match(node("sharing-usage-coverage").textContent,/可能不完整/);
  pending.push({status:200,data:{...idle,protocol:1}});context.InspectorSharing.setActive(true);await flush();
  assert.equal(node("sharing-cards").hidden,true);assert.match(node("sharing-message").textContent,/隐私隔离/);
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
  console.log(JSON.stringify({sharing_ui:"passed"}));
})().catch(error=>{console.error(error);process.exitCode=1;});
