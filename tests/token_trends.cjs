/* Synthetic-only contracts for the chart's data flow, cancellation and usage sources. */
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('packet_inspector/ui/app.js','utf8');
const classSource=source.slice(source.indexOf('function isTrendGeneration('),source.indexOf('tokenTrendView=new TokenTrendView('));
const helpers=source.split(/\r?\n/).filter(line=>/^function (tokenVersion|compressedBytes|originalBytes|uploadBytes|compact)\(/.test(line)).join('\n');
const context={Date,Map,Set,AbortController,setTimeout,clearTimeout,URLSearchParams,document:{hidden:false},
  purposeFromMetadata:p=>p.session_kind||null,hasModelTokenUsage:p=>p.method==='POST'&&!p.is_compaction&&p.request_kind!=='tool_search',
  tokenVersion:null};
vm.createContext(context);vm.runInContext(helpers+'\n'+classSource+'\nglobalThis.View=TokenTrendView;globalThis.isGeneration=isTrendGeneration;',context);
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
class Node {
 constructor(tag){this.tag=tag;this.attrs={};this.children=[];this.dataset={};this.textContent='';this.hidden=true;this.clientWidth=640;this.clientHeight=240;this.offsetWidth=260;this.offsetHeight=150;
  this.style={setProperty:(key,value)=>{this.style[key]=value;}};
  this.classList={toggle:(key,on)=>{const names=new Set((this.attrs.class||'').split(' ').filter(Boolean));on?names.add(key):names.delete(key);this.attrs.class=[...names].join(' ');}};}
 setAttribute(key,value){this.attrs[key]=String(value);if(key.startsWith('data-'))this.dataset[key.slice(5).replace(/-([a-z])/g,(_,char)=>char.toUpperCase())]=String(value);}
 append(...nodes){for(const node of nodes)this.children.push(...(node.tag==='#fragment'?node.children:[node]));}
 prepend(node){this.children.unshift(node);}
 replaceChildren(...nodes){this.children=[];this.append(...nodes);}
 all(test){return this.children.flatMap(child=>[...(test(child)?[child]:[]),...child.all(test)]);}
 querySelectorAll(){return this.all(node=>node.attrs['data-series']!==undefined);}
 getBoundingClientRect(){return {left:0,top:0,width:this.clientWidth,height:this.clientHeight};}
}
function rendered(rows){
 const ids=new Map(),html=fs.readFileSync('packet_inspector/ui/index.html','utf8'),frames=[];
 for(const match of html.matchAll(/<(\w+)\b[^>]*\bid="([^"]+)"[^>]*>/g))ids.set(match[2],new Node(match[1]));
 ids.get('trend-charts').clientHeight=492;
 const buttons=[1,6,24,0].map(hours=>{const node=new Node('button');node.dataset.hours=String(hours);return node;});
 context.document={hidden:false,createDocumentFragment:()=>new Node('#fragment'),createElementNS:(_,tag)=>new Node(tag),
  createTextNode:text=>{const node=new Node('#text');node.textContent=text;return node;},querySelectorAll:selector=>selector==='#trend-range button'?buttons:[]};
 context.$=id=>ids.get(id);context.t=text=>text;context.nf=new Intl.NumberFormat('en-US');context.uiI18n={language:'en'};
 context.el=(tag,cls='',text='')=>{const node=new Node(tag);node.attrs.class=cls;node.textContent=text;return node;};
 context.reconcileContent=(target,fragment)=>target.replaceChildren(fragment);
 context.localStorage={getItem:()=>null,setItem(){}};
 context.ResizeObserver=class{constructor(callback){this.callback=callback;this.observed=[];}observe(node){this.observed.push(node);}};
 context.requestAnimationFrame=callback=>{frames.push(callback);return frames.length;};
 const result=new View(view().services);result.active=true;result.hours=0;result.packets=new Map(rows.map(row=>[row.id,row]));
 result.render();return {result,ids,buttons,frames};
}
function chartNodes(chart,cls){return chart.svg.all(node=>(node.attrs.class||'').split(' ').includes(cls));}
function nodeText(node){return node.textContent+node.children.map(nodeText).join(' ');}
function dualRows(){return [
 packet(101,{created:now-300,upstream_usage:{input_tokens:100},outbound_wire_bytes:100000}),
 packet(102,{created:now-200,usage_override_applied:true,usage_override_request_bytes:250000,outbound_wire_bytes:200000}),
 packet(103,{created:now-100,upstream_usage:{input_tokens:300},overflow_cache_hit:true,overflow_original_wire_bytes:400000,outbound_wire_bytes:300000}),
 packet(104,{created:now-50,is_compaction:true,outbound_wire_bytes:150000})];}
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
  assert.ok(chartNodes(size,'trend-axis-title')[0].textContent.includes('包大小'));
  assert.equal(chartNodes(token,'trend-axis-title')[0].textContent,'输入 Token');
 });
 await test('linked hover preserves missing values without pinning or stale tooltips',()=>{
  const {result}=rendered(dualRows()),[size,token]=result.charts,missing=size.points[1];
  size.svg.onpointermove({clientX:missing.x,clientY:missing.y});
  assert.equal(result.hover,missing.packet.id);assert.equal(size.tooltip.hidden,false);assert.equal(token.tooltip.hidden,true);
  assert.equal(chartNodes(size,'trend-focus-line').length,1);assert.equal(chartNodes(token,'trend-focus-line').length,1);
  assert.equal(chartNodes(token,'trend-focus-dot').length,0);assert.ok(nodeText(size.tooltip).includes('输入 Token · —'));
  assert.ok(nodeText(size.tooltip).includes('实际上传'));
  for(const chart of result.charts){assert.equal(chart.svg.onclick,undefined);assert.equal(chart.svg.onkeydown,undefined);}
  assert.equal('pinned' in result,false);
  const next=token.points.at(-1);token.svg.onpointermove({clientX:next.x,clientY:next.y});
  assert.equal(result.hover,id(103));assert.equal(token.tooltip.hidden,false);assert.equal(size.tooltip.hidden,true);
  assert.equal(chartNodes(size,'trend-focus-dot').length,1);
  token.svg.onpointerleave();assert.equal(result.hover,null);assert.equal(result.hoverChart,null);
  for(const chart of result.charts){assert.equal(chart.tooltip.hidden,true);assert.equal(chartNodes(chart,'trend-focus-line').length,0);}
 });
 await test('a shared sidebar searches and highlights both metrics together',()=>{
  const {result,ids}=rendered([...dualRows(),packet(105,{thread_id:'session-b',created:now-10,upstream_usage:{input_tokens:900},outbound_wire_bytes:900000})]);
  const list=ids.get('trend-series'),a=list.children.find(node=>node.dataset.renderKey==='trend-series:session-a');
  assert.equal(a.onclick,undefined);a.onpointerenter();
  for(const chart of result.charts){const b=chart.svg.querySelectorAll('[data-series]').find(node=>node.dataset.series==='session-b');assert.ok(b.attrs.class.includes('dimmed'));}
  a.onpointerleave();for(const chart of result.charts)assert.equal(new Set(chart.points.map(point=>point.series.id)).size,2);
  ids.get('trend-search').oninput({currentTarget:{value:'session-a'}});
  for(const chart of result.charts)assert.ok(chart.points.every(point=>point.series.id==='session-a'));
  ids.get('trend-search').oninput({currentTarget:{value:'missing'}});
  for(const chart of result.charts){assert.equal(chart.points.length,0);assert.equal(chart.empty.hidden,false);}
  ids.get('trend-search').oninput({currentTarget:{value:''}});assert.equal(result.query,'');
  for(const chart of result.charts)assert.equal(new Set(chart.points.map(point=>point.series.id)).size,2);
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
  const {result,buttons,frames}=rendered([...dualRows(),packet(106,{created:now-7200,upstream_usage:{input_tokens:900},outbound_wire_bytes:900000})]);
  result.refresh=force=>{assert.equal(force,true);result.render();};buttons[0].onclick();
  for(const chart of result.charts){assert.equal(chart.bounds.end-chart.bounds.start,3600);assert.ok(chart.points.every(point=>point.packet.id!==id(106)));chart.plot.clientWidth=800;chart.plot.clientHeight=260;}
  result.resize.callback();frames.shift()();
  for(const chart of result.charts)assert.equal(chart.svg.attrs.viewBox,'0 0 800 260');
  assert.equal(result.charts[0].bounds.x(now-100),result.charts[1].bounds.x(now-100));
  const point=result.charts[0].points[0];result.charts[0].svg.onpointermove({clientX:point.x,clientY:point.y});
  result.emphasis='session-a';result.setActive(false);
  assert.equal(result.hover,null);assert.equal(result.hoverChart,null);assert.equal(result.emphasis,null);
  for(const chart of result.charts)assert.equal(chart.tooltip.hidden,true);
  result.refresh=()=>{};result.setActive(true);
  for(const chart of result.charts){assert.equal(chart.tooltip.hidden,true);assert.equal(chartNodes(chart,'trend-focus-line').length,0);}
 });
 console.log(JSON.stringify({passed:checks}));
})().catch(error=>{console.error(error);process.exitCode=1;});
