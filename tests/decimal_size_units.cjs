/* Decimal display and whole-MB storage settings. */
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('packet_inspector/ui/app.js','utf8'),context={};
const compact=source.split(/\r?\n/).find(line=>line.startsWith('function compact('));
const first=source.indexOf('function storageLimitSettings('),last=source.indexOf('function fillSettings(',first);
assert(first>=0&&last>first);
vm.createContext(context);vm.runInContext(compact+'\n'+source.slice(first,last),context);
for(const [bytes,label] of [[0,'0 B'],[999,'999 B'],[1000,'1.0 KB'],[1024,'1.0 KB'],[250000,'250.0 KB'],
 [1000000,'1.00 MB'],[1048576,'1.05 MB'],[536870912,'536.87 MB'],[1000000000,'1000.00 MB']])assert.equal(context.compact(bytes),label);
for(const [bytes,mb] of [[16777216,17],[512000000,512],[512000123,513],[8589934592,8590],[8590000000,8590]]){
 const settings=context.storageLimitSettings({max_storage_bytes:bytes});
 assert.equal(settings.value,mb);assert.equal(settings.toBackend(mb),mb*1000000);
 assert.equal(settings.min,17);assert.equal(settings.max,8590);
}
const prior=context.storageLimitSettings({max_storage_bytes:8589934592,storage_limit_min_bytes:16777216,storage_limit_max_bytes:8589934592});
assert.equal(prior.value,8589);assert.equal(prior.min,17);assert.equal(prior.max,8589);
const html=fs.readFileSync('packet_inspector/ui/index.html','utf8'),i18n=fs.readFileSync('packet_inspector/ui/i18n.js','utf8');
assert.match(html,/id="budget-unit"[^>]*>KB<\/span>/);
assert.match(html,/<span id="packet-limit-label" class="settings-key">记录上限<\/span>/);
assert.match(html,/<input id="packet-limit"[^>]*>\s*<span id="packet-limit-unit"[^>]*>条<\/span>/);
assert.match(html,/<span id="storage-limit-label" class="settings-key">存储上限<\/span>/);
assert.match(html,/<input id="storage-limit" type="number" min="17" max="8590" step="1"[^>]*>\s*<span id="storage-limit-unit"[^>]*>MB<\/span>/);
assert(!/\b(?:KiB|MiB|GiB|kB)\b/.test(html+i18n));
console.log(JSON.stringify({passed:true}));
