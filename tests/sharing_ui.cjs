"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
class Element{
 constructor(){this.value="";this.hidden=false;this.disabled=false;this.checked=false;this.attributes={};this.handlers={};this.innerHTML="";this.textContent="";this.dataset={};this.classList={toggle(){}};}
 setAttribute(k,v){this.attributes[k]=v;} addEventListener(k,v){this.handlers[k]=v;} querySelectorAll(){return [];}
 focus(){} select(){this.selected=true;} showModal(){this.open=true;} close(){this.open=false;}
}
const elements=new Map(),node=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
const idle={supported:true,protocol:4,credentials_supported:true,host:{active:false,running:false},client:{active:false},egress:{supported:true,host:{active:false},selection:{mode:"provider"}},credentials:[]};
const outgoing={id:"q1",kind:"quota",direction:"out",label:"Colleague <name>",enabled:true,expires_at:null,budget_nanos:10e9,period_cost_nanos:2e9,week_cost_nanos:3e9,weekly_reset:true,totals:{cost_nanos:4e9,input_tokens:1234,output_tokens:66,cached_input_tokens:500,missing_responses:1,unpriced_responses:2},daily:{}};
const incoming={id:"q2",kind:"quota",direction:"in",label:"Saved provider",snapshot:{...outgoing,expires_at:Date.now()/1000+86400}};
const exitIncoming={id:"e1",kind:"exit",direction:"in",label:"Exit provider",snapshot:{expires_at:null,enabled:true,totals:{sent_bytes:30000,received_bytes:20000,requests:2},daily:{}}};
const host={...idle,host:{active:true,running:true},credentials:[outgoing,incoming,exitIncoming]};
const requests=[],pending=[];let copied="",interval=null;
const context={console,AbortController,Date,Number,Set,Map,document:{hidden:false,getElementById:node,addEventListener(){}},navigator:{clipboard:{writeText:async value=>{copied=value;}}},setInterval:fn=>{interval=fn;return 1;},clearInterval:()=>{interval=null;},t:x=>x,
 fetch:async(url,options)=>{requests.push({url,data:options.body?JSON.parse(options.body):null});const r=await(pending.shift()||{status:200,data:idle});return {status:r.status,ok:r.status<400,json:async()=>r.data};}};
