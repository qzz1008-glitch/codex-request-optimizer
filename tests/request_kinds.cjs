const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../packet_inspector/ui/app.js'),'utf8');
const helpers=source.slice(source.indexOf('function hasModelTokenUsage('),source.indexOf('const filterLabels ='))+
  source.slice(source.indexOf('async function refreshFilters('),source.indexOf('\n}',source.indexOf('async function refreshFilters('))+2);
const selected={};
const context={t:value=>value,uiI18n:{labels:value=>value},purposeFromMetadata:()=>null,
  requestPurposeCache:new Map(),auxiliarySessionKind:()=>null,auxiliaryLabels:{},
  state:{status:{storage:{revision:1}}},sessionSummaries:[],
  api:async()=>({kinds:['generation','compaction','image_generation','image_edit','other'],statuses:[],threads:[]}),
  setFilterOptions:(key,values)=>{selected[key]=values;},statusLabel:value=>value,
  renderStorageInfo(){},renderSessions(){}};
vm.createContext(context);
vm.runInContext(helpers+'\nglobalThis.requestKind=requestKind;globalThis.hasModelTokenUsage=hasModelTokenUsage;globalThis.refreshFilters=refreshFilters;',context);
async function main(){
  for(const [path,label] of [['/images/edits','图片编辑'],['/v1/images/generations/?x=1','图片生成'],['/responses','对话生成']]){
    const packet={method:'POST',path};
    assert.equal(context.requestKind(packet),label);
    assert.equal(context.hasModelTokenUsage(packet),path==='/responses');
  }
  assert.equal(context.requestKind({method:'POST',request_kind:'image_edit'}),'图片编辑');
  assert.equal(context.requestKind({method:'POST',request_kind:'image_generation'}),'图片生成');
  assert.equal(context.requestKind({method:'POST',path:'/alpha/search'}),'工具搜索');
  assert.equal(context.requestKind({method:'POST',path:'/responses',is_compaction:true}),'上下文压缩');
  await context.refreshFilters();
  assert.deepEqual(Array.from(selected.kind,entry=>entry.value),['generation','compaction','image_generation','image_edit','other']);
  console.log('request classification and filters passed');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
