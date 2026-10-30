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
const incoming={id:"q2",kind:"quota",direction:"in",label:"Saved provider",snapshot:{...outgoing,weekly_reset:false,expires_at:Date.now()/1000+86400}};
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
 const initialRequests=requests.length;node("quota-billing-help").onclick();assert.equal(node("sharing-billing-dialog").open,true);node("sharing-billing-close").onclick();assert.equal(node("sharing-billing-dialog").open,false);assert.equal(requests.length,initialRequests);
 node("quota-label").value="draft";node("quota-budget").value="0.99";await submit("quota-issue");assert.equal(requests.length,initialRequests);assert.match(node("sharing-message").textContent,/1 到 1,000,000/);node("quota-budget").value="1";node("quota-expiry-field").dataset.mode="custom";node("quota-expiry").value=new Date(Date.now()+3600000).toISOString();await submit("quota-issue");assert.equal(requests.length,initialRequests);assert.match(node("sharing-message").textContent,/至少为一天/);node("quota-expiry-field").dataset.mode="week";
 assert.match(node("sharing-config").innerHTML,/quota-issue/);assert.match(node("sharing-config").innerHTML,/exit-issue/);assert.doesNotMatch(node("sharing-config").innerHTML,/sharing-port|egress-port|Token 上限|binding-note|一张凭据仅供|sharing-service|暂停借出|恢复借出/);assert.match(node("sharing-config").innerHTML,/每张出口凭据仅供一台机器使用/);
 node("quota-label").value="Alice";node("quota-budget").value="10";node("quota-weekly").checked=true;node("exit-label").value="Exit draft";node("exit-expiry-field").dataset.mode="none";node("exit-expiry").value="";
 let release;pending.push(new Promise(resolve=>{release=resolve;}));submit("quota-issue");submit("quota-issue");
 assert.equal(requests.filter(r=>r.data?.action==="credential_issue").length,1);assert.equal(node("quota-generate").attributes["aria-busy"],"true");
 const created=requests.at(-1).data;assert.equal(created.budget,"10");assert.equal(created.weekly_reset,true);assert.ok(created.expires_at>Date.now()/1000+6*86400);assert.equal("port" in created,false);assert.equal("token_limit" in created,false);assert.match(node("sharing-config").innerHTML,/<span id="quota-budget-label">额度<\/span>/);assert.match(node("sharing-config").innerHTML,/<span id="quota-budget-unit"[^>]*>美元<\/span>/);
 release({status:200,data:{...host,link:"codex-share://dummy-secret"}});await flush();assert.equal(node("sharing-link-area").hidden,false);assert.equal(node("sharing-message").hidden,true);await node("sharing-copy").onclick();assert.equal(copied,"codex-share://dummy-secret");
 assert.equal(node("quota-label").value,"");assert.equal(node("quota-budget").value,"1");assert.equal(node("quota-weekly").checked,false);assert.equal(node("quota-expiry-field").dataset.mode,"week");assert.equal(node("quota-expiry-week").attributes["aria-pressed"],"true");assert.equal(node("exit-label").value,"Exit draft");assert.equal(node("exit-expiry-field").dataset.mode,"none");
 pending.push({status:400,data:{error:"同类型凭据名称已存在，请使用其他名称。"}});await submit("exit-issue");assert.match(node("sharing-message").textContent,/同类型凭据名称已存在/);assert.equal(node("exit-label").value,"Exit draft");assert.equal(node("exit-expiry-field").dataset.mode,"none");assert.equal(node("sharing-link").value,"codex-share://dummy-secret");
 pending.push({status:200,data:{...host,link:"codex-exit://dummy-exit-secret"}});await submit("exit-issue");assert.equal(node("exit-label").value,"");assert.equal(node("exit-expiry-field").dataset.mode,"week");assert.equal(node("exit-expiry-week").attributes["aria-pressed"],"true");assert.equal(node("sharing-link").value,"codex-exit://dummy-exit-secret");click("sharing-config",{kind:"quota",view:"out"});
 assert.match(node("sharing-credential-rows").innerHTML,/Colleague &lt;name&gt;/);assert.doesNotMatch(node("sharing-credential-rows").innerHTML,/Colleague <name>/);assert.match(node("sharing-summary").innerHTML,/\$2\.00/);assert.match(node("sharing-stat-note").textContent,/用量缺失 1 · 未计价 2/);
 assert.match(node("sharing-table-head").innerHTML,/>每周重置<\/th>/);assert.match(node("sharing-credential-rows").innerHTML,/class="sharing-weekly-reset">是<\/td>/);
 assert.match(node("sharing-table-head").innerHTML,/>凭据类型<\/th>/);assert.match(node("sharing-credential-rows").innerHTML,/class="sharing-credential-type">额度 · 借出<\/td>/);
 click("sharing-config",{kind:"quota",view:"in"});assert.equal(node("quota-issue").hidden,true);assert.equal(node("quota-borrow").hidden,false);assert.equal(node("quota-activate").disabled,true);
 assert.match(node("sharing-credential-rows").innerHTML,/Saved provider/);assert.doesNotMatch(node("sharing-credential-rows").innerHTML,/data-action="edit"/);
 assert.match(node("sharing-credential-rows").innerHTML,/class="sharing-weekly-reset">否<\/td>/);
 assert.match(node("sharing-credential-rows").innerHTML,/class="sharing-credential-type">额度 · 借入<\/td>/);
 pending.push({status:200,data:{...host,credentials:[outgoing,{...incoming,snapshot:{...incoming.snapshot,weekly_reset:undefined}},exitIncoming]}});await interval();assert.match(node("sharing-credential-rows").innerHTML,/class="sharing-weekly-reset">—<\/td>/);
 click("sharing-config",{kind:"quota",view:"out"});pending.push({status:200,data:{...host,host:{active:false,running:false},credentials:[{...outgoing,enabled:false},incoming,exitIncoming]}});await click("sharing-credential-rows",{action:"toggle",id:"q1"});assert.equal(requests.at(-1).data.action,"credential_edit");assert.equal(requests.at(-1).data.id,"q1");assert.equal(requests.at(-1).data.enabled,false);click("sharing-config",{kind:"quota",view:"in"});assert.equal(node("quota-activate").disabled,false);
 const borrowing={...host,host:{active:false,running:false},client:{active:true,active_id:"q2"}};
 pending.push({status:200,data:{...borrowing,reload_required:true}});await click("sharing-credential-rows",{action:"use",id:"q2"});await flush();assert.equal(requests.at(-1).data.id,"q2");assert.equal(requests.at(-1).data.action,"credential_use");assert.match(node("sharing-message").textContent,/重启/);assert.equal(node("quota-generate").disabled,true);
 click("sharing-config",{kind:"exit",view:"in"});assert.match(node("sharing-stat-title").textContent,/出口/);assert.match(node("sharing-credential-rows").innerHTML,/Exit provider/);assert.equal(node("exit-activate").disabled,false,"Exit and quota sources stay independent");
 assert.doesNotMatch(node("sharing-table-head").innerHTML,/每周重置/);
 assert.match(node("sharing-credential-rows").innerHTML,/class="sharing-credential-type">出口 · 借入<\/td>/);
 node("exit-borrow-link").value="codex-exit://private-value";node("exit-borrow-label").value="C";pending.push({status:200,data:borrowing});await submit("exit-borrow");assert.equal(requests.at(-1).data.kind,"exit");assert.equal(node("exit-borrow-link").value,"");
 node("quota-label").value="draft";pending.push({status:200,data:host});await interval();assert.equal(node("quota-label").value,"draft","Polling preserves form drafts");
 click("sharing-config",{kind:"quota",view:"out"});click("sharing-credential-rows",{action:"edit",id:"q1"});assert.equal(node("sharing-credential-dialog").open,true);assert.equal(node("credential-edit-budget").value,10);assert.equal(node("credential-edit-expiry").value,"");
 node("credential-edit-budget").value="25";pending.push({status:200,data:host});await submit("credential-edit-form");assert.equal(requests.at(-1).data.budget,"25");assert.equal(requests.at(-1).data.expires_at,null);assert.equal(node("sharing-credential-dialog").open,false);
 pending.push({status:200,data:host});await click("sharing-credential-rows",{action:"reset",id:"q1"});assert.equal(requests.at(-1).data.action,"credential_reset");
 pending.push({status:200,data:{...host,host:{active:false,running:false}}});await interval();assert.match(node("sharing-credential-rows").innerHTML,/data-action="toggle"[^>]*>启用/);pending.push({status:200,data:host});await click("sharing-credential-rows",{action:"toggle",id:"q1"});assert.equal(requests.at(-1).data.enabled,true,"Enable resumes a saved credential even if its stored enabled flag was true");
 const before=requests.length;pending.push({status:400,data:{error:"此凭据的本期额度已用完。"}});await click("sharing-credential-rows",{action:"use",id:"q2"});assert.equal(requests.length,before+1);assert.match(node("sharing-message").textContent,/额度已用完/);
 context.InspectorSharing.setActive(false);assert.equal(node("sharing-link").value,"");assert.equal(node("sharing-link-area").hidden,true);assert.equal(interval,null);
 pending.push({status:200,data:{...idle,protocol:3,credentials_supported:false}});context.InspectorSharing.setActive(true);await flush();assert.equal(node("sharing-cards").hidden,true);assert.match(node("sharing-message").textContent,/更新软件/);context.InspectorSharing.setActive(false);
 assert.doesNotMatch(fs.readFileSync("packet_inspector/ui/sharing.js","utf8"),/localStorage|sessionStorage/);
 console.log(JSON.stringify({sharing_ui:"passed",checks:["two_resource_cards","reusable_credentials","history_selection","expiry_and_weekly_reset","live_statistics","privacy","old_backend_guard"]}));
})().catch(error=>{console.error(error);process.exitCode=1;});