context.globalThis=context;vm.createContext(context);vm.runInContext(fs.readFileSync("packet_inspector/ui/sharing.js","utf8"),context);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const click=(target,dataset)=>{const button=new Element();button.dataset=dataset;return node(target).handlers.click({target:{closest:()=>button}});};
const submit=id=>node(id).handlers.submit({preventDefault(){}});
(async()=>{
 context.InspectorSharing.setActive(true);await flush();assert.equal(node("sharing-cards").hidden,false);
 assert.match(node("sharing-config").innerHTML,/quota-issue/);assert.match(node("sharing-config").innerHTML,/exit-issue/);assert.doesNotMatch(node("sharing-config").innerHTML,/sharing-port|egress-port|Token 上限|binding-note|一台机器/);
 node("quota-label").value="Alice";node("quota-budget").value="10";node("quota-weekly").checked=true;
 let release;pending.push(new Promise(resolve=>{release=resolve;}));submit("quota-issue");submit("quota-issue");
 assert.equal(requests.filter(r=>r.data?.action==="credential_issue").length,1);assert.equal(node("quota-generate").attributes["aria-busy"],"true");
 const created=requests.at(-1).data;assert.equal(created.budget,"10");assert.equal(created.weekly_reset,true);assert.ok(created.expires_at>Date.now()/1000+6*86400);assert.equal("port" in created,false);assert.equal("token_limit" in created,false);assert.match(node("sharing-config").innerHTML,/<span id="quota-budget-label">额度<\/span>/);assert.match(node("sharing-config").innerHTML,/<span id="quota-budget-unit"[^>]*>美元<\/span>/);
 release({status:200,data:{...host,link:"codex-share://dummy-secret"}});await flush();assert.equal(node("sharing-link-area").hidden,false);await node("sharing-copy").onclick();assert.equal(copied,"codex-share://dummy-secret");
 assert.match(node("sharing-credential-rows").innerHTML,/Colleague &lt;name&gt;/);assert.doesNotMatch(node("sharing-credential-rows").innerHTML,/Colleague <name>/);assert.match(node("sharing-summary").innerHTML,/\$2\.00/);assert.match(node("sharing-stat-note").textContent,/用量缺失 1 · 未计价 2/);
 click("sharing-config",{kind:"quota",view:"in"});assert.equal(node("quota-issue").hidden,true);assert.equal(node("quota-borrow").hidden,false);assert.equal(node("quota-activate").disabled,true);assert.equal(node("quota-service").hidden,false,"Pause remains accessible when quota lending blocks borrowing");
 assert.match(node("sharing-credential-rows").innerHTML,/Saved provider/);assert.doesNotMatch(node("sharing-credential-rows").innerHTML,/data-action="edit"/);
 pending.push({status:200,data:{...host,host:{active:false,running:false}}});click("sharing-config",{kind:"quota",action:"service"});await flush();assert.equal(requests.at(-1).data.action,"host_stop");assert.equal(node("quota-activate").disabled,false);
 const borrowing={...host,host:{active:false,running:false},client:{active:true,active_id:"q2"}};
 pending.push({status:200,data:{...borrowing,reload_required:true}});await click("sharing-credential-rows",{action:"use",id:"q2"});await flush();assert.equal(requests.at(-1).data.id,"q2");assert.equal(requests.at(-1).data.action,"credential_use");assert.match(node("sharing-message").textContent,/重启/);assert.equal(node("quota-generate").disabled,true);
 click("sharing-config",{kind:"exit",view:"in"});assert.match(node("sharing-stat-title").textContent,/出口/);assert.match(node("sharing-credential-rows").innerHTML,/Exit provider/);assert.equal(node("exit-activate").disabled,false,"Exit and quota sources stay independent");
 node("exit-borrow-link").value="codex-exit://private-value";node("exit-borrow-label").value="C";pending.push({status:200,data:borrowing});await submit("exit-borrow");assert.equal(requests.at(-1).data.kind,"exit");assert.equal(node("exit-borrow-link").value,"");
 node("quota-label").value="draft";pending.push({status:200,data:host});await interval();assert.equal(node("quota-label").value,"draft","Polling preserves form drafts");
 click("sharing-config",{kind:"quota",view:"out"});click("sharing-credential-rows",{action:"edit",id:"q1"});assert.equal(node("sharing-credential-dialog").open,true);assert.equal(node("credential-edit-budget").value,10);assert.equal(node("credential-edit-expiry").value,"");
 node("credential-edit-budget").value="25";pending.push({status:200,data:host});await submit("credential-edit-form");assert.equal(requests.at(-1).data.budget,"25");assert.equal(requests.at(-1).data.expires_at,null);assert.equal(node("sharing-credential-dialog").open,false);
 pending.push({status:200,data:host});await click("sharing-credential-rows",{action:"reset",id:"q1"});assert.equal(requests.at(-1).data.action,"credential_reset");
 const before=requests.length;pending.push({status:400,data:{error:"此凭据的本期额度已用完。"}});await click("sharing-credential-rows",{action:"use",id:"q2"});assert.equal(requests.length,before+1);assert.match(node("sharing-message").textContent,/额度已用完/);
 context.InspectorSharing.setActive(false);assert.equal(node("sharing-link").value,"");assert.equal(node("sharing-link-area").hidden,true);assert.equal(interval,null);
 pending.push({status:200,data:{...idle,protocol:3,credentials_supported:false}});context.InspectorSharing.setActive(true);await flush();assert.equal(node("sharing-cards").hidden,true);assert.match(node("sharing-message").textContent,/更新软件/);context.InspectorSharing.setActive(false);
 assert.doesNotMatch(fs.readFileSync("packet_inspector/ui/sharing.js","utf8"),/localStorage|sessionStorage/);
 console.log(JSON.stringify({sharing_ui:"passed",checks:["two_resource_cards","reusable_credentials","history_selection","expiry_and_weekly_reset","live_statistics","privacy","old_backend_guard"]}));
})().catch(error=>{console.error(error);process.exitCode=1;});
