/* Decimal display and legacy/current storage API round trips. */
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('packet_inspector/ui/app.js','utf8'),context={};
const compact=source.split(/\r?\n/).find(line=>line.startsWith('function compact('));
const first=source.indexOf('function storageLimitSettings('),last=source.indexOf('function fillSettings(',first);
assert(first>=0&&last>first);
vm.createContext(context);vm.runInContext(compact+'\n'+source.slice(first,last),context);
for(const [bytes,label] of [[0,'0 B'],[999,'999 B'],[1000,'1.0 KB'],[1024,'1.0 KB'],[250000,'250.0 KB'],
 [1000000,'1.00 MB'],[1048576,'1.05 MB'],[536870912,'536.87 MB'],[1000000000,'1000.00 MB']])assert.equal(context.compact(bytes),label);
for(const legacy of [16,512,8192]){
 const settings=context.storageLimitSettings({max_storage_mb:legacy,storage:{max_bytes:legacy*1048576}});
 assert.equal(settings.value,legacy*1048576/1000000);assert.equal(settings.toBackend(settings.value),legacy);
 assert.equal(settings.key,'max_storage_mb');assert.equal(settings.step,'1.048576');
}
for(const bytes of [16777216,512000000,512000123,8589934592]){
 const settings=context.storageLimitSettings({max_storage_bytes:bytes,max_storage_mb:Math.ceil(bytes/1048576)});
 assert.equal(settings.value,bytes/1000000);assert.equal(settings.toBackend(settings.value),bytes);
 assert.equal(settings.key,'max_storage_bytes');assert.equal(settings.step,'any');
}
const html=fs.readFileSync('packet_inspector/ui/index.html','utf8'),i18n=fs.readFileSync('packet_inspector/ui/i18n.js','utf8');
assert.match(html,/id="budget-unit"[^>]*>KB<\/span>/);assert(html.includes('存储上限（MB）'));
assert(!/\b(?:KiB|MiB|GiB|kB)\b/.test(html+i18n));
console.log(JSON.stringify({passed:true}));
