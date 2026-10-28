/* Synthetic-only contracts for the chart's data flow, cancellation and usage sources. */
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('packet_inspector/ui/app.js','utf8');
const classSource=source.slice(source.indexOf('function isTrendGeneration('),source.indexOf('tokenTrendView=new TokenTrendView('));
const helpers=source.split(/\r?\n/).filter(line=>/^function (tokenVersion|compressedBytes|originalBytes|uploadBytes|compact)\(/.test(line)).join('\n');
// Keep samples and viewports on one clock, including on slower CI runners.
const fixtureNow=Date.now();
class FixtureDate extends Date{static now(){return fixtureNow;}}
const context={Date:FixtureDate,Map,Set,AbortController,setTimeout,clearTimeout,URLSearchParams,document:{hidden:false},
  purposeFromMetadata:p=>p.session_kind||null,hasModelTokenUsage:p=>p.method==='POST'&&!p.is_compaction&&p.request_kind!=='tool_search',
  tokenVersion:null};
vm.createContext(context);vm.runInContext(helpers+'\n'+classSource+'\nglobalThis.View=TokenTrendView;globalThis.isGeneration=isTrendGeneration;',context);
const View=context.View,now=fixtureNow/1000,id=n=>n.toString(16).padStart(32,'0');
const packet=(n,extra={})=>({id:id(n),created:now-n,thread_id:'session-a',method:'POST',path:'/responses',state:'complete',status:200,...extra});
let checks=0;
function test(name,fn){return Promise.resolve().then(fn).then(()=>{checks++;}).catch(error=>{error.message=name+': '+error.message;throw error;});}
function view(services={}) {
 const result=Object.create(View.prototype);
  Object.assign(result,{active:true,loading:false,hydrating:false,packets:new Map(),native:new Map(),hours:1,windowEnd:null,loadedBounds:{since:now-12*3600-1,before:null},requestBounds:null,windowTimer:null,oldestCreated:now-12*3600,boundsReady:true,revision:null,lastFull:0,query:'',error:false,limited:false,
  services:{api:async()=>[],status:()=>({storage:{revision:1,oldest_created:now-12*3600},trend_packets_supported:true}),sessions:()=>[],peek:()=>null,isGeneration:context.isGeneration,...services},
  render(){},hydrate(){},scheduleRender(){}});
 return result;
}
class Node {
 constructor(tag){this.tag=tag;this.attrs={};this.children=[];this.dataset={};this.textContent='';this.hidden=true;this.clientWidth=640;this.clientHeight=240;this.offsetWidth=260;this.offsetHeight=150;
  this.style={setProperty:(key,value)=>{this.style[key]=value;}};this.events=new Map();
  this.classList={toggle:(key,on)=>{const names=new Set((this.attrs.class||'').split(' ').filter(Boolean));on?names.add(key):names.delete(key);this.attrs.class=[...names].join(' ');}};}
 setAttribute(key,value){this.attrs[key]=String(value);if(key.startsWith('data-'))this.dataset[key.slice(5).replace(/-([a-z])/g,(_,char)=>char.toUpperCase())]=String(value);}
 addEventListener(type,callback,options){this.events.set(type,{callback,options});}
 append(...nodes){for(const node of nodes)this.children.push(...(node.tag==='#fragment'?node.children:[node]));}
 prepend(node){this.children.unshift(node);}
 replaceChildren(...nodes){this.children=[];this.append(...nodes);}
 all(test){return this.children.flatMap(child=>[...(test(child)?[child]:[]),...child.all(test)]);}
 querySelectorAll(){return this.all(node=>node.attrs['data-series']!==undefined);}
 contains(node){return this===node||this.children.some(child=>child.contains(node));}
 getBoundingClientRect(){return {left:this.left||0,top:this.top||0,width:this.clientWidth,height:this.clientHeight};}
}
function rendered(rows,services={}){
 const ids=new Map(),html=fs.readFileSync('packet_inspector/ui/index.html','utf8'),frames=[],events=new Map();
 for(const match of html.matchAll(/<(\w+)\b[^>]*\bid="([^"]+)"[^>]*>/g))ids.set(match[2],new Node(match[1]));
 ids.get('trend-charts').clientHeight=512;ids.get('trend-focus').clientHeight=512;
 ids.get('trend-range-slider').clientWidth=180;
 ids.get('trend-time-axis').clientHeight=32;ids.get('trend-time-axis').top=480;
 for(const id of ['trend-size-tooltip','trend-tooltip']){ids.get(id).offsetWidth=150;ids.get(id).offsetHeight=30;}
 ids.get('trend-time-tooltip').offsetWidth=128;ids.get('trend-time-tooltip').offsetHeight=24;
 context.document={hidden:false,addEventListener:(type,handler)=>events.set(type,handler),createDocumentFragment:()=>new Node('#fragment'),createElementNS:(_,tag)=>new Node(tag),
  createTextNode:text=>{const node=new Node('#text');node.textContent=text;return node;},querySelectorAll:()=>[]};
 context.$=id=>ids.get(id);context.t=text=>text;context.nf=new Intl.NumberFormat('en-US');context.uiI18n={language:'en'};
 context.el=(tag,cls='',text='')=>{const node=new Node(tag);node.attrs.class=cls;node.textContent=text;return node;};
 context.reconcileContent=(target,fragment)=>target.replaceChildren(fragment);
 context.localStorage={getItem:()=>null,setItem(){}};
 context.ResizeObserver=class{constructor(callback){this.callback=callback;this.observed=[];}observe(node){this.observed.push(node);}};
 context.requestAnimationFrame=callback=>{frames.push(callback);return frames.length;};
 const result=new View(view(services).services);result.active=true;result.loadedBounds={since:now-12*3600-1,before:null};result.packets=new Map(rows.map(row=>[row.id,row]));
 result.render();return {result,ids,frames,events};
}
function chartNodes(chart,cls){return chart.svg.all(node=>(node.attrs.class||'').split(' ').includes(cls));}
function focusLines(result){return chartNodes({svg:result.guide},'trend-focus-line');}
function nodeText(node){return node.textContent+node.children.map(nodeText).join(' ');}
async function clocked(fn){
 const previous=context.Date;let current=now*1000;context.Date=class extends Date{static now(){return current;}};
 try{return await fn(delta=>{current+=delta;});}finally{context.Date=previous;}
}
function dualRows(){return [
 packet(101,{created:now-300,upstream_usage:{input_tokens:100},outbound_wire_bytes:100000}),
 packet(102,{created:now-200,usage_override_applied:true,usage_override_request_bytes:250000,outbound_wire_bytes:200000}),
 packet(103,{created:now-100,upstream_usage:{input_tokens:300},overflow_cache_hit:true,overflow_original_wire_bytes:400000,outbound_wire_bytes:300000}),
 packet(104,{created:now-50,is_compaction:true,outbound_wire_bytes:150000})];}
function idleRows(){return [12,1].flatMap((hours,chunk)=>Array.from({length:13},(_,index)=>packet(800+chunk*20+index,{
 created:now-hours*3600+index*300,upstream_usage:{input_tokens:100+index*100},outbound_wire_bytes:100000+index*10000
})));}
function eventRows(){return [
 packet(701,{usage_override_applied:true}),packet(702,{overflow_cache_hit:true,native_retry_advised:true}),
 packet(703,{thread_id:'session-b',native_retry_advised:true}),packet(704,{thread_id:'session-b',is_compaction:true}),
 packet(705),packet(706,{thread_id:'session-b',usage_override_applied:true,native_retry_advised:true})
].map((row,index)=>({...row,upstream_usage:{input_tokens:100+index*100},outbound_wire_bytes:100000+index*100000}));}
(async()=>{
 await test('size axis steps use decimal bytes and match the configured limit units',()=>{
  const scale=View.sizeScale(1000000);
  assert.equal(scale.step,200000);assert.equal(scale.max,1000000);
  const {result}=rendered([packet(700,{outbound_wire_bytes:250000})],{status:()=>({budget_bytes:1000000})});
  assert.ok(nodeText(chartNodes(result.charts[0],'trend-budget-label')[0]).includes('1.00 MB'));
  assert.ok(chartNodes(result.charts[0],'trend-axis-label').some(node=>nodeText(node)==='200 KB'));
 });
 await test('configured size limits remain visible above the data and at the axis maximum',()=>{
  for(const budget of [1000000,1048576])for(const rows of [[packet(701,{outbound_wire_bytes:100000,upstream_usage:{input_tokens:100}})],[]]){
   const {result}=rendered(rows,{status:()=>({storage:{revision:1,oldest_created:now-12*3600},budget_bytes:budget})}),[size,token]=result.charts;
   const [line]=chartNodes(size,'trend-budget-line'),[label]=chartNodes(size,'trend-budget-label');
   assert.equal(chartNodes(size,'trend-budget-line').length,1);assert.equal(chartNodes(token,'trend-budget-line').length,0);
   assert.equal(Number(line.attrs.x1),size.bounds.left);assert.equal(Number(line.attrs.x2),size.bounds.width-size.bounds.right);
   assert.equal(line.attrs.y1,line.attrs.y2);assert.ok(Number(line.attrs.y1)>=size.bounds.top&&Number(line.attrs.y1)<=size.bounds.height-size.bounds.bottom);
   assert.ok(Number(label.attrs.y)>=0);assert.ok(nodeText(label).includes(context.compact(budget)));
   assert.equal(chartNodes(size,'trend-budget')[0].attrs['pointer-events'],'none');
  }
 });
 await test('saved size limit changes redraw without packet changes or additional reads',async()=>{
  let budget=2000000,reads=0;
  const {result}=rendered([packet(702,{outbound_wire_bytes:1800000,upstream_usage:{input_tokens:100}})],{
   status:()=>({storage:{revision:1,oldest_created:now-12*3600},budget_bytes:budget}),api:async()=>{reads++;return[];}});
  const before=chartNodes(result.charts[0],'trend-budget-line')[0].attrs.y1,tokenPoints=Array.from(result.charts[1].points,point=>point.y);
  result.lastFull=context.Date.now();result.revision=1;budget=1000000;await result.refresh();
  assert.notEqual(chartNodes(result.charts[0],'trend-budget-line')[0].attrs.y1,before);
  assert.ok(nodeText(chartNodes(result.charts[0],'trend-budget-label')[0]).includes(context.compact(budget)));
  assert.deepEqual(Array.from(result.charts[1].points,point=>point.y),tokenPoints);assert.equal(reads,0);
 });
 await test('missing and invalid size limits do not create a guide or invalid coordinates',()=>{
  for(const budget of [undefined,null,0,-5,NaN,Infinity,'1000000']){
   const {result}=rendered([packet(703,{outbound_wire_bytes:100000,upstream_usage:{input_tokens:100}})],{status:()=>({budget_bytes:budget})});
   for(const chart of result.charts){assert.equal(chartNodes(chart,'trend-budget-line').length,0);assert.ok(chart.points.every(point=>Number.isFinite(point.x)&&Number.isFinite(point.y)));}
  }
 });
 await test('real usage wins over reported and native synthetic counts',()=>{
  assert.equal(View.input(packet(1,{upstream_usage:{input_tokens:12345},usage_override_applied:true,reported_usage:{input_tokens:500000}}),{state:'available',input_tokens:500000}),12345);
  assert.equal(View.input(packet(1,{usage_override_applied:true}),{state:'available',input_tokens:500000}),null);
 });
 await test('metadata-only records retain both charts and protection markers',()=>{
  const rows=[packet(601,{details_evicted:true,upstream_usage:{input_tokens:12345},outbound_wire_bytes:345678,
    usage_override_applied:true,overflow_cache_hit:true}),
    packet(602,{details_evicted:true,upstream_usage:{input_tokens:22345},outbound_wire_bytes:445678})];
  const {result}=rendered(rows);
  assert.equal(result.charts[0].points.length,2);
  assert.equal(result.charts[1].points.length,2);
  assert.equal(View.input(rows[0],null),12345);
  assert.deepEqual(Array.from(View.events(rows[0])),['predictive','overflow']);
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
  const result=view({status:()=>({storage:{revision,oldest_created:now-12*3600}}),api:async path=>{
   const query=new URL(path,'http://fixture').searchParams;queries.push(query);
   const after=query.get('after'),start=after?rows.findIndex(row=>row.id===after)+1:0;return rows.slice(start,start+300);
  }});
  await result.refresh();assert.equal(result.packets.size,303);assert.equal(queries.length,2);assert.equal(queries[1].get('after'),rows[299].id);
  await result.refresh();assert.equal(queries.length,2,'unchanged revision uses cached snapshot');
  revision++;rows.unshift(packet(1000,{created:now+1}));await result.refresh();assert.equal(queries.length,3);assert.equal(result.packets.size,304);
 });
 await test('stale in-flight requests are refreshed outside the newest page',async()=>{
  let revision=1,detail=0;
  const result=view({status:()=>({storage:{revision,oldest_created:now-12*3600}}),api:async path=>{
   if(path.includes('/api/packets/')){detail++;return packet(1,{state:'complete',upstream_usage:{input_tokens:80}});}
   return [packet(2)];
  }});
  await result.refresh();result.packets.set(id(1),packet(1,{state:'streaming'}));revision++;
  await result.refresh();assert.equal(detail,1);assert.equal(result.packets.get(id(1)).upstream_usage.input_tokens,80);
 });
 await test('failed snapshots retain the last good chart and can recover',async()=>{
  let failed=false,revision=1;
  const result=view({status:()=>({storage:{revision,oldest_created:now-12*3600}}),api:async()=>{if(failed==='timeout'){result.controller.abort();throw Error('timeout');}if(failed)throw Error('offline');return [packet(revision)];}});
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
  let n=0;
  const result=view({api:async path=>Array.from({length:Number(new URL(path,'http://fixture').searchParams.get('limit'))},()=>packet(++n,{created:now-n/2}))});
  await result.refresh();assert.equal(result.packets.size,20000);assert.equal(result.limited,true);assert.equal(result.segmented,true);
  assert.equal(result.covers(result.loadedBounds,{start:now-20001,end:now}),false);
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
 await test('size curves use the request-list basis without fabricating missing values',()=>{
  assert.equal(View.size({overflow_original_wire_bytes:50,usage_override_request_bytes:40,outbound_wire_bytes:30}),50);
  assert.equal(View.size({usage_override_request_bytes:40,outbound_wire_bytes:30}),40);
  assert.equal(View.size({outbound_wire_bytes:0}),0);
  for(const value of [undefined,-1,1.5,NaN,Infinity])assert.equal(View.size({outbound_wire_bytes:value}),null);
  for(const value of [0,100,1000000,12000000]){const scale=View.sizeScale(value);assert.ok(scale.max>=value&&scale.step>=1&&Number.isFinite(scale.max));}
 });
 await test('two rendered metrics share time coordinates and bridge missing usage',()=>{
  const {result}=rendered(dualRows()),[size,token]=result.charts;
  assert.equal(result.resize.observed.length,2);
  assert.equal(size.bounds.start,token.bounds.start);assert.equal(size.bounds.end,token.bounds.end);
  assert.deepEqual(Array.from(size.points,p=>p.value),[100000,250000,400000,150000]);
  assert.deepEqual(Array.from(token.points,p=>p.value),[100,300]);
  for(const point of token.points)assert.equal(point.x,size.points.find(p=>p.packet.id===point.packet.id).x);
  for(const chart of result.charts){const path=chartNodes(chart,'trend-curve')[0].attrs.d;assert.equal((path.match(/M/g)||[]).length,1);assert.equal((path.match(/L/g)||[]).length,chart.points.length-1);assert.ok(!path.includes('NaN'));}
  assert.equal(chartNodes(size,'trend-axis-title')[0].textContent,'请求大小');
  assert.equal(chartNodes(token,'trend-axis-title')[0].textContent,'输入 Token');
 });
 await test('one adaptive bottom time axis aligns ticks with both equal-height metric panels',()=>{
  const {result}=rendered(dualRows());result.loadedBounds=null;result.render();
  const labels=chartNodes({svg:result.timeAxis},'trend-axis-label');
  assert.ok(labels.length>=3&&labels.length<=9);assert.ok(labels.every(label=>/^\d{2}:[0-5]0$/.test(label.textContent)));assert.equal(result.timeAxis.attrs.viewBox,'0 0 640 32');
  const [size,token]=result.charts;
  assert.equal(size.bounds.height-size.bounds.top-size.bounds.bottom,token.bounds.height-token.bounds.top-token.bounds.bottom);
  for(const chart of result.charts){
   assert.ok(chartNodes(chart,'trend-axis-label').every(label=>!label.textContent.includes(':')));
   const grids=chartNodes(chart,'trend-time-grid');assert.equal(grids.length,labels.length);
   for(let i=0;i<labels.length;i++){
    assert.equal(Number(labels[i].attrs.x),chart.bounds.x(Number(labels[i].dataset.time)));
    assert.equal(grids[i].attrs.x1,labels[i].attrs.x);assert.equal(grids[i].dataset.time,labels[i].dataset.time);
   }
  }
  const fine=View.timeTicks(now-3600,now,800),narrow=View.timeTicks(now-3600,now,300),distant=View.timeTicks(now-12*3600,now,800);
  assert.ok(fine.length>=6&&fine.length<=9);assert.ok(fine.slice(1).every((stamp,index)=>stamp-fine[index]===600));
  assert.ok(fine.some(stamp=>new Date(stamp*1000).getMinutes()!==0));
  for(const ticks of [narrow,distant]){assert.ok(ticks.length>=2&&ticks.length<=9);assert.ok(ticks.slice(1).every((stamp,index)=>stamp-ticks[index]>600));}
 });
 await test('one global time coordinate shortens long idle intervals to one minute',()=>{
  const scale=View.compactTimeScale(0,7200,[[100,200],[3500,3600],[7000,7100]]);
  assert.equal(scale.gaps.length,2);
  for(const gap of scale.gaps)assert.ok(Math.abs(scale.offset(gap.end)-scale.offset(gap.start)-60)<1e-6);
  let previous=-1;
  for(let stamp=0;stamp<=7200;stamp+=25){const offset=scale.offset(stamp);assert.ok(offset>previous);assert.ok(Math.abs(scale.at(offset)-stamp)<1e-6);previous=offset;}
  assert.equal(scale.at(-1),0);assert.equal(scale.at(Infinity),7200);
 });
 await test('short gaps and an empty history preserve linear time',()=>{
  for(const activity of [[],[[100,200],[600,700],[1100,1800]],[[0,1800]]]){
   const scale=View.compactTimeScale(0,1800,activity);assert.equal(scale.gaps.length,0);
   assert.equal(scale.offset(900),900);assert.equal(scale.at(450),450);
  }
 });
 await test('overlapping request durations merge before detecting idle intervals',()=>{
  const scale=View.compactTimeScale(0,3600,[[0,1000],[300,500],[900,2600],[2500,3600]]);
  assert.equal(scale.gaps.length,0);assert.equal(scale.at(1800),1800);
 });
 await test('both charts share a continuous curve and one honest idle marker on the time axis',()=>{
  const {result}=rendered(idleRows());result.seek(.5);result.render();const [size,token]=result.charts,axis=size.bounds.axis;
  assert.equal(axis,token.bounds.axis);assert.equal(axis.gaps.length,1);
  const breaks=chartNodes({svg:result.timeAxis},'trend-time-break');assert.equal(breaks.length,1);
  assert.ok(nodeText(breaks[0]).includes(result.time(Number(breaks[0].dataset.start),true)));
  for(const chart of result.charts){
   assert.equal(chartNodes(chart,'trend-idle-gap').length,0);
    const folds=chartNodes(chart,'trend-time-fold');assert.equal(folds.length,1);assert.equal(folds[0].dataset.start,breaks[0].dataset.start);assert.equal(folds[0].dataset.end,breaks[0].dataset.end);
    const shade=chartNodes(chart,'trend-fold-shade')[0],center=Number(shade.attrs.x)+Number(shade.attrs.width)/2;
    assert.ok(Math.abs(center-(chart.bounds.x(axis.gaps[0].start)+chart.bounds.x(axis.gaps[0].end))/2)<1e-5);
    assert.ok(Number(shade.attrs.width)>=12);assert.equal(Number(shade.attrs.height),chart.bounds.height-chart.bounds.top-chart.bounds.bottom);
   assert.equal((chartNodes(chart,'trend-curve')[0].attrs.d.match(/M/g)||[]).length,1);
   for(const point of chart.points)assert.equal(point.x,size.bounds.x(point.packet.created));
   for(const grid of chartNodes(chart,'trend-time-grid'))assert.equal(Number(grid.attrs.x1),size.bounds.x(Number(grid.dataset.time)));
  }
  for(const tick of chartNodes({svg:result.timeAxis},'trend-axis-label')){
   const stamp=Number(tick.dataset.time);assert.equal(axis.gaps.some(gap=>stamp>gap.start&&stamp<gap.end),false);
   assert.equal(Number(tick.attrs.x),size.bounds.x(stamp));
  }
  result.cancelPending();
 });
 await test('one-hour navigation crosses overnight idle time without an empty window',()=>{
  const {result}=rendered(idleRows());result.refresh=()=>assert.fail('covered navigation fetched history');
  for(let index=0;index<=20;index++){
   result.seek(index/20);result.render();assert.equal(result.hours,1);assert.equal(result.viewport().duration,3600);
   for(const chart of result.charts){assert.ok(chart.points.length>0);assert.equal(chart.empty.hidden,true);}
  }
  result.seek(.5);assert.ok(result.viewport().end-result.viewport().start>9*3600);assert.ok(result.covers(result.loadedBounds));result.cancelPending();
 });
 await test('panning moves every shared point by the same distance across an idle boundary',()=>{
  const {result}=rendered(idleRows());result.seek(.5);result.render();
  for(let index=0;index<20;index++){
   const chart=result.charts[0],before=new Map(chart.points.map(point=>[point.packet.id,point.x])),width=chart.bounds.width-chart.bounds.left-chart.bounds.right;
   result.pan(10);result.render();let compared=0;
   for(const point of chart.points)if(before.has(point.packet.id)){assert.ok(Math.abs(point.x-before.get(point.packet.id)+10/3600*width)<1e-5);compared++;}
   assert.ok(compared>0);
  }
  result.cancelPending();
 });
 await test('request duration and in-flight requests never become idle gaps',()=>{
  for(const extra of [{duration_ms:3300000},{state:'streaming'},{state:'captured'}]){
   const {result}=rendered([packet(850,{created:now-3550,...extra}),packet(851,{created:now-100})]);
   assert.equal(result.timeScale().gaps.length,0);
  }
 });
 await test('hidden sessions and requests without metrics still establish activity',()=>{
  const rows=[packet(852,{created:now-3550,thread_id:'session-a',upstream_usage:{input_tokens:100}}),packet(853,{created:now-100,thread_id:'session-a',upstream_usage:{input_tokens:200}}),
   packet(854,{created:now-3400,thread_id:null,duration_ms:3200000}),packet(855,{created:now-200,thread_id:'session-b'})];
  const {result}=rendered(rows);assert.equal(result.timeScale().gaps.length,0);
  result.query='session-a';result.render();assert.equal(result.timeScale().gaps.length,0);assert.equal(result.model().visible.length,1);
 });
 await test('only verified history can be folded and a failed refresh preserves the established scale',()=>{
  const {result}=rendered(idleRows());const original=result.timeScale();assert.equal(original.gaps.length,1);
  result.error=true;assert.equal(result.timeScale(),original);
  result.loadedBounds=null;result.render();assert.equal(result.timeScale().gaps.length,0);
  result.loadedBounds={since:now-3600,before:null};result.render();assert.equal(result.timeScale().gaps.length,0);
  result.loadedBounds={since:now-43201,before:null};result.limited=true;result.render();assert.equal(result.timeScale().gaps.length,1);
  assert.ok(Math.abs(result.timeScale().offset(now-43200)-1)<1e-6);
 });
 await test('line hover remains continuous across a shortened idle interval',()=>{
  const {result}=rendered(idleRows());result.seek(.5);result.render();
  for(const chart of result.charts){
   const gap=chart.bounds.axis.gaps[0],a=chart.points.filter(point=>point.packet.created<gap.start).at(-1),b=chart.points.find(point=>point.packet.created>gap.end);
   assert.ok(a&&b);const x=(chart.bounds.x(gap.start)+chart.bounds.x(gap.end))/2,y=a.y+(b.y-a.y)*(x-a.x)/(b.x-a.x);
   chart.svg.onpointermove({clientX:x,clientY:y});assert.ok(result.hover||result.hoverSeries==='session-a');
   const point=chart.points.at(-1);chart.svg.onpointermove({clientX:point.x,clientY:point.y});assert.equal(result.hover,point.packet.id);assert.equal(result.timeTooltip.hidden,false);
  }
  result.cancelPending();
 });
 await test('Ctrl wheel preserves the actual cursor time across an overnight gap',()=>clocked(()=>{
  const {result,ids,frames}=rendered(idleRows());result.seek(.5);while(frames.length)frames.shift()();
  const wheel=ids.get('trend-charts').events.get('wheel');
  for(const chart of result.charts)for(const deltaY of [120,-120]){
   const point=chart.points.find(point=>point.packet.id===id(809)),bounds=chart.bounds,width=bounds.width-bounds.left-bounds.right,fraction=(point.x-bounds.left)/width;
   const anchor=bounds.axis.timeAt(fraction),rect=chart.svg.getBoundingClientRect(),clientX=rect.left+point.x*rect.width/bounds.width;
   wheel.callback({deltaY,ctrlKey:true,clientX,preventDefault(){}});while(frames.length)frames.shift()();
   assert.ok(Math.abs(chart.bounds.axis.fraction(anchor)-fraction)*width<1e-5);
   assert.ok(Math.abs(result.viewport().duration-result.hours*3600)<1e-5);
  }
  result.cancelPending();
 }));
 await test('resizing and searching never change the global time coordinate',()=>{
  const rows=idleRows().map((row,index)=>({...row,thread_id:index%2?'session-a':'session-b'}));
  const {result}=rendered(rows);result.seek(.5);result.render();const view=result.viewport(),point=result.charts[0].points[0],offset=view.scale.offset(point.packet.created);
  result.query='session-a';for(const chart of result.charts)chart.plot.clientWidth=900;result.render();
  assert.equal(result.viewport().scale.offset(point.packet.created),offset);assert.equal(result.viewport().start,view.start);assert.equal(result.viewport().end,view.end);result.cancelPending();
 });
 await test('clock-aligned ticks and vertical grids move with panning and remain aligned after zooming',()=>clocked(()=>{
  const {result}=rendered(dualRows());result.setRange(3);result.seek(.5);result.render();
  const labels=()=>chartNodes({svg:result.timeAxis},'trend-axis-label'),before=new Map(labels().map(node=>[node.dataset.time,{x:Number(node.attrs.x),text:node.textContent}]));
  const bounds=result.charts[0].bounds,shift=-300/(bounds.end-bounds.start)*(bounds.width-bounds.left-bounds.right);
  result.pan(300);result.render();const common=labels().filter(node=>before.has(node.dataset.time));assert.ok(common.length>=1);
  for(const node of common){assert.equal(node.textContent,before.get(node.dataset.time).text);assert.ok(Math.abs(Number(node.attrs.x)-before.get(node.dataset.time).x-shift)<.00001);}
  result.setRange(2,.4);result.render();
  for(const chart of result.charts){
   const grids=chartNodes(chart,'trend-time-grid');assert.equal(grids.length,labels().length);
   for(let i=0;i<grids.length;i++){
    const stamp=Number(grids[i].dataset.time);assert.equal(new Date(stamp*1000).getSeconds(),0);assert.equal(new Date(stamp*1000).getMinutes()%10,0);
    assert.equal(grids[i].attrs.x1,labels()[i].attrs.x);assert.equal(Number(grids[i].attrs.x1),chart.bounds.x(stamp));
   }
  }
  result.cancelPending();
 }));
 await test('one hover guide spans both panels and stays aligned after a resize',()=>{
  const {result,ids}=rendered(dualRows());ids.get('trend-chart').top=240;
  result.hover=id(101);result.hoverChart='token';result.renderFocus();
  let lines=focusLines(result);assert.equal(lines.length,1);assert.equal(lines[0].attrs.y1,'30');assert.equal(lines[0].attrs.y2,'485');
  for(const chart of result.charts){
   assert.equal(chartNodes(chart,'trend-focus-line').length,0);const nodes=chartNodes(chart,'trend-hover-node');assert.equal(nodes.length,1);
   const point=chart.points.find(point=>point.packet.id===id(101));assert.equal(nodes[0],point.node);assert.equal(nodes[0].tag,'circle');assert.equal(nodes[0].attrs.fill,point.series.color);assert.equal(chart.focusLayer.children.length,0);
  }
  for(const chart of result.charts){chart.plot.clientWidth=900;chart.svg.clientWidth=900;chart.plot.clientHeight=300;chart.svg.clientHeight=300;}
  ids.get('trend-chart').top=300;ids.get('trend-time-axis').top=600;result.guide.clientWidth=900;result.guide.clientHeight=632;
  result.render();lines=focusLines(result);assert.equal(lines.length,1);assert.equal(lines[0].attrs.y2,'605');
  assert.ok(Math.abs(Number(lines[0].attrs.x1)-result.charts[0].points.find(point=>point.packet.id===result.hover).x)<1e-6);
  assert.equal(result.guide.attrs.viewBox,'0 0 900 632');assert.equal(result.timeAxis.attrs.viewBox,'0 0 900 32');
  result.charts[1].svg.onpointerleave();assert.equal(focusLines(result).length,0);
 });
 await test('hover splits time and both metrics into three matching callouts without fabricating missing values',()=>{
  const {result}=rendered(dualRows()),[size,token]=result.charts,missing=size.points[1];
  size.svg.onpointermove({clientX:missing.x,clientY:missing.y});
  assert.equal(result.hover,missing.packet.id);assert.equal(size.tooltip.hidden,false);assert.equal(token.tooltip.hidden,false);
  assert.equal(result.timeTooltip.hidden,false);assert.equal(result.timeTooltip.textContent,result.time(missing.packet.created,true));
  assert.equal(focusLines(result).length,1);
  assert.equal(chartNodes(token,'trend-hover-node').length,0);assert.equal(size.tooltip.children.length,2);
  assert.ok(nodeText(size.tooltip).includes('处理后包'));assert.ok(nodeText(size.tooltip).includes('250 KB'));
  assert.equal(token.tooltip.children[1].textContent,'—');assert.ok(nodeText(token.tooltip).includes('输入 Token'));
  for(const text of ['输入 Token','实际上传','原始包',missing.packet.id,':'])assert.ok(!nodeText(size.tooltip).includes(text));
  for(const chart of result.charts){assert.equal(chart.svg.onclick,undefined);assert.equal(chart.svg.onkeydown,undefined);}
  assert.equal('pinned' in result,false);
  const next=token.points.at(-1);token.svg.onpointermove({clientX:next.x,clientY:next.y});
  assert.equal(result.hover,id(103));assert.equal(token.tooltip.hidden,false);assert.equal(size.tooltip.hidden,false);
  assert.equal(token.tooltip.children.length,2);assert.ok(nodeText(token.tooltip).replace(/\s+/g,' ').includes('输入 Token 300'));
  assert.equal(result.timeTooltip.textContent,result.time(next.packet.created,true));assert.ok(nodeText(size.tooltip).includes('400 KB'));
  assert.equal(chartNodes(size,'trend-hover-node').length,1);
  for(const chart of result.charts){const node=chartNodes(chart,'trend-hover-node')[0];assert.equal(node.dataset.packet,id(103));assert.equal(node.children[0].tag,'path');assert.equal(chart.focusLayer.children.length,0);}
  token.svg.onpointerleave();assert.equal(result.hover,null);assert.equal(result.hoverChart,null);
  for(const chart of result.charts){assert.equal(chart.tooltip.hidden,true);assert.equal(chartNodes(chart,'trend-focus-line').length,0);assert.equal(chartNodes(chart,'trend-hover-node').length,0);}
  assert.equal(focusLines(result).length,0);
  assert.equal(result.timeTooltip.hidden,true);
 });
 await test('metric callouts stay in their own panels and time stays on the axis at both timeline edges',()=>{
  const {result,ids}=rendered([packet(401,{created:now-3599,duration_ms:3598900,upstream_usage:{input_tokens:0},outbound_wire_bytes:0}),
   packet(402,{created:now-.1,upstream_usage:{input_tokens:1000},outbound_wire_bytes:1000000})]);
  ids.get('trend-chart').top=240;result.timeTooltip.offsetWidth=180;
  for(const selected of [id(401),id(402)]){
   result.hover=selected;result.renderFocus();
   for(const chart of result.charts){
    const left=parseFloat(chart.tooltip.style.left),top=parseFloat(chart.tooltip.style.top),rect=chart.svg.getBoundingClientRect();
    assert.ok(left>=8&&left+chart.tooltip.offsetWidth<=result.tooltipHost.clientWidth-8);
    assert.ok(top>=rect.top+6&&top+chart.tooltip.offsetHeight<=rect.top+rect.height-6);
    const px=chart.points.find(point=>point.packet.id===selected).x;assert.equal(left>px,selected===id(401));
   }
   const left=parseFloat(result.timeTooltip.style.left),top=parseFloat(result.timeTooltip.style.top);
   assert.ok(left>=8&&left+result.timeTooltip.offsetWidth<=result.tooltipHost.clientWidth-8);
   assert.ok(top>=480&&top+result.timeTooltip.offsetHeight<=512);
  }
 });
 await test('missing size is shown as missing while a real zero input remains visible',()=>{
  const {result}=rendered([packet(403,{upstream_usage:{input_tokens:0}})]);
  result.hover=id(403);result.renderFocus();const [size,token]=result.charts;
  assert.equal(size.tooltip.hidden,false);assert.equal(size.tooltip.children[1].textContent,'—');
  assert.equal(chartNodes(size,'trend-hover-node').length,0);assert.equal(token.tooltip.children[1].textContent,'0');
  assert.equal(result.timeTooltip.hidden,false);assert.equal(chartNodes(token,'trend-hover-node').length,1);
  result.packets.set(id(403),{...result.packets.get(id(403)),outbound_wire_bytes:0});result.render();
  assert.equal(size.tooltip.children[1].textContent,'0 B');assert.equal(chartNodes(size,'trend-hover-node').length,1);
 });
 await test('a shared sidebar searches and highlights both metrics together',()=>{
  const {result,ids}=rendered([...dualRows(),packet(105,{thread_id:'session-b',created:now-10,upstream_usage:{input_tokens:900},outbound_wire_bytes:900000})]);
  const list=ids.get('trend-series'),a=list.children.find(node=>node.dataset.renderKey==='trend-series:session-a'),b=list.children.find(node=>node.dataset.renderKey==='trend-series:session-b');
  const selected=()=>list.children.filter(node=>node.attrs.class.split(' ').includes('active'));
  const [size,token]=result.charts,first=size.points.find(point=>point.series.id==='session-a'),next=token.points.find(point=>point.series.id==='session-b');
  size.svg.onpointermove({clientX:first.x,clientY:first.y});assert.deepEqual(selected(),[a]);assert.equal(a.attrs['aria-current'],'true');
  token.svg.onpointermove({clientX:next.x,clientY:next.y});assert.deepEqual(selected(),[b]);assert.equal(a.attrs['aria-current'],'false');
  token.svg.onpointerleave();assert.deepEqual(selected(),[]);
  assert.equal(a.onclick,undefined);a.onpointerenter();
  assert.deepEqual(selected(),[a]);
  for(const chart of result.charts){const b=chart.svg.querySelectorAll('[data-series]').find(node=>node.dataset.series==='session-b');assert.ok(b.attrs.class.includes('dimmed'));}
  a.onpointerleave();assert.deepEqual(selected(),[]);for(const chart of result.charts)assert.equal(new Set(chart.points.map(point=>point.series.id)).size,2);
  ids.get('trend-search').oninput({currentTarget:{value:'session-a'}});
  for(const chart of result.charts)assert.ok(chart.points.every(point=>point.series.id==='session-a'));
  ids.get('trend-search').oninput({currentTarget:{value:'missing'}});
  for(const chart of result.charts){assert.equal(chart.points.length,0);assert.equal(chart.empty.hidden,false);}
  ids.get('trend-search').oninput({currentTarget:{value:''}});assert.equal(result.query,'');
  for(const chart of result.charts)assert.equal(new Set(chart.points.map(point=>point.series.id)).size,2);
 });
 await test('all mechanism markers retain their session color and existing priority shapes',()=>{
  const {result}=rendered(eventRows());
  for(const chart of result.charts) {
   const curves=chart.svg.querySelectorAll('[data-series]');assert.equal(chart.eventNodes.length,chart.key==='size'?5:4);
   for(const node of chart.eventNodes) {
    const point=chart.points.find(point=>point.packet.id===node.dataset.packet),curve=curves.find(curve=>curve.dataset.series===point.series.id);
     const color=chartNodes({svg:curve},'trend-curve')[0].attrs.stroke;
     assert.equal(chartNodes({svg:node},'trend-event')[0].attrs.stroke,color);
     for(const counter of chartNodes({svg:node},'trend-event-count'))assert.equal(counter.attrs.fill,color);
    assert.equal(chartNodes({svg:node},'trend-event').length,1);assert.equal(node.attrs.class,'trend-event-node');
     result.hover=point.packet.id;result.renderFocus();assert.equal(chartNodes({svg:node},'trend-event')[0].attrs.stroke,color);
     result.hover=null;result.renderFocus();
   }
   const overlap=chart.eventNodes.find(node=>node.dataset.packet===id(702));
   assert.equal(chartNodes({svg:overlap},'trend-event-overflow').length,1);assert.equal(chartNodes({svg:overlap},'trend-event-count').length,0);
   assert.equal(overlap.dataset.events,'overflow retry');
  }
 });
 await test('hovering each mechanism legend highlights matching nodes in both charts without redrawing',()=>{
  const {result,ids}=rendered(eventRows()),expected={predictive:[701,706],overflow:[702],retry:[702,703,706],compact:[704]};
  result.services.api=()=>assert.fail('legend hover fetched data');result.render=()=>assert.fail('legend hover rebuilt charts');
  result.hover=id(705);result.hoverChart='token';result.renderFocus();
  for(const [key,numbers] of Object.entries(expected)) {
   const button=ids.get('trend-event-'+key);button.onpointerenter();assert.equal(result.hover,null);assert.equal(result.hoverChart,null);assert.ok(button.attrs.class.includes('highlighted'));
   for(const chart of result.charts) {
    const present=numbers.filter(number=>chart.points.some(point=>point.packet.id===id(number)));
    assert.ok(chart.svg.attrs.class.includes('trend-event-filtering'));
    assert.deepEqual(Array.from(chart.eventNodes.filter(node=>node.attrs.class.includes('highlighted')),node=>node.dataset.packet).sort(),present.map(id).sort());
    assert.equal(chart.eventNodes.filter(node=>node.attrs.class.includes('dimmed')).length,chart.eventNodes.length-present.length);
    assert.equal(chart.svg.querySelectorAll('[data-series]').filter(node=>node.attrs.class.includes('dimmed')).length,0);
    assert.equal(chart.tooltip.hidden,true);
     for(const node of chart.eventNodes){const point=chart.points.find(point=>point.packet.id===node.dataset.packet);assert.equal(chartNodes({svg:node},'trend-event')[0].attrs.stroke,point.series.color);}
   }
   assert.equal(result.timeTooltip.hidden,true);button.onpointerleave();
   for(const chart of result.charts){assert.ok(!chart.svg.attrs.class.includes('trend-event-filtering'));assert.ok(chart.eventNodes.every(node=>node.attrs.class==='trend-event-node'));}
  }
 });
 await test('keyboard focus highlights mechanisms and switching views resets highlight state',()=>{
  const {result,ids}=rendered(eventRows()),predictive=ids.get('trend-event-predictive'),retry=ids.get('trend-event-retry');
  predictive.onfocus();assert.equal(result.eventKey(),'predictive');retry.onpointerenter();assert.equal(result.eventKey(),'retry');
  retry.onpointerleave();assert.equal(result.eventKey(),'predictive');predictive.onblur();assert.equal(result.eventKey(),null);
  retry.onfocus();result.setActive(false);assert.equal(result.eventKey(),null);result.setActive(true);
  for(const chart of result.charts){assert.ok(!chart.svg.attrs.class.includes('trend-event-filtering'));assert.ok(chart.eventNodes.every(node=>node.attrs.class==='trend-event-node'));}
  assert.equal(result.eventLegend.filter(({button})=>button.attrs.class.includes('highlighted')).length,0);
 });
 await test('mechanism highlights survive live SVG publication and missing usage without bright flashes',()=>{
  const {result,ids}=rendered([...eventRows(),packet(707,{usage_override_applied:true,outbound_wire_bytes:700000})]);
  ids.get('trend-event-predictive').onpointerenter();let published=0;
  for(const chart of result.charts){const replace=chart.svg.replaceChildren.bind(chart.svg);chart.svg.replaceChildren=fragment=>{
   assert.ok(chart.svg.attrs.class.includes('trend-event-filtering'));
   for(const node of chartNodes({svg:fragment},'trend-event-node')){
    assert.equal(node.attrs.class.includes('highlighted'),node.dataset.events.split(' ').includes('predictive'));
    assert.equal(node.attrs.class.includes('dimmed'),!node.dataset.events.split(' ').includes('predictive'));
   }
   published++;replace(fragment);
  };}
  for(let index=0;index<3;index++){
   const next=packet(708+index,{thread_id:'session-b',usage_override_applied:true,upstream_usage:{input_tokens:900+index},outbound_wire_bytes:800000+index});
   result.packets.set(next.id,next);result.render();
  }
  assert.equal(published,6);assert.ok(result.charts[0].eventNodes.some(node=>node.dataset.packet===id(707)));assert.ok(!result.charts[1].eventNodes.some(node=>node.dataset.packet===id(707)));
  ids.get('trend-event-predictive').onpointerleave();for(const chart of result.charts)assert.equal(chartNodes(chart,'dimmed').length,0);
 });
 await test('one metric can render before the other and accepts later real zero usage',()=>{
  const row=packet(101,{outbound_wire_bytes:0}),{result}=rendered([row]),[size,token]=result.charts;
  assert.equal(size.points.length,1);assert.equal(size.points[0].value,0);assert.equal(size.empty.hidden,true);
  assert.equal(token.points.length,0);assert.equal(token.empty.hidden,false);
  result.packets.set(row.id,{...row,upstream_usage:{input_tokens:0}});result.render();
  assert.equal(token.points.length,1);assert.equal(token.points[0].value,0);assert.equal(token.empty.hidden,true);
  result.packets.set(row.id,{...row,upstream_usage:{input_tokens:100},outbound_wire_bytes:null});result.render();
  assert.equal(size.empty.hidden,false);assert.equal(token.empty.hidden,true);
 });
 await test('range controls and resize redraw both charts using the same domain',()=>{
  const {result,ids,frames}=rendered([...dualRows(),packet(106,{created:now-7200,upstream_usage:{input_tokens:900},outbound_wire_bytes:900000})]);
  result.refresh=()=>assert.fail('zoom must not fetch history');
  const slider=ids.get('trend-range-slider');result.setRange(3);assert.equal(result.hours,3);assert.ok(slider.attrs['aria-valuetext'].includes('3 小时'));assert.equal(ids.has('trend-range-value'),false);frames.shift()();
  for(const chart of result.charts)assert.ok(chart.points.some(point=>point.packet.id===id(106)));
  slider.value='1';slider.oninput();assert.equal(result.hours,3);result.setRange(1);result.seek(1);frames.shift()();
  assert.equal(result.viewport().duration,3600);
  for(const chart of result.charts){assert.ok(chart.bounds.end-chart.bounds.start>3600);assert.ok(chart.points.some(point=>point.packet.id===id(106)));chart.plot.clientWidth=800;chart.plot.clientHeight=260;}
  result.resize.callback();frames.shift()();
  for(const chart of result.charts)assert.equal(chart.svg.attrs.viewBox,'0 0 800 260');
  assert.equal(result.charts[0].bounds.x(now-100),result.charts[1].bounds.x(now-100));
  const point=result.charts[0].points[0];result.charts[0].svg.onpointermove({clientX:point.x,clientY:point.y});
  result.emphasis='session-a';result.setActive(false);
  assert.equal(result.hover,null);assert.equal(result.hoverChart,null);assert.equal(result.emphasis,null);
  for(const chart of result.charts)assert.equal(chart.tooltip.hidden,true);
  assert.equal(result.timeTooltip.hidden,true);
  result.refresh=()=>{};result.setActive(true);
  for(const chart of result.charts){assert.equal(chart.tooltip.hidden,true);assert.equal(chartNodes(chart,'trend-focus-line').length,0);}
  assert.equal(result.timeTooltip.hidden,true);
 });
 await test('live refresh never publishes bright background curves while a point is hovered',()=>{
  const {result,events}=rendered([...dualRows(),packet(201,{thread_id:'session-b',created:now-5,upstream_usage:{input_tokens:500},outbound_wire_bytes:500000})]);
  result.hover=id(101);result.hoverChart='token';result.renderFocus();let published=0;
  for(const chart of result.charts){const replace=chart.svg.replaceChildren.bind(chart.svg);chart.svg.replaceChildren=(fragment)=>{
   for(const node of fragment.querySelectorAll('[data-series]'))assert.equal((node.attrs.class||'').includes('dimmed'),node.dataset.series!=='session-a','curve brightness changed during SVG publication');
   published++;replace(fragment);
  };}
  for(let index=0;index<5;index++){
   const next=packet(202+index,{thread_id:'session-b',created:now+index/10,upstream_usage:{input_tokens:600+index},outbound_wire_bytes:600000+index});
   result.packets.set(next.id,next);result.render();assert.equal(result.hover,id(101));
   assert.equal(focusLines(result).length,1);
  }
  assert.equal(published,10);
  events.get('pointermove')({target:result.charts[0].svg});assert.equal(result.hover,id(101));
  events.get('pointermove')({target:new Node('header')});assert.equal(result.hover,null);assert.equal(result.hoverChart,null);
  assert.equal(result.guide.children.length,0);assert.equal(result.timeTooltip.hidden,true);
  for(const chart of result.charts){assert.equal(chart.tooltip.hidden,true);assert.equal(chartNodes(chart,'trend-hover-node').length,0);}
 });
 await test('native backfill and sidebar emphasis keep redraw brightness stable until hover ends',async()=>{
  const pending=packet(301,{created:now-10,outbound_wire_bytes:200000});
  const {result,frames}=rendered([pending,packet(302,{thread_id:'session-b',created:now-5,upstream_usage:{input_tokens:500},outbound_wire_bytes:500000})]);
  result.hover=pending.id;result.hoverChart='size';result.renderFocus();let active='session-a',published=0;
  for(const chart of result.charts){const replace=chart.svg.replaceChildren.bind(chart.svg);chart.svg.replaceChildren=(fragment)=>{
   for(const node of fragment.querySelectorAll('[data-series]'))assert.equal((node.attrs.class||'').includes('dimmed'),!!active&&node.dataset.series!==active,'backfill or sidebar redraw changed curve brightness');
   published++;replace(fragment);
  };}
  result.services.read=async()=>({state:'available',input_tokens:1234});await result.hydrate();frames.shift()();
  assert.equal(result.charts[1].points.find(point=>point.packet.id===pending.id).input,1234);assert.equal(result.hover,pending.id);
  active='session-b';result.emphasis=active;result.render();
  active=null;result.emphasis=null;result.charts[0].svg.onpointerleave();result.render();
  for(const chart of result.charts)assert.equal(chartNodes(chart,'dimmed').length,0);
  assert.equal(published,6);
 });
 await test('hovering a long segment highlights its session across redraws without inventing a sample',()=>{
  const {result,ids,events}=rendered([
   packet(401,{created:now-600,upstream_usage:{input_tokens:100},outbound_wire_bytes:100000}),
   packet(402,{created:now-300,outbound_wire_bytes:200000}),
   packet(403,{created:now-1,upstream_usage:{input_tokens:300},outbound_wire_bytes:300000}),
   packet(404,{thread_id:'session-b',created:now-600,upstream_usage:{input_tokens:500},outbound_wire_bytes:500000}),
   packet(405,{thread_id:'session-b',created:now-1,upstream_usage:{input_tokens:500},outbound_wire_bytes:500000})
  ]),token=result.charts[1],[a,b]=token.points.filter(p=>p.series.id==='session-a');
  const x=(a.x+b.x)/2,y=(a.y+b.y)/2;assert.ok(Math.hypot(x-a.x,y-a.y)>28);
  token.svg.onpointermove({clientX:x,clientY:y});
  assert.equal(result.hover,null);assert.equal(result.hoverSeries,'session-a');
  assert.deepEqual(ids.get('trend-series').children.filter(n=>n.attrs.class.split(' ').includes('active')).map(n=>n.dataset.renderKey),['trend-series:session-a']);
  for(const chart of result.charts){assert.equal(chart.tooltip.hidden,true);assert.equal(chartNodes(chart,'dimmed').length,1);}
  assert.equal(result.timeTooltip.hidden,true);assert.equal(focusLines(result).length,0);
  for(const chart of result.charts){const replace=chart.svg.replaceChildren.bind(chart.svg);chart.svg.replaceChildren=fragment=>{
   for(const line of fragment.querySelectorAll('[data-series]'))assert.equal(line.attrs.class.includes('dimmed'),line.dataset.series!=='session-a');replace(fragment);
  };}
  result.render();assert.equal(result.hoverSeries,'session-a');
  events.get('pointermove')({target:new Node('header')});assert.equal(result.hoverSeries,null);
  for(const chart of result.charts)assert.equal(chartNodes(chart,'dimmed').length,0);
  token.svg.onpointermove({clientX:x,clientY:y});result.setActive(false);assert.equal(result.hoverSeries,null);
 });
 await test('line hits use screen distance, choose the closest series and never bridge sessions',()=>{
  let {result}=rendered([
   packet(501,{created:now-3599,duration_ms:3598000,upstream_usage:{input_tokens:100},outbound_wire_bytes:100000}),
   packet(502,{created:now-1,upstream_usage:{input_tokens:100},outbound_wire_bytes:100000}),
   packet(503,{thread_id:'session-b',created:now-3599,upstream_usage:{input_tokens:105},outbound_wire_bytes:105000}),
   packet(504,{thread_id:'session-b',created:now-1,upstream_usage:{input_tokens:105},outbound_wire_bytes:105000})
  ]);
  let token=result.charts[1];
  for(const id of ['session-a','session-b']){const [a,b]=token.points.filter(p=>p.series.id===id);token.svg.onpointermove({clientX:(a.x+b.x)/2,clientY:a.y});assert.equal(result.hoverSeries,id);assert.equal(result.hover,null);}
  ({result}=rendered([
   packet(505,{created:now-3599,duration_ms:3598000,upstream_usage:{input_tokens:100}}),
   packet(506,{created:now-1,upstream_usage:{input_tokens:100}})
  ]));
  token=result.charts[1];token.svg.clientWidth/=2;token.svg.clientHeight/=2;
  let [a,b]=token.points,x=(a.x+b.x)/4,y=a.y/2;
  token.svg.onpointermove({clientX:x,clientY:y+6});assert.equal(result.hoverSeries,'session-a');
  token.svg.onpointermove({clientX:x,clientY:y+10});assert.equal(result.hoverSeries,null);
  ({result}=rendered([
   packet(507,{created:now-3599,duration_ms:3598000,upstream_usage:{input_tokens:100}}),
   packet(508,{thread_id:'session-b',created:now-1,upstream_usage:{input_tokens:300}})
  ]));
  token=result.charts[1];[a,b]=token.points;token.svg.onpointermove({clientX:(a.x+b.x)/2,clientY:(a.y+b.y)/2});
  assert.equal(result.hoverSeries,null);assert.equal(result.hover,null);
 });
 await test('plain wheel pans and Ctrl wheel zooms locally without canceling covered history',()=>{
  const {result,ids,frames}=rendered(dualRows()),slider=ids.get('trend-range-slider');
  result.controller=new AbortController();result.loading=true;result.refresh=()=>assert.fail('local zoom performed network work');
  let prevented=0;const wheel=ids.get('trend-charts').events.get('wheel');assert.equal(wheel.options.passive,false);
  wheel.callback({deltaY:-120,deltaMode:0,preventDefault(){prevented++;}});
  assert.equal(result.hours,1);assert.ok(Number(slider.value)<1);assert.equal(result.controller.signal.aborted,false);assert.equal(prevented,1);frames.shift()();
  const thumb=parseFloat(slider.style['--trend-thumb-width']);wheel.callback({deltaY:120,ctrlKey:true,preventDefault(){prevented++;}});
  assert.ok(result.hours>1);assert.ok(parseFloat(slider.style['--trend-thumb-width'])>thumb);assert.equal(prevented,2);frames.shift()();
  result.setRange(100);assert.equal(result.hours,12);result.setRange(-10);assert.equal(result.hours,1);
  result.oldestCreated=context.Date.now()/1000-7200;result.setRange(100);assert.ok(result.hours>=2&&result.hours<2.01);assert.equal(Number(slider.max),1);
  const before=result.hours;wheel.callback({deltaY:120,metaKey:true,preventDefault(){assert.fail('system zoom was intercepted');}});assert.equal(result.hours,before);
 });
 await test('zooming in and out preserves the cursor time in either metric panel',()=>clocked(()=>{
  const {result,ids,frames}=rendered(dualRows());result.seek(.5);while(frames.length)frames.shift()();
  const wheel=ids.get('trend-charts').events.get('wheel');
  for(const chart of result.charts)for(const deltaY of [120,-120,200,-200]){
   const fraction=.23,rect=chart.svg.getBoundingClientRect(),bounds=chart.bounds,anchor=bounds.start+(bounds.end-bounds.start)*fraction;
   const clientX=rect.left+(bounds.left+(bounds.width-bounds.left-bounds.right)*fraction)*rect.width/bounds.width;
   wheel.callback({deltaY,ctrlKey:true,clientX,target:chart.svg,preventDefault(){}});while(frames.length)frames.shift()();
   assert.ok(Math.abs(chart.bounds.start+(chart.bounds.end-chart.bounds.start)*fraction-anchor)<.00001);
  }
  result.cancelPending();
 }));
 await test('the scrollbar thumb tracks duration while dragging only changes time position',()=>clocked(()=>{
  const {result,ids,frames}=rendered(dualRows()),slider=ids.get('trend-range-slider');
  result.setRange(4);const thumb=parseFloat(slider.style['--trend-thumb-width']),span=result.timeline().end-result.timeline().start;
  assert.ok(Math.abs(thumb-slider.clientWidth*4*3600/span)<.00001);
  for(const position of [0,.35,1]){
   slider.value=String(position);slider.oninput();while(frames.length)frames.shift()();
   assert.equal(result.hours,4);assert.ok(Math.abs(Number(slider.value)-position)<.00001);assert.equal(parseFloat(slider.style['--trend-thumb-width']),thumb);
   for(const chart of result.charts)assert.equal(chart.bounds.end-chart.bounds.start,4*3600);
  }
  result.oldestCreated=now-7200;result.setRange(12);assert.ok(result.hours>2&&result.hours<2.01);assert.equal(parseFloat(slider.style['--trend-thumb-width']),slider.clientWidth);
  result.pan(-1000000);assert.equal(result.viewport().end,now);result.cancelPending();
 }));
 await test('Ctrl wheel over the scrollbar resizes around the hovered thumb position',()=>clocked(()=>{
  const {result,ids}=rendered(dualRows()),slider=ids.get('trend-range-slider');result.setRange(4);result.seek(.5);
  const view=result.viewport(),fraction=.7,thumb=parseFloat(slider.style['--trend-thumb-width']),anchor=view.start+(view.end-view.start)*fraction;
  const clientX=slider.getBoundingClientRect().left+(slider.clientWidth-thumb)*Number(slider.value)+thumb*fraction;
  const wheel=ids.get('trend-range').events.get('wheel');assert.equal(wheel.options.passive,false);
  wheel.callback({deltaY:120,ctrlKey:true,clientX,preventDefault(){}});const next=result.viewport();
  assert.ok(result.hours>4);assert.ok(parseFloat(slider.style['--trend-thumb-width'])>thumb);assert.ok(Math.abs(next.start+(next.end-next.start)*fraction-anchor)<.00001);
  const hours=result.hours;wheel.callback({deltaY:-80,preventDefault(){}});assert.equal(result.hours,hours);assert.ok(result.viewport().end<next.end);result.cancelPending();
 }));
 await test('horizontal wheel deltas and line/page units pan without changing duration',()=>clocked(()=>{
  const {result,ids}=rendered(dualRows());result.seek(.5);const wheel=ids.get('trend-charts').events.get('wheel');
  const initial=result.viewport().end;wheel.callback({deltaX:-100,deltaY:0,preventDefault(){}});assert.ok(result.viewport().end<initial);assert.equal(result.hours,1);
  const before=result.viewport().end;wheel.callback({deltaY:1,deltaMode:1,preventDefault(){}});assert.ok(result.viewport().end>before);
  result.pan(-1000000);assert.equal(result.viewport().start,result.timeline().start);result.pan(1000000);assert.equal(result.windowEnd,null);
  wheel.callback({deltaY:-1,deltaMode:2,preventDefault(){}});assert.ok(result.viewport().end<now);assert.equal(result.hours,1);result.cancelPending();
 }));
 await test('a compact history loads its full timeline once, including idle periods longer than twelve hours',async()=>{
  const queries=[],rows=idleRows().map((row,index)=>({...row,created:index<13?row.created-24*3600:row.created}));
  const oldest=Math.min(...rows.map(row=>row.created));
  const {result}=rendered([],{status:()=>({storage:{revision:1,count:rows.length,oldest_created:oldest},trend_packets_supported:true}),api:async path=>{queries.push(new URL(path,'http://fixture').searchParams);return rows;}});
  await result.refresh(true);assert.equal(queries.length,1);assert.equal(Number(queries[0].get('since')),oldest-1);assert.equal(queries[0].get('before'),null);assert.equal(result.packets.size,rows.length);
  for(const position of [0,.25,.5,.75,1]){result.seek(position);result.render();for(const chart of result.charts)assert.equal(chart.empty.hidden,true);}
  assert.equal(queries.length,1);result.cancelPending();
 });
 await test('large histories query bounded pages around older viewports',()=>clocked(async()=>{
  const queries=[],old=packet(1301,{created:now-18.5*3600,upstream_usage:{input_tokens:100},outbound_wire_bytes:1000}),recent=packet(1302,{created:now-60,upstream_usage:{input_tokens:200},outbound_wire_bytes:2000});
  const {result}=rendered([recent],{status:()=>({storage:{revision:1,count:50000,oldest_created:now-48*3600}}),api:async path=>{queries.push(new URL(path,'http://fixture').searchParams);return[recent,old];}});
  result.pan(-18*3600);await result.refresh(true);assert.equal(queries.length,1);assert.equal(queries[0].get('limit'),'50');
  assert.ok(Number(queries[0].get('before'))<now-12*3600);assert.ok(Math.abs(Number(queries[0].get('before'))-Number(queries[0].get('since'))-12*3600)<.01);
  for(const chart of result.charts)assert.deepEqual(Array.from(chart.points,point=>point.packet.id),[old.id]);
  result.seek(1);await result.refresh(true);assert.equal(queries.length,2);assert.equal(queries[1].get('before'),null);
  for(const chart of result.charts)assert.deepEqual(Array.from(chart.points,point=>point.packet.id),[recent.id]);result.cancelPending();
 }));
 await test('returning to cached recent data cancels an obsolete history read and ignores its late result',()=>clocked(async()=>{
  let release,entered,reads=0;const started=new Promise(resolve=>entered=resolve),recent=packet(1310,{created:now-60,upstream_usage:{input_tokens:100},outbound_wire_bytes:1000});
  const {result}=rendered([recent],{status:()=>({storage:{revision:1,count:50000,oldest_created:now-48*3600}}),api:()=>{reads++;entered();return new Promise(resolve=>release=resolve);}});
  result.pan(-18*3600);const pending=result.refresh(true);await started;const controller=result.controller;
  result.seek(1);assert.equal(controller.signal.aborted,true);assert.equal(result.windowTimer,null);
  release([packet(1311,{created:now-18.5*3600})]);await pending;assert.equal(reads,1);assert.ok(result.packets.has(recent.id));assert.equal(result.error,false);
  result.render();for(const chart of result.charts)assert.deepEqual(Array.from(chart.points,point=>point.packet.id),[recent.id]);result.cancelPending();
 }));
 await test('a failed historical read keeps its cache and reports failure instead of claiming no requests',()=>clocked(async()=>{
  const recent=packet(1320,{created:now-60,upstream_usage:{input_tokens:100},outbound_wire_bytes:1000});
  const {result}=rendered([recent],{status:()=>({storage:{revision:1,oldest_created:now-48*3600}}),api:async()=>{throw Error('offline');}});
  result.pan(-18*3600);result.render();for(const chart of result.charts)assert.equal(chart.empty.textContent,'正在加载趋势…');
  await result.refresh(true);assert.equal(result.error,true);assert.ok(result.packets.has(recent.id));
  for(const chart of result.charts){assert.equal(chart.empty.hidden,false);assert.equal(chart.empty.textContent,'暂时无法加载趋势，请稍后重试');}
  const old=packet(1321,{created:now-18.5*3600,upstream_usage:{input_tokens:200},outbound_wire_bytes:2000});
  result.services.api=async()=>[old];await result.refresh(true);assert.equal(result.error,false);for(const chart of result.charts)assert.equal(chart.empty.hidden,true);result.cancelPending();
 }));
 await test('rapid historical navigation debounces reads and leaving trends cancels pending work',()=>clocked(async()=>{
  let reads=0;const {result}=rendered([],{status:()=>({storage:{revision:1,oldest_created:now-48*3600}})});result.refresh=async()=>{reads++;};
  result.pan(-18*3600);result.pan(-3600);result.pan(-3600);assert.equal(reads,0);await new Promise(resolve=>setTimeout(resolve,150));assert.equal(reads,1);
  result.pan(-3600);result.setActive(false);await new Promise(resolve=>setTimeout(resolve,150));assert.equal(reads,1);assert.equal(result.windowTimer,null);
 }));
 await test('history remains fixed during live refresh and the right edge resumes following new requests',()=>clocked(advance=>{
  const {result,ids}=rendered(dualRows());result.seek(.5);const end=result.viewport().end;
  advance(5*60000);result.render();assert.equal(result.viewport().end,end);assert.ok(Number(ids.get('trend-range-slider').value)<1);
  result.seek(1);assert.equal(result.windowEnd,null);advance(5*60000);result.render();assert.equal(result.viewport().end,now+600);
  result.seek(0);result.oldestCreated=now-3*3600;result.render();assert.equal(result.viewport().start,result.oldestCreated-1);result.cancelPending();
 }));
 await test('first paint precedes history loading and pending activation can be canceled',async()=>{
  let reads=0;const {result,frames}=rendered([],{api:async()=>{reads++;return[];}});
  result.setActive(false);result.setActive(true);assert.equal(reads,0);await result.refresh();assert.equal(reads,0);
  result.setActive(false);frames.shift()();await new Promise(resolve=>setTimeout(resolve,5));assert.equal(reads,0);
  result.setActive(true);assert.equal(reads,0);frames.shift()();await new Promise(resolve=>setTimeout(resolve,5));assert.equal(reads,1);
 });
 await test('progressive pages paint before completion, zoom locally and roll back a failed snapshot',async()=>{
  let release,entered,reads=0;const second=new Promise(resolve=>entered=resolve),old=packet(900,{created:now-10,upstream_usage:{input_tokens:100}});
  const {result,frames}=rendered([old],{api:async()=>{if(++reads===1)return Array.from({length:300},(_,i)=>packet(i+1));entered();return new Promise(resolve=>release=resolve);}});
  const loading=result.refresh(true);await second;assert.equal(result.packets.size,1);assert.equal(result.previewPackets.size,301);
  frames.shift()();assert.equal(result.model().series[0].points.length,301);
  result.setRange(4);assert.equal(result.loading,true);assert.equal(result.controller.signal.aborted,false);frames.shift()();assert.equal(reads,2);
  release({invalid:true});await loading;assert.equal(result.error,true);assert.equal(result.previewPackets,null);assert.equal(result.packets.size,1);assert.ok(result.packets.has(old.id));
 });
 await test('old backends use small pages and discard response payloads while retaining real usage',async()=>{
  const queries=[],rows=Array.from({length:53},(_,i)=>packet(i+1,{upstream_usage:{input_tokens:i,payload:'x'.repeat(10000)},reported_usage:{input_tokens:500000},capacity_retry_diagnostic:{events:['private']}}));
  const result=view({status:()=>({storage:{revision:1}}),api:async path=>{
   const q=new URL(path,'http://fixture').searchParams;queries.push(q);if(q.get('direction')==='asc')return[rows.at(-1)];const start=q.has('after')?rows.findIndex(p=>p.id===q.get('after'))+1:0;return rows.slice(start,start+Number(q.get('limit')));
  }});
  await result.refresh();assert.equal(result.packets.size,53);assert.equal(queries.length,3);assert.equal(queries[0].get('direction'),'asc');assert.equal(queries[0].get('limit'),'1');
  for(const q of queries.slice(1)){assert.equal(q.get('limit'),'50');assert.equal(q.get('view'),'trend');assert.ok(Number(q.get('since'))<=rows.at(-1).created);assert.equal(q.get('before'),null);}
  for(const row of result.packets.values()){assert.deepEqual(Object.keys(row.upstream_usage),['input_tokens']);assert.equal(row.reported_usage,undefined);assert.equal(row.capacity_retry_diagnostic,undefined);}
 });
 await test('dense curves retain every hover point and event marker with bounded ordinary dots',()=>{
  const rows=Array.from({length:12000},(_,i)=>packet(i+1,{created:now-i/4,upstream_usage:{input_tokens:1000+i},outbound_wire_bytes:200000+i,...(i%1000===0?{usage_override_applied:true}: {})}));
  const {result}=rendered(rows);
  for(const chart of result.charts){assert.equal(chart.points.length,12000);assert.equal(chartNodes(chart,'trend-event-predictive').length,12);assert.ok(chart.svg.all(n=>n.tag==='circle').length<500);assert.equal(chartNodes(chart,'trend-curve').length,1);}
 });
 await test('status metadata fixes the range before history arrives and a short page cannot shrink it',async()=>{
  let release,entered,reads=0;const started=new Promise(resolve=>entered=resolve),oldest=now-5.6*3600;
  const {result,ids}=rendered([],{status:()=>({storage:{revision:1,oldest_created:oldest},trend_packets_supported:true}),api:()=>{reads++;entered();return new Promise(resolve=>release=resolve);}});
  const slider=ids.get('trend-range-slider');assert.equal(slider.disabled,false);assert.ok(result.rangeLimit()>=5.6&&result.rangeLimit()<5.61);assert.equal(reads,0);
  const loading=result.refresh();await started;result.setRange(12);const selected=result.hours;
  release([packet(1201,{created:now-60})]);await loading;
  assert.ok(result.hours>=selected&&result.hours<selected+.001);assert.ok(result.rangeLimit()>5.6);assert.equal(reads,1);
 });
 await test('legacy range lookup finishes before paged history and unknown bounds cannot zoom to twelve hours',async()=>{
  let rangeRelease,pageRelease,enteredRange,enteredPage;const rangeStarted=new Promise(resolve=>enteredRange=resolve),pageStarted=new Promise(resolve=>enteredPage=resolve),queries=[];
  const {result,ids}=rendered([],{status:()=>({storage:{revision:1}}),api:path=>{
   const q=new URL(path,'http://fixture').searchParams;queries.push(q);
   if(q.get('direction')==='asc'){enteredRange();return new Promise(resolve=>rangeRelease=resolve);}
   enteredPage();return new Promise(resolve=>pageRelease=resolve);
  }});
  const slider=ids.get('trend-range-slider');assert.equal(result.boundsReady,false);
  assert.equal(slider.disabled,true);assert.equal(Number(slider.max),1);result.setRange(12);assert.equal(result.hours,1);
  ids.get('trend-charts').events.get('wheel').callback({deltaY:120,preventDefault(){assert.fail('unknown bounds intercepted zoom');}});assert.equal(result.hours,1);
  let prevented=false;ids.get('trend-charts').events.get('wheel').callback({deltaY:120,ctrlKey:true,preventDefault(){prevented=true;}});assert.equal(prevented,true);assert.equal(result.hours,1);
  const loading=result.refresh();await rangeStarted;assert.equal(queries.length,1);assert.equal(slider.disabled,true);
  rangeRelease([packet(1202,{created:now-5.6*3600})]);await pageStarted;
  assert.equal(slider.disabled,false);assert.ok(result.rangeLimit()>5.6&&result.rangeLimit()<5.61);result.setRange(12);
  pageRelease([packet(1203,{created:now-30})]);await loading;assert.ok(result.hours>5.6&&result.hours<5.61);assert.equal(queries.length,2);
 });
 await test('canceled range discovery cannot overwrite newer metadata',async()=>{
  let release;const result=view({status:()=>({storage:{revision:1}}),api:()=>new Promise(resolve=>release=resolve)});result.boundsReady=false;
  const old=result.refresh();result.cancelPending();const expected=now-3*3600;
  result.services.status=()=>({storage:{revision:2,oldest_created:expected}});result.services.api=async()=>[packet(1204)];
  await result.refresh();release([packet(1205,{created:now-10*3600})]);await old;
  assert.equal(result.oldestCreated,expected);assert.equal(result.boundsReady,true);assert.equal(result.error,false);
 });
 await test('empty and failed range discovery stay bounded and can recover without a guessed maximum',async()=>{
  const {result,ids}=rendered([],{status:()=>({storage:{revision:1}}),api:async()=>{throw Error('offline');}});
  await result.refresh();assert.equal(result.error,true);assert.equal(ids.get('trend-range-slider').disabled,true);assert.equal(result.rangeLimit(),1);
  result.services.api=async()=>[];await result.refresh();assert.equal(result.error,false);assert.equal(result.boundsReady,true);assert.equal(result.rangeLimit(),1);assert.equal(ids.get('trend-range-slider').disabled,false);
  result.services.status=()=>({storage:{revision:2,oldest_created:now-24*3600}});await result.refresh();assert.equal(result.rangeLimit(),12);
 });
 console.log(JSON.stringify({passed:checks}));
})().catch(error=>{console.error(error);process.exitCode=1;});
