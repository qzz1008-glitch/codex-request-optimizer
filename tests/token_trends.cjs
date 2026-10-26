/* Synthetic-only contracts for the chart's data flow, cancellation and usage sources. */
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('packet_inspector/ui/app.js','utf8');
const classSource=source.slice(source.indexOf('function isTrendGeneration('),source.indexOf('tokenTrendView=new TokenTrendView('));
const tokenVersion=source.split(/\r?\n/).find(line=>line.startsWith('function tokenVersion('));
const context={Date,Map,Set,AbortController,setTimeout,clearTimeout,URLSearchParams,document:{hidden:false},
  purposeFromMetadata:p=>p.session_kind||null,hasModelTokenUsage:p=>p.method==='POST'&&!p.is_compaction&&p.request_kind!=='tool_search',
  tokenVersion:null};
vm.createContext(context);vm.runInContext(tokenVersion+'\n'+classSource+'\nglobalThis.View=TokenTrendView;globalThis.isGeneration=isTrendGeneration;',context);
const View=context.View,now=Date.now()/1000,id=n=>n.toString(16).padStart(32,'0');
const packet=(n,extra={})=>({id:id(n),created:now-n,thread_id:'session-a',method:'POST',path:'/responses',state:'complete',status:200,...extra});
let checks=0;
function test(name,fn){return Promise.resolve().then(fn).then(()=>{checks++;}).catch(error=>{error.message=name+': '+error.message;throw error;});}
function view(services={}) {
 const result=Object.create(View.prototype);
 Object.assign(result,{active:true,loading:false,hydrating:false,packets:new Map(),native:new Map(),hours:0,revision:null,lastFull:0,query:'',error:false,limited:false,
  services:{api:async()=>[],status:()=>({storage:{revision:1}}),sessions:()=>[],peek:()=>null,isGeneration:context.isGeneration,...services},
  render(){},hydrate(){},scheduleRender(){}});
 return result;
}
(async()=>{
 await test('real usage wins over reported and native synthetic counts',()=>{
  assert.equal(View.input(packet(1,{upstream_usage:{input_tokens:12345},usage_override_applied:true,reported_usage:{input_tokens:500000}}),{state:'available',input_tokens:500000}),12345);
  assert.equal(View.input(packet(1,{usage_override_applied:true}),{state:'available',input_tokens:500000}),null);
 });
 await test('invalid and missing usage never becomes zero',()=>{
  for(const value of [-1,NaN,1.5,Number.MAX_SAFE_INTEGER+1])assert.equal(View.input(packet(1,{upstream_usage:{input_tokens:value}}),null),null);
  assert.equal(View.input(packet(1),{state:'available',input_tokens:0}),0);
  assert.equal(View.input(packet(1),{state:'pending',input_tokens:700}),null);
 });
 await test('all simultaneous mechanisms are preserved',()=>{
  const events=View.events(packet(1,{usage_override_applied:true,overflow_cache_hit:true,capacity_retry_count:3,is_compaction:true}));
  assert.deepEqual(Array.from(events),['predictive','overflow','retry','compact']);
  assert.ok(View.events(packet(1,{relay_error:'overflow_prepare_failed'})).includes('overflow'));
 });
 await test('automatic auxiliary requests do not distort session context',()=>{
  for(const kind of ['prewarm','title_generation','next_message_suggestion','session_index','tool_search'])assert.equal(context.isGeneration(packet(1,{request_kind:kind})),false);
  assert.equal(context.isGeneration(packet(1,{session_kind:'image_analysis'})),true);
  assert.equal(context.isGeneration(packet(1,{is_compaction:true})),false);
 });
 await test('scale includes all values and identity colors stay stable',()=>{
  for(const n of [0,1,999,500000,10000000]){const scale=View.scale(n);assert.ok(scale.max>=n&&scale.step>0&&Number.isFinite(scale.max));}
  assert.equal(View.color('session-a'),View.color('session-a'));assert.notEqual(View.color('session-a'),View.color('session-b'));
 });
 await test('model shares request times while preserving missing values and event-only compaction',()=>{
  const result=view({sessions:()=>[{thread_id:'session-a',title:'A'}]});
  result.packets=new Map([packet(3,{upstream_usage:{input_tokens:30}}),packet(2),packet(1,{is_compaction:true}),packet(4,{thread_id:null}),packet(5,{request_kind:'title_generation'})].map(p=>[p.id,p]));
  const model=result.model();assert.equal(model.series.length,1);assert.equal(model.unknown,1);
  assert.deepEqual(Array.from(model.visible[0].points,p=>p.input),[30,null,null]);
  assert.equal(model.visible[0].points[2].events[0],'compact');
  assert.equal(model.visible.length,model.matched.length);
  result.query='missing';assert.equal(result.model().matched.length,0);
 });
 await test('full snapshot pages without duplicates, and deltas stop on overlap',async()=>{
  const rows=Array.from({length:303},(_,i)=>packet(i+1)),queries=[];let revision=1;
  const result=view({status:()=>({storage:{revision}}),api:async path=>{
   const query=new URL(path,'http://fixture').searchParams;queries.push(query);
   const after=query.get('after'),start=after?rows.findIndex(row=>row.id===after)+1:0;return rows.slice(start,start+300);
  }});
  await result.refresh();assert.equal(result.packets.size,303);assert.equal(queries.length,2);assert.equal(queries[1].get('after'),rows[299].id);
  await result.refresh();assert.equal(queries.length,2,'unchanged revision uses cached snapshot');
  revision++;rows.unshift(packet(1000,{created:now+1}));await result.refresh();assert.equal(queries.length,3);assert.equal(result.packets.size,304);
 });
 await test('stale in-flight requests are refreshed outside the newest page',async()=>{
  let revision=1,detail=0;
  const result=view({status:()=>({storage:{revision}}),api:async path=>{
   if(path.includes('/api/packets/')){detail++;return packet(1,{state:'complete',upstream_usage:{input_tokens:80}});}
   return [packet(2)];
  }});
  await result.refresh();result.packets.set(id(1),packet(1,{state:'streaming'}));revision++;
  await result.refresh();assert.equal(detail,1);assert.equal(result.packets.get(id(1)).upstream_usage.input_tokens,80);
 });
 await test('failed snapshots retain the last good chart and can recover',async()=>{
  let failed=false,revision=1;
  const result=view({status:()=>({storage:{revision}}),api:async()=>{if(failed==='timeout'){result.controller.abort();throw Error('timeout');}if(failed)throw Error('offline');return [packet(revision)];}});
  await result.refresh();failed=true;revision++;await result.refresh();assert.equal(result.error,true);assert.ok(result.packets.has(id(1)));
  failed='timeout';await result.refresh();assert.equal(result.error,true);assert.ok(result.packets.has(id(1)));
  failed=false;await result.refresh();assert.equal(result.error,false);assert.ok(result.packets.has(id(2)));
 });
 await test('canceled old loads cannot replace a newer snapshot',async()=>{
  const result=view({api:(_path,_data,signal)=>new Promise((_resolve,reject)=>signal.addEventListener('abort',()=>reject(Error('aborted')),{once:true}))});
  const old=result.refresh(true);result.cancelPending();result.services.api=async()=>[packet(77)];
  await result.refresh(true);await old;assert.equal(result.packets.size,1);assert.ok(result.packets.has(id(77)));
 });
 await test('snapshot and metadata retention are bounded',async()=>{
  let calls=0;
  const result=view({api:async()=>Array.from({length:300},(_,i)=>packet(++calls*1000+i))});
  await result.refresh();assert.equal(result.packets.size,12000);assert.equal(result.limited,true);
 });
 await test('native backfill is bounded to two readers and eight jobs per pass',async()=>{
  let active=0,maximum=0,calls=0;
  const result=view({read:async()=>{active++;maximum=Math.max(maximum,active);calls++;await new Promise(resolve=>setImmediate(resolve));active--;return {state:'available',input_tokens:50};}});
  result.hydrate=View.prototype.hydrate;
  result.packets=new Map(Array.from({length:9},(_,i)=>{const p=packet(i+1);return [p.id,p];}));
  await result.hydrate();assert.equal(calls,8);assert.equal(maximum,2);
  await result.hydrate();assert.equal(calls,9);
  result.packets.set(id(50),packet(50,{usage_override_applied:true}));await result.hydrate();assert.equal(calls,9,'synthetic protection usage never triggers native backfill');
 });
 console.log(JSON.stringify({passed:checks}));
})().catch(error=>{console.error(error);process.exitCode=1;});
