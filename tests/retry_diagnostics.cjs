const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('packet_inspector/ui/app.js', 'utf8');
const translations = fs.readFileSync('packet_inspector/ui/i18n.js', 'utf8');
const dictionary = vm.runInNewContext(translations.slice(translations.indexOf('const english = {'), translations.indexOf('\n  };') + 5) + '\nenglish;');
let english = false;
const context = {nf:new Intl.NumberFormat('en-US'), t:key=>english?(dictionary[key]||key):key};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function capacityRetryCount('), source.indexOf('function listTokens(')), context);
vm.runInContext(source.slice(source.indexOf('function sortInputTokens('),source.indexOf('function sortedTokenPackets(')),context);
vm.runInContext('class Trends {\n' + source.slice(source.indexOf('  static events(packet)'), source.indexOf('  static markerShape(')) + '\n}\nthis.events=Trends.events;', context);
function packet(fields={}, count=0) {
  return {source:'live',capacity_retry_count:count, capacity_retry_diagnostic:{id:'request-1', journal_saved:true, journal_available:true,
    enabled:true, eligible:true, max_retries:3, retry_count:count, scheduled_retries:count,
    state:'finished', outcome:'success', stop_reason:'stream_committed', ...fields}};
}
assert.equal(context.packetProtection({native_retry_advised:true,capacity_retry_count:0}).retry,true);
assert.deepEqual(Array.from(context.events({native_retry_advised:true})),['retry']);
let p = packet({outcome:'capacity_error',capacity_error_seen:true,stop_reason:'retry_limit'},3);
assert.equal(context.packetProtection(p).retry, true);
assert.deepEqual(Array.from(context.events(p)), ['retry']);
p = packet({enabled:false,outcome:'capacity_error',capacity_error_seen:true,stop_reason:'disabled'});
assert.equal(context.packetProtection(p).retry, false);
assert.equal(context.packetProtection(packet({scheduled_retries:1})).retry, false);
assert.deepEqual(Array.from(context.events(packet({scheduled_retries:1}))), []);

function node(tag,text='') {
  return {tag,text,children:[],dataset:{},classList:{add(){}},append(...children){this.children.push(...children);},
    get lastChild(){return this.children.at(-1);}};
}
let rendered;
Object.assign(context, {
  document:{createDocumentFragment:()=>node('fragment')},
  panel:title=>Object.assign(node('section'),{title}),
  fact:(parent,label,value)=>{const row=node('fact');row.label=label;row.value=value;row.append(node('label',label),node('value',value));parent.append(row);return row;},
  el:(tag,cls,text)=>node(tag,text),
  originalBytes:()=>100,compressedBytes:()=>50,bytes:String,
  $:()=>null,reconcileContent:(_,root)=>{rendered=root;},renderProtectionPanelState(){}
});
vm.runInContext(source.slice(source.indexOf('function renderCompression(m)'),source.indexOf('function bars(')),context);
context.renderCompression(packet({outcome:'capacity_error',capacity_error_seen:true,stop_reason:'retry_limit'},3));
let rows=rendered.children[1].children;
assert.deepEqual(Array.from(rows,row=>row.label),['预测保护','超限保护','重试保护']);
assert.equal(rows[2].value,'已触发');
context.renderCompression(packet({journal_available:false}));
assert.equal(rendered.children[1].children[2].value,'未触发');
context.renderCompression(packet({outcome:'cancelled',scheduled_retries:1}));
assert.equal(rendered.children[1].children[2].value,'未触发');
context.renderCompression({capacity_retry_count:2});
assert.equal(rendered.children[1].children[2].value,'已触发');
context.renderCompression({});
assert.equal(rendered.children[1].children[2].value,'暂无记录');
// A summary can turn a compressed packet into a small identity upload. The
// encoding/level displayed next to the pre-summary size must describe that size.
const interrupted={source:'live',state:'relay_error',status:200,
  relay_content_encoding:'identity',relay_zstd_level:null,
  overflow_compaction_calls:1,overflow_original_wire_bytes:1004951,
  usage_override_request_bytes:1004951,usage_override_trigger_bytes:950000};
function facts(section){return Object.fromEntries(rendered.children[section].children.map(row=>[row.label,row.value]));}
context.renderCompression(interrupted);
assert.equal(facts(0)['压缩方式'],'—');
assert.equal(facts(0)['zstd 压缩等级'],'—');
assert.equal(facts(1)['预测保护'],'未完成');
assert.equal(facts(1)['超限保护'],'已触发');
context.renderCompression({...interrupted,transport_content_encoding:'zstd',transport_zstd_level:17});
assert.equal(facts(0)['压缩方式'],'zstd');
assert.equal(facts(0)['zstd 压缩等级'],17);
context.renderCompression({...interrupted,transport_content_encoding:'zstd',transport_zstd_level:22});
assert.equal(facts(0)['zstd 压缩等级'],22);
context.renderCompression({...interrupted,state:'streaming'});
assert.equal(facts(1)['预测保护'],'等待响应');
context.renderCompression({...interrupted,state:'complete',usage_override_applied:true});
assert.equal(facts(1)['预测保护'],'已触发');
context.renderCompression({...interrupted,state:'complete',usage_override_applied:false,reported_usage:{input_tokens:500000}});
assert.equal(facts(1)['预测保护'],'已满足');
context.renderCompression({source:'live',relay_content_encoding:'zstd',relay_zstd_level:1});
assert.equal(facts(0)['压缩方式'],'zstd');
assert.equal(facts(0)['zstd 压缩等级'],1);
context.renderCompression({...interrupted,overflow_compaction_calls:0,overflow_cache_hit:true});
assert.equal(facts(0)['压缩方式'],'—');
english=true;
context.renderCompression(interrupted);
assert.equal(facts(1)['Predictive protection'],'Incomplete');
context.renderCompression({...interrupted,state:'streaming'});
assert.equal(facts(1)['Predictive protection'],'Waiting for response');
context.renderCompression({...interrupted,usage_override_applied:false,reported_usage:{input_tokens:500000}});
assert.equal(facts(1)['Predictive protection'],'Already at threshold');
context.renderCompression(packet({outcome:'capacity_error',capacity_error_seen:true,stop_reason:'retry_limit'},3));
rows=rendered.children[1].children;
assert.deepEqual(Array.from(rows,row=>row.label),['Predictive protection','Over-limit protection','Retry protection']);
assert.equal(rows[2].value,'Triggered');
console.log('Retry trigger status, actual retry markers and English labels passed.');
