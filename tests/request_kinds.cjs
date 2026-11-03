const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../packet_inspector/ui/app.js'),'utf8');
const helpers=source.match(/^const auxiliaryLabels=.*$/m)[0]+'\n'+
  source.match(/^function auxiliarySessionKind\(row\).*$/m)[0]+'\n'+
  source.slice(source.indexOf('function purposeFromMetadata('),source.indexOf('function cacheRequestPurpose('))+
  source.slice(source.indexOf('async function sessionPacketPage('),source.indexOf('async function compressedSortedPackets('))+
  source.slice(source.indexOf('function hasModelTokenUsage('),source.indexOf('const filterLabels ='))+
  source.slice(source.indexOf('async function refreshFilters('),source.indexOf('\n}',source.indexOf('async function refreshFilters('))+2);
const selected={};
const context={t:value=>value,uiI18n:{labels:value=>value},
  URLSearchParams,
  requestPurposeCache:new Map(),
  state:{status:{storage:{revision:1}}},sessionSummaries:[],
  api:async()=>({kinds:['generation','compaction','image_generation','image_edit','other'],statuses:[],threads:[]}),
  setFilterOptions:(key,values)=>{selected[key]=values;},statusLabel:value=>value,
  originalBytes:packet=>packet.decoded_bytes,uploadBytes:packet=>packet.outbound_wire_bytes,
  compressedBytes:packet=>packet.outbound_wire_bytes,
  renderStorageInfo(){},renderSessions(){}};
vm.createContext(context);
vm.runInContext(helpers+'\nglobalThis.requestKind=requestKind;globalThis.hasModelTokenUsage=hasModelTokenUsage;globalThis.refreshFilters=refreshFilters;globalThis.requestFilterKind=requestFilterKind;globalThis.sessionPacketPage=sessionPacketPage;',context);

