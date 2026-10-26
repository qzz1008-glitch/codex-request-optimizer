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
vm.runInContext('class Trends {\n' + source.slice(source.indexOf('  static events(packet)'), source.indexOf('  static markerShape(')) + '\n}\nthis.events=Trends.events;', context);
function packet(fields={}, count=0) {
  return {capacity_retry_count:count, capacity_retry_diagnostic:{id:'request-1', journal_saved:true, journal_available:true,
    enabled:true, eligible:true, max_retries:3, retry_count:count, scheduled_retries:count,
    state:'finished', outcome:'success', stop_reason:'stream_committed', ...fields}};
}
const details = context.capacityRetryDetails;
assert.equal(details({}).status, '暂无重试日志');
assert.equal(details({capacity_retry_count:2}).status, '已重试 2 次 · 结果未记录');
assert.equal(details(packet({},2)).status, '已重试 2 次 · 成功');
let p = packet({outcome:'capacity_error',capacity_error_seen:true,stop_reason:'retry_limit'},3);
assert.equal(details(p).status, '已重试 3 次 · 容量不足');
assert.equal(details(p).reason, '达到重试次数上限');
assert.equal(details(p).settings, '已开启 · 最多 3 次');
assert.equal(details(p).logId, 'request-1');
assert.equal(context.packetProtection(p).retry, true);
assert.deepEqual(Array.from(context.events(p)), ['retry']);
p = packet({enabled:false,outcome:'capacity_error',capacity_error_seen:true,stop_reason:'disabled'});
assert.equal(details(p).status, '未重试 · 容量不足');
assert.equal(details(p).reason, '请求时未开启重试');
assert.equal(details(p).settings, '未开启');
assert.equal(details(packet({state:'active',scheduled_retries:1})).status, '未重试 · 等待重试');
assert.equal(context.packetProtection(packet({scheduled_retries:1})).retry, false);
assert.deepEqual(Array.from(context.events(packet({scheduled_retries:1}))), []);
for(const [outcome,label] of Object.entries({cancelled:'已取消',transport_error:'连接异常',upstream_error:'上游报错',
  http_error:'HTTP 错误',unobserved:'最终结果未确认',unfinished:'缺少结束记录'})) {
  assert.equal(details(packet({outcome},1)).status, '已重试 1 次 · ' + label);
}
assert.equal(details(packet({outcome:'capacity_error',capacity_error_seen:true})).reason,'已开始转发输出');
assert.equal(details(packet({outcome:'unobserved',observation_reason:'encoded_response'})).reason,'响应带传输编码，无法识别结果');
assert.equal(details(packet({journal_saved:false})).logId,null);
assert.equal(details(packet({journal_available:false})).logExpired,true);
assert.equal(details(packet({journal_available:false})).logId,null);
assert.equal(details(packet({journal_available:null})).logUnavailable,true);
assert.equal(details(packet({journal_available:null})).logExpired,false);
assert.equal(details(packet({journal_available:null})).logId,null);
assert.equal(details(packet({id:'../invalid'})).logId,null);

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
assert(rows.some(row=>row.label==='自动重试'&&row.value==='已重试 3 次 · 容量不足'));
assert(rows.some(row=>row.label==='请求时重试设置'&&row.value==='已开启 · 最多 3 次'));
const anchor=rows.find(row=>row.label==='重试日志').lastChild.lastChild;
assert.equal(anchor.href,'/api/retry-diagnostics/request-1?download=1');
assert.equal(anchor.download,'codex-retry-request-1.json');
context.renderCompression(packet({journal_available:false}));
assert(rendered.children[1].children.some(row=>row.label==='重试日志'&&row.value==='日志已清理'));
english=true;
context.renderCompression(packet({outcome:'capacity_error',capacity_error_seen:true,stop_reason:'retry_limit'},3));
rows=rendered.children[1].children;
assert(rows.some(row=>row.label==='Automatic retries'&&row.value==='Retried 3 times · Model at capacity'));
assert(rows.some(row=>row.label==='Why retrying stopped'&&row.value==='Retry limit reached'));
console.log('Retry outcomes, actual retry markers, download controls and English labels passed.');
