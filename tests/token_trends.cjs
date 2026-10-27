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
 Object.assign(result,{active:true,loading:false,hydrating:false,packets:new Map(),native:new Map(),hours:1,revision:null,lastFull:0,query:'',error:false,limited:false,
  services:{api:async()=>[],status:()=>({storage:{revision:1},trend_packets_supported:true}),sessions:()=>[],peek:()=>null,isGeneration:context.isGeneration,...services},
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
 const result=new View(view(services).services);result.active=true;result.packets=new Map(rows.map(row=>[row.id,row]));
 result.render();return {result,ids,frames,events};
}
function chartNodes(chart,cls){return chart.svg.all(node=>(node.attrs.class||'').split(' ').includes(cls));}
function focusLines(result){return chartNodes({svg:result.guide},'trend-focus-line');}
function nodeText(node){return node.textContent+node.children.map(nodeText).join(' ');}
function dualRows(){return [
 packet(101,{created:now-300,upstream_usage:{input_tokens:100},outbound_wire_bytes:100000}),
 packet(102,{created:now-200,usage_override_applied:true,usage_override_request_bytes:250000,outbound_wire_bytes:200000}),
 packet(103,{created:now-100,upstream_usage:{input_tokens:300},overflow_cache_hit:true,overflow_original_wire_bytes:400000,outbound_wire_bytes:300000}),
 packet(104,{created:now-50,is_compaction:true,outbound_wire_bytes:150000})];}
(async()=>{
 await test('size axis steps use decimal bytes and match the configured limit units',()=>{
  const scale=View.sizeScale(1000000);
  assert.equal(scale.step,200000);assert.equal(scale.max,1000000);
  const {result}=rendered([packet(700,{outbound_wire_bytes:250000})],{status:()=>({budget_bytes:1000000})});
  assert.ok(nodeText(chartNodes(result.charts[0],'trend-budget-label')[0]).includes('1.00 MB'));
  assert.ok(chartNodes(result.charts[0],'trend-axis-label').some(node=>nodeText(node)==='200.0 KB'));
 });
 await test('configured size limits remain visible above the data and at the axis maximum',()=>{
  for(const budget of [1000000,1048576])for(const rows of [[packet(701,{outbound_wire_bytes:100000,upstream_usage:{input_tokens:100}})],[]]){
   const {result}=rendered(rows,{status:()=>({storage:{revision:1},budget_bytes:budget})}),[size,token]=result.charts;
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
   status:()=>({storage:{revision:1},budget_bytes:budget}),api:async()=>{reads++;return[];}});
  const before=chartNodes(result.charts[0],'trend-budget-line')[0].attrs.y1,tokenPoints=Array.from(result.charts[1].points,point=>point.y);
  result.lastFull=Date.now();result.revision=1;budget=1000000;await result.refresh();
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
  let n=0;
  const result=view({api:async path=>Array.from({length:Number(new URL(path,'http://fixture').searchParams.get('limit'))},()=>packet(++n,{created:now-n/2}))});
  await result.refresh();assert.equal(result.packets.size,20000);assert.equal(result.limited,true);
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
 await test('one bottom time axis aligns ticks with both equal-height metric panels',()=>{
  const {result}=rendered(dualRows()),labels=chartNodes({svg:result.timeAxis},'trend-axis-label');
  assert.equal(labels.length,6);assert.equal(result.timeAxis.attrs.viewBox,'0 0 640 32');
  const [size,token]=result.charts;
  assert.equal(size.bounds.height-size.bounds.top-size.bounds.bottom,token.bounds.height-token.bounds.top-token.bounds.bottom);
  for(const chart of result.charts){
   assert.ok(chartNodes(chart,'trend-axis-label').every(label=>!label.textContent.includes(':')));
   for(let i=0;i<labels.length;i++)assert.equal(Number(labels[i].attrs.x),chart.bounds.x(chart.bounds.start+(chart.bounds.end-chart.bounds.start)*i/5));
  }
 });
 await test('one hover guide spans both panels and stays aligned after a resize',()=>{
  const {result,ids}=rendered(dualRows());ids.get('trend-chart').top=240;
  result.hover=id(101);result.hoverChart='token';result.renderFocus();
  let lines=focusLines(result);assert.equal(lines.length,1);assert.equal(lines[0].attrs.y1,'30');assert.equal(lines[0].attrs.y2,'485');
  for(const chart of result.charts){assert.equal(chartNodes(chart,'trend-focus-line').length,0);assert.equal(chartNodes(chart,'trend-focus-dot').length,1);}
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
  assert.equal(chartNodes(token,'trend-focus-dot').length,0);assert.equal(size.tooltip.children.length,2);
  assert.ok(nodeText(size.tooltip).includes('压缩包'));assert.ok(nodeText(size.tooltip).includes('250.0 KB'));
  assert.equal(token.tooltip.children[1].textContent,'—');assert.ok(nodeText(token.tooltip).includes('输入 Token'));
  for(const text of ['输入 Token','实际上传','原始包',missing.packet.id,':'])assert.ok(!nodeText(size.tooltip).includes(text));
  for(const chart of result.charts){assert.equal(chart.svg.onclick,undefined);assert.equal(chart.svg.onkeydown,undefined);}
  assert.equal('pinned' in result,false);
  const next=token.points.at(-1);token.svg.onpointermove({clientX:next.x,clientY:next.y});
  assert.equal(result.hover,id(103));assert.equal(token.tooltip.hidden,false);assert.equal(size.tooltip.hidden,false);
  assert.equal(token.tooltip.children.length,2);assert.ok(nodeText(token.tooltip).replace(/\s+/g,' ').includes('输入 Token 300'));
  assert.equal(result.timeTooltip.textContent,result.time(next.packet.created,true));assert.ok(nodeText(size.tooltip).includes('400.0 KB'));
  assert.equal(chartNodes(size,'trend-focus-dot').length,1);
  token.svg.onpointerleave();assert.equal(result.hover,null);assert.equal(result.hoverChart,null);
  for(const chart of result.charts){assert.equal(chart.tooltip.hidden,true);assert.equal(chartNodes(chart,'trend-focus-line').length,0);}
  assert.equal(focusLines(result).length,0);
  assert.equal(result.timeTooltip.hidden,true);
 });
 await test('metric callouts stay in their own panels and time stays on the axis at both timeline edges',()=>{
  const {result,ids}=rendered([packet(401,{created:now-3599,upstream_usage:{input_tokens:0},outbound_wire_bytes:0}),
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
  assert.equal(chartNodes(size,'trend-focus-dot').length,0);assert.equal(token.tooltip.children[1].textContent,'0');
  assert.equal(result.timeTooltip.hidden,false);assert.equal(chartNodes(token,'trend-focus-dot').length,1);
  result.packets.set(id(403),{...result.packets.get(id(403)),outbound_wire_bytes:0});result.render();
  assert.equal(size.tooltip.children[1].textContent,'0 B');assert.equal(chartNodes(size,'trend-focus-dot').length,1);
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
  const slider=ids.get('trend-range-slider');slider.value='3';slider.oninput();assert.equal(result.hours,3);assert.equal(ids.get('trend-range-value').textContent,'3 小时');frames.shift()();
  for(const chart of result.charts)assert.ok(chart.points.some(point=>point.packet.id===id(106)));
  slider.value='1';slider.oninput();frames.shift()();
  for(const chart of result.charts){assert.equal(chart.bounds.end-chart.bounds.start,3600);assert.ok(chart.points.every(point=>point.packet.id!==id(106)));chart.plot.clientWidth=800;chart.plot.clientHeight=260;}
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
  for(const chart of result.charts){assert.equal(chart.tooltip.hidden,true);assert.equal(chartNodes(chart,'trend-focus-dot').length,0);}
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
   packet(501,{created:now-3599,upstream_usage:{input_tokens:100},outbound_wire_bytes:100000}),
   packet(502,{created:now-1,upstream_usage:{input_tokens:100},outbound_wire_bytes:100000}),
   packet(503,{thread_id:'session-b',created:now-3599,upstream_usage:{input_tokens:105},outbound_wire_bytes:105000}),
   packet(504,{thread_id:'session-b',created:now-1,upstream_usage:{input_tokens:105},outbound_wire_bytes:105000})
  ]);
  let token=result.charts[1];
  for(const id of ['session-a','session-b']){const [a,b]=token.points.filter(p=>p.series.id===id);token.svg.onpointermove({clientX:(a.x+b.x)/2,clientY:a.y});assert.equal(result.hoverSeries,id);assert.equal(result.hover,null);}
  ({result}=rendered([
   packet(505,{created:now-3599,upstream_usage:{input_tokens:100}}),
   packet(506,{created:now-1,upstream_usage:{input_tokens:100}})
  ]));
  token=result.charts[1];token.svg.clientWidth/=2;token.svg.clientHeight/=2;
  let [a,b]=token.points,x=(a.x+b.x)/4,y=a.y/2;
  token.svg.onpointermove({clientX:x,clientY:y+6});assert.equal(result.hoverSeries,'session-a');
  token.svg.onpointermove({clientX:x,clientY:y+10});assert.equal(result.hoverSeries,null);
  ({result}=rendered([
   packet(507,{created:now-3599,upstream_usage:{input_tokens:100}}),
   packet(508,{thread_id:'session-b',created:now-1,upstream_usage:{input_tokens:300}})
  ]));
  token=result.charts[1];[a,b]=token.points;token.svg.onpointermove({clientX:(a.x+b.x)/2,clientY:(a.y+b.y)/2});
  assert.equal(result.hoverSeries,null);assert.equal(result.hover,null);
 });
 await test('wheel and slider share local bounds without canceling an in-flight request',()=>{
  const {result,ids,frames}=rendered(dualRows()),slider=ids.get('trend-range-slider');
  result.controller=new AbortController();result.loading=true;result.refresh=()=>assert.fail('local zoom performed network work');
  let prevented=0;const wheel=ids.get('trend-charts').events.get('wheel');assert.equal(wheel.options.passive,false);
  wheel.callback({deltaY:120,deltaMode:0,preventDefault(){prevented++;}});
  assert.ok(result.hours>1);assert.equal(Number(slider.value),result.hours);assert.equal(result.controller.signal.aborted,false);assert.equal(prevented,1);frames.shift()();
  result.setRange(100);assert.equal(result.hours,12);result.setRange(-10);assert.equal(result.hours,1);
  result.oldestCreated=Date.now()/1000-7200;result.setRange(100);assert.ok(result.hours>=2&&result.hours<2.01);assert.equal(Number(slider.max),result.hours);
  const before=result.hours;wheel.callback({deltaY:120,ctrlKey:true,preventDefault(){assert.fail('browser zoom was intercepted');}});assert.equal(result.hours,before);
 });
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
   const q=new URL(path,'http://fixture').searchParams;queries.push(q);const start=q.has('after')?rows.findIndex(p=>p.id===q.get('after'))+1:0;return rows.slice(start,start+Number(q.get('limit')));
  }});
  await result.refresh();assert.equal(result.packets.size,53);assert.equal(queries.length,2);
  for(const q of queries){assert.equal(q.get('limit'),'50');assert.equal(q.get('view'),'trend');assert.ok(Number(q.get('since'))<=now-43199);}
  for(const row of result.packets.values()){assert.deepEqual(Object.keys(row.upstream_usage),['input_tokens']);assert.equal(row.reported_usage,undefined);assert.equal(row.capacity_retry_diagnostic,undefined);}
 });
 await test('dense curves retain every hover point and event marker with bounded ordinary dots',()=>{
  const rows=Array.from({length:12000},(_,i)=>packet(i+1,{created:now-i/4,upstream_usage:{input_tokens:1000+i},outbound_wire_bytes:200000+i,...(i%1000===0?{usage_override_applied:true}: {})}));
  const {result}=rendered(rows);
  for(const chart of result.charts){assert.equal(chart.points.length,12000);assert.equal(chartNodes(chart,'trend-event-predictive').length,12);assert.ok(chart.svg.all(n=>n.tag==='circle').length<500);assert.equal(chartNodes(chart,'trend-curve').length,1);}
 });
 console.log(JSON.stringify({passed:checks}));
})().catch(error=>{console.error(error);process.exitCode=1;});