function packet(id,extra={}) {
  return {id:String(id).padStart(4,'0'),created:id,method:'POST',path:'/responses',thread_id:'parent',
    decoded_bytes:id*10,outbound_wire_bytes:id*3,state:'done',status:200,...extra};
}
function oldBackend(records) {
  const queries=[];
  context.api=async address=>{
    const query=new URL(address,'http://localhost').searchParams;
    queries.push(query);
    const kind=query.get('kind');
    assert.ok(!kind||['generation','compaction','image_generation','image_edit','other'].includes(kind),
      'frontend must support a running backend without the image-analysis filter');
    const coarseKind=record=>record.is_compaction?'compaction':
      ['image_generation','image_edit'].includes(record.request_kind)?record.request_kind:
      record.request_kind==='tool_search'?'other':record.method==='POST'?'generation':'other';
    const value=record=>query.get('sort')==='decoded_bytes'?record.decoded_bytes:
      ['compressed_bytes','upload_bytes'].includes(query.get('sort'))?record.outbound_wire_bytes:record.created;
    const direction=query.get('direction')==='asc'?1:-1;
    const compare=(a,b)=>(value(a)-value(b)||a.created-b.created||a.id.localeCompare(b.id))*direction;
    const after=records.find(record=>record.id===query.get('after'));
    return records.filter(record=>(!kind||coarseKind(record)===kind)&&
      (query.get('include_get')==='1'||record.method!=='GET')&&
      (!query.has('thread')||record.thread_id===query.get('thread'))&&
      (!query.has('thread_unknown')||!record.thread_id)&&
      (!query.has('status')||query.get('status')==='http:'+record.status)&&
      (!after||compare(record,after)>0)).sort(compare).slice(0,Number(query.get('limit')||100));
  };
  return queries;
}
async function page(params,signal=new AbortController().signal) {
  return Array.from(await context.sessionPacketPage(new URLSearchParams(params),signal));
}
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
  for(const field of ['request_purpose','request_kind','session_kind']){
    const packet={method:'POST',path:'/responses',request_kind:'turn',[field]:'ambient_suggestions'};
    assert.equal(context.requestKind(packet),'任务推荐');
    assert.equal(context.hasModelTokenUsage(packet),true);
  }
  context.sessionSummaries=[{thread_id:'recommendations',session_kind:'ambient_suggestions'}];
  assert.equal(context.requestKind({method:'POST',thread_id:'recommendations',request_kind:'turn'}),'任务推荐');
  await context.refreshFilters();
  assert.deepEqual(Array.from(selected.kind,entry=>entry.value),['generation','compaction','image_analysis','image_generation','image_edit','other']);
  context.api=async()=>({kinds:['generation'],statuses:[],threads:[]});
  await context.refreshFilters();
  assert.deepEqual(Array.from(selected.kind,entry=>entry.value),['generation','compaction','image_analysis','other']);

  const images=[packet(1,{image_isolation_worker:true}),packet(2,{request_purpose:'image_analysis'}),
    packet(3,{request_kind:'image_analysis'}),packet(4,{session_kind:'image_analysis'}),
    packet(5,{thread_id:'image-session'}),packet(6,{request_kind:'image_generation'}),
    packet(7,{path:'/images/edits'}),packet(8,{request_kind:'image_edit'}),
    packet(9,{path:'/images/generations'})];
  context.sessionSummaries.push({thread_id:'image-session',session_kind:'image_analysis'});
  const others=['next_message_suggestion','title_generation','session_index','ambient_suggestions','tool_search']
    .map((request_kind,index)=>packet(20+index,{request_kind}));
  others.push(packet(25,{method:'GET',path:'/models'}),packet(26,{method:'DELETE',path:'/cache'}),
    packet(27,{thread_id:'recommendations'}));
  const generations=[packet(30),packet(31,{request_kind:'subagent'})],compaction=packet(32,{is_compaction:true});
  for(const record of others)assert.equal(context.requestFilterKind(record),'other');
  for(const record of [...images,...generations,compaction])assert.notEqual(context.requestFilterKind(record),'other');
  oldBackend([...images,...others,...generations,compaction]);
  assert.deepEqual((await page({kind:'other'})).map(record=>record.id),others.map(record=>record.id).reverse());
  assert.deepEqual((await page({kind:'generation'})).map(record=>record.id),generations.map(record=>record.id).reverse());
  assert.deepEqual((await page({kind:'image_analysis',direction:'asc'})).map(record=>record.id),images.slice(0,5).map(record=>record.id));

  const sparse=[...Array.from({length:650},(_,index)=>packet(index+10)),images[0]];
  let queries=oldBackend(sparse);
  assert.deepEqual((await page({kind:'image_analysis',limit:'100'})).map(record=>record.id),[images[0].id]);
  assert.equal(queries.length,3,'scan beyond backend pages without matching records');
  queries=oldBackend([...sparse,others[0]]);
  assert.deepEqual((await page({kind:'other'})).map(record=>record.id),[others[0].id]);
  assert.equal(queries.length,3,'other requests must include matches after pages of excluded types');

  const interleaved=Array.from({length:670},(_,index)=>packet(index+100,{
    request_purpose:index%3===0?'image_analysis':null}));
  oldBackend(interleaved);
  const query=new URLSearchParams({kind:'image_analysis',limit:'100',sort:'created',direction:'desc'});
  const expected=interleaved.filter(record=>record.request_purpose==='image_analysis').reverse().map(record=>record.id);
  const actual=[];
  for(;;) {
    const result=await page(query);actual.push(...result.map(record=>record.id));
    if(result.length<100)break;
    query.set('after',result.at(-1).id);
  }
  assert.deepEqual(actual,expected,'pagination must retain every match without duplicates');

  const grouped=[packet(1,{request_purpose:'title_generation'}),
    packet(2,{thread_id:'child',request_purpose:'image_analysis'}),
    packet(3,{thread_id:'child',request_purpose:'next_message_suggestion',decoded_bytes:500}),
    packet(4,{thread_id:'unrelated',request_purpose:'title_generation'}),
    packet(5,{request_purpose:'title_generation',status:500}),
    packet(6,{request_purpose:'title_generation',decoded_bytes:300})];
  queries=oldBackend(grouped);
  const groupQuery=new URLSearchParams({kind:'other',thread_group:'1',status:'http:200',sort:'decoded_bytes',direction:'asc',limit:'2'});
  groupQuery.append('thread','parent');groupQuery.append('thread','child');
  const before=groupQuery.toString();
  assert.deepEqual((await page(groupQuery)).map(record=>record.id),['0001','0006']);
  assert.equal(groupQuery.toString(),before,'scanning must not mutate the caller query');
  groupQuery.set('after','0006');
  assert.deepEqual((await page(groupQuery)).map(record=>record.id),['0003']);
  assert.ok(queries.every(query=>query.get('include_get')==='1'&&!query.has('kind')&&query.get('status')==='http:200'));
  groupQuery.delete('after');groupQuery.set('direction','desc');
  assert.deepEqual((await page(groupQuery)).map(record=>record.id),['0003','0006']);

  const controller=new AbortController();let calls=0;
  context.api=async()=>{calls++;controller.abort();return sparse.slice(0,300);};
  await assert.rejects(page({kind:'image_analysis'},controller.signal),{name:'AbortError'});
  assert.equal(calls,1,'aborted scans must stop before the next page');
  console.log('request classification, complementary filters and pagination passed');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
