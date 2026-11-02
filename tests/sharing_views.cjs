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
function ui(value){
 let state=value,interval=null;
 const elements=new Map(),node=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
 const requests=[],context={console,AbortController,Date,Number,Set,Map,innerWidth:1360,innerHeight:960,t:value=>value,
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
  expect:(quota,exit)=>{for(const [kind,view] of [["quota",quota],["exit",exit]]){assert.equal(node(kind+"-issue").hidden,view!=="out",kind+" issue form");assert.equal(node(kind+"-borrow").hidden,view!=="in",kind+" borrow form");}}};
}
const row=(kind,direction,created_at,extra={})=>({id:kind+direction,kind,direction,created_at,label:kind+direction,...extra});
(async()=>{
 const fresh=ui(idle);await fresh.open();fresh.expect("out","out");
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
 console.log(JSON.stringify({sharing_views:"passed",checks:["default","independent_resources","remembered_operations","same_window_state","no_tab_writes","poll_does_not_jump","cold_reopen","active_fallback","lending_fallback","legacy_creation_fallback","passive_stats_ignored","invalid_memory","early_manual_choice"]}));
})().catch(error=>{console.error(error);process.exitCode=1;});
