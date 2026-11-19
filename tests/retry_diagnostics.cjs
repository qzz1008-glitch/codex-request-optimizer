const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('packet_inspector/ui/app.js', 'utf8');
const translations = fs.readFileSync('packet_inspector/ui/i18n.js', 'utf8');
const dictionary = vm.runInNewContext(translations.slice(translations.indexOf('const english = {'), translations.indexOf('\n  };') + 5) + '\nenglish;');
let english = false;
const context = {nf:new Intl.NumberFormat('en-US'), t:key=>english?(dictionary[key]||key):key};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function autoRetryCount('), source.indexOf('function listTokens(')), context);
vm.runInContext(source.slice(source.indexOf('function sortInputTokens('),source.indexOf('function sortedTokenPackets(')),context);
vm.runInContext('class Trends {\n' + source.slice(source.indexOf('  static events(packet)'), source.indexOf('  static markerShape(')) + '\n}\nthis.events=Trends.events;', context);
function packet(fields={}, count=0) {
  return {source:'live',capacity_retry_count:count, capacity_retry_diagnostic:{id:'request-1', journal_saved:true, journal_available:true,
    enabled:true, eligible:true, max_retries:3, retry_count:count, scheduled_retries:count,
    state:'finished', outcome:'success', stop_reason:'stream_committed', ...fields}};
}
assert.equal(context.packetProtection({native_retry_advised:true,capacity_retry_count:0}).retry,true);
assert.deepEqual(Array.from(context.events({native_retry_advised:true})),['retry']);
assert.equal(context.packetProtection({bad_request_retry_count:2,capacity_retry_count:0}).retry,true);
assert.equal(context.autoRetryCount({bad_request_retry_count:2,capacity_retry_count:0}),2);
assert.deepEqual(Array.from(context.events({bad_request_retry_count:2})),['retry']);
let p = packet({outcome:'capacity_error',capacity_error_seen:true,stop_reason:'retry_limit'},3);
assert.equal(context.packetProtection(p).retry, true);
assert.deepEqual(Array.from(context.events(p)), ['retry']);
p = packet({enabled:false,outcome:'capacity_error',capacity_error_seen:true,stop_reason:'disabled'});
assert.equal(context.packetProtection(p).retry, false);
assert.equal(context.packetProtection(packet({scheduled_retries:1})).retry, false);
assert.deepEqual(Array.from(context.events(packet({scheduled_retries:1}))), []);

function node(tag,text='') {
  const classes=new Set();
  return {tag,tagName:tag,localName:tag,text,textContent:text,children:[],dataset:{},attributes:{},
    classList:{add(...values){values.forEach(value=>classes.add(value));},contains(value){return classes.has(value);},
      toggle(value,enabled){if(enabled)classes.add(value);else classes.delete(value);}},
    append(...children){this.children.push(...children);},setAttribute(name,value){this.attributes[name]=value;},
    querySelectorAll(selector){assert.equal(selector,'[data-protection]');return descendants(this).filter(child=>child.dataset.protection);},
    get lastChild(){return this.children.at(-1);}};
}
function descendants(root){return root.children.flatMap(child=>[child,...descendants(child)]);}
let rendered;
const opened=[];
Object.assign(context, {
  URL,requestBodyView:null,sessionSummaries:[],uiI18n:{labels:values=>values},
  state:{status:{packet_size_protection_enabled:true,capacity_retry_enabled:true,image_analysis_mode:'optimized',
    open_directory_supported:true,open_image_supported:true,reveal_image_supported:true}},
  document:{createDocumentFragment:()=>node('fragment'),createElementNS:(_,tag)=>node(tag)},
  el:(tag,cls,text)=>Object.assign(node(tag,text),{className:cls||''}),
  bytes:value=>value==null?'—':String(value),
  $:id=>id==='packet-protection-panel'?rendered?.children.find(child=>child.id===id):null,
  reconcileContent:(_,root)=>{rendered=root;},api:async(route,data)=>{opened.push([route,data.path]);return {opened:true};},
  fail:error=>{throw error;}
});
vm.runInContext(source.split(/\r?\n/).filter(line=>/^function (originalBytes|compressedBytes|auxiliarySessionKind)\(|^const (states|auxiliaryLabels)\b/.test(line)).join('\n'),context);
vm.runInContext(source.slice(source.indexOf('function panel('),source.indexOf('function table(')),context);
vm.runInContext(source.slice(source.indexOf('function fact('),source.indexOf('function showTab(')),context);
vm.runInContext(source.slice(source.indexOf('function purposeFromMetadata('),source.indexOf('function cacheRequestPurpose(')),context);
vm.runInContext(source.slice(source.indexOf('function localImagePath('),source.indexOf('function renderHeaders(')),context);
vm.runInContext(source.slice(source.indexOf('function renderProtectionPanelState('),source.indexOf('function bars(')),context);
vm.runInContext(source.slice(source.indexOf('function renderHeaders('),source.indexOf('async function setInterfaceLanguage(')),context);
vm.runInContext(source.slice(source.indexOf('function packetDetailVersion('),source.indexOf('async function selectPacket(')),context);
const pictureA='C:\\saved-images\\a.png',pictureB='C:\\saved-images\\b.png';
const imagePacket=(id,created,image_paths,extra={})=>({id,created,thread_id:'session-a',image_isolation_applied:true,image_paths,...extra});
const uniqueImageRows=packets=>Array.from(context.imageOptimizationRows(packets));
const repeatedImages=[imagePacket('first',1,[pictureA]),imagePacket('repeat',2,[pictureA]),
  imagePacket('new-image',3,[pictureA,pictureB]),imagePacket('repeat-both',4,[pictureA,pictureB])];
for(const order of [repeatedImages,[...repeatedImages].reverse(),[repeatedImages[2],repeatedImages[0],repeatedImages[3],repeatedImages[1]]])
  assert.deepEqual(uniqueImageRows(order),['first','new-image'],'List sorting must not change the first image occurrence');
assert.deepEqual(repeatedImages.map(row=>row.id),['first','repeat','new-image','repeat-both'],'Deduplication must not reorder source records');
assert.equal(context.packetProtection(repeatedImages[1]).optimization,true,'Request details must retain the actual processing flag');
assert.deepEqual(uniqueImageRows([imagePacket('first',1,[pictureA,pictureA]),imagePacket('alias',2,['c:/SAVED-images/a.png','file:///C:/saved-images/a.png'])]),['first']);
assert.deepEqual(uniqueImageRows([imagePacket('source',1,['C:\\clipboard.png'],{image_path_rewrites:{'C:\\clipboard.png':pictureA}}),imagePacket('retained',2,[pictureA])]),['source']);
const retiredImage='C:\\Temp\\CodexRequestOptimizer\\'+'a'.repeat(32)+'.png';
assert.deepEqual(uniqueImageRows([imagePacket('permanent',1,[pictureA]),imagePacket('old-duplicate',2,[pictureA,retiredImage])]),
  ['permanent'],'Removed temporary copies must not create extra image markers');
assert.deepEqual(uniqueImageRows([imagePacket('first',1,[pictureA]),imagePacket('other-session',2,[pictureA],{thread_id:'session-b'})]),['first','other-session']);
assert.deepEqual(uniqueImageRows([imagePacket('upper',1,['/home/user/A.png']),imagePacket('lower',2,['/home/user/a.png'])]),['upper','lower'],'Linux paths are case sensitive');
assert.deepEqual(uniqueImageRows([imagePacket('tie-b',1,[pictureA]),imagePacket('tie-a',1,[pictureA])]),['tie-a']);
assert.deepEqual(uniqueImageRows([imagePacket('native',1,[pictureA],{image_isolation_applied:false}),imagePacket('optimized',2,[pictureA])]),['optimized']);
assert.deepEqual(uniqueImageRows([imagePacket('tools-only',1,[]),imagePacket('missing',2,undefined),imagePacket('invalid',3,['https://example.invalid/a.png'])]),[],
  'A changed tool catalog or missing image identity must not create a per-image marker');
const manyPictures=Array.from({length:33},(_,index)=>`C:\\saved-images\\picture-${index}.png`);
assert.deepEqual(uniqueImageRows([imagePacket('old-images',1,manyPictures.slice(0,32)),imagePacket('image-33',2,manyPictures)]),['old-images','image-33']);
assert.deepEqual(uniqueImageRows(repeatedImages.slice(1)),['repeat','new-image']);
assert.deepEqual(uniqueImageRows(repeatedImages),['first','new-image'],'Loading older history must remove the duplicate marker from the later row');
const waitingPacket={id:'pending',state:'captured',status:null};
for(const fields of [{image_isolation_applied:true},{image_paths:['C:\\image.png']},
  {upstream_usage:{input_tokens:123},reported_usage:{input_tokens:500000}},
  {inline_image_bytes:123},{transport_input_bytes:1000},{transport_wire_bytes:500},
  {overflow_cache_hit:true,relay_wire_bytes:1000},{image_upload_before_bytes:1200000},{image_upload_images:[{width:1600,height:900}]}])
  assert.notEqual(context.packetDetailVersion({...waitingPacket,...fields}),context.packetDetailVersion(waitingPacket),
    'Protection details must refresh even if request status has not changed');
const labels=['大小预测','超限压缩','容量重试','解图优化'];
function protection(){return rendered.children.at(-1);}
function rows(){return protection().children.filter(child=>child.dataset.protection);}
function headings(){return rows().map(row=>{
  const heading=row.children[0],status=heading.lastChild;
  assert.equal(heading.lastChild.text,row.dataset.triggered==='true'?'✓':'—');
  assert.equal(status.title,context.t(row.dataset.triggered==='true'?'已触发':'未触发'));
  assert.equal(status.attributes['aria-label'],status.title);
  assert.equal(status.attributes.role,'img');
  assert.equal(row.dataset.mechanism,row.dataset.renderKey.replace('protection:',''));
  return {label:heading.children[0].text,value:status.text};
});}
function details(key){return rows().find(row=>row.dataset.renderKey==='protection:'+key)?.children[1];}
function fields(root){return Object.fromEntries(descendants(root).filter(child=>child.className==='fact-row').map(row=>[row.children[0].text,row.children[1].text]));}
function overviewTitles(){return rendered.children.map(section=>{
  assert.equal(section.children[0].tag,'h2');
  return section.children[0].text;
});}
function originalFacts(){return fields(rendered.children[0]);}
function forwardedFacts(){return fields(rendered.children[1]);}
function protectionFacts(){return Object.fromEntries(headings().map(row=>[row.label,row.value]));}
const mechanisms=['predictive','overflow','retry','optimization'];
for(let mask=0;mask<16;mask++) {
  const active=mechanisms.filter((_,index)=>mask&(1<<index));
  const meta={source:'live',usage_override_applied:active.includes('predictive'),overflow_cache_hit:active.includes('overflow'),
    native_retry_advised:active.includes('retry'),image_isolation_applied:active.includes('optimization'),image_upload_optimized:true};
  const flags=context.packetProtection(meta),markers=context.renderProtectionMarkers(flags);
  assert.deepEqual(Object.keys(flags).filter(key=>flags[key]),active);
  if(!active.length)assert.equal(markers,null);
  else {
    assert.equal(markers.attributes.role,'img');
    assert.deepEqual(Array.from(markers.children,child=>child.dataset.mechanism),active,'Simultaneous mechanisms must all remain visible');
    assert.equal(markers.attributes['aria-label'],active.map(key=>labels[mechanisms.indexOf(key)]).join(' · ')+' · 已触发');
    for(const marker of markers.children)assert.equal(marker.title,labels[mechanisms.indexOf(marker.dataset.mechanism)]+' · 已触发');
  }
  context.renderHeaders(meta);
  const [heading,...items]=protection().children;
  assert.equal(protection().tag,'section');
  assert.equal(heading.tag,'h2');
  assert.equal(heading.text,'请求保护');
  assert.equal(protection().attributes['aria-label'],heading.text);
  assert.deepEqual(items,rows(),'Protection rows are direct children of the section');
  assert.deepEqual(Array.from(headings(),row=>row.value),mechanisms.map(key=>active.includes(key)?'✓':'—'));
}
assert.equal(context.packetProtection({image_isolation_removed:1}).optimization,true);
assert.equal(context.packetProtection({image_isolation_removed:'1'}).optimization,false);
assert.equal(context.renderProtectionMarkers(context.packetProtection({image_upload_optimized:true})),null,
  'Resizing an image is ordinary compression and must not create a special mechanism marker');
context.renderHeaders(packet({outcome:'capacity_error',capacity_error_seen:true,stop_reason:'retry_limit'},3));
assert.deepEqual(Array.from(headings(),row=>row.label),labels);
assert.equal(headings()[2].value,'✓');
assert.deepEqual(fields(details('retry')),{'重试次数':'3'});
context.renderHeaders(packet({journal_available:false}));
assert.equal(headings()[2].value,'—');
assert.equal(details('retry'),undefined);
context.renderHeaders(packet({outcome:'cancelled',scheduled_retries:1}));
assert.equal(headings()[2].value,'—');
context.renderHeaders({capacity_retry_count:2});
assert.equal(headings()[2].value,'✓');
assert.deepEqual(fields(details('retry')),{'重试次数':'2'});
context.renderHeaders({source:'live',native_retry_advised:true,capacity_retry_count:0,
  native_retry_delay_ms:30000,capacity_retry_diagnostic:{max_retries:15,scheduled_retries:1}});
assert.equal(headings()[2].value,'✓');
assert.deepEqual(fields(details('retry')),{'重试次数':'—'},'An advice, delay or configured limit cannot stand in for actual retries');
context.renderHeaders({});
assert.deepEqual(Array.from(headings(),row=>row.value),labels.map(()=>'—'));
assert(rows().every(row=>row.children.length===1),'Untriggered protections must have no detail fields');
// A summary can turn a compressed packet into a small identity upload. The
// encoding/level displayed next to the pre-summary size must describe that size.
const interrupted={source:'live',state:'relay_error',status:200,
  relay_content_encoding:'identity',relay_zstd_level:null,
  overflow_compaction_calls:1,overflow_original_wire_bytes:1004951,
  usage_override_request_bytes:1004951,usage_override_trigger_bytes:950000};
context.renderHeaders(interrupted);
assert.equal(forwardedFacts()['请求压缩'],'-');
assert.equal(protectionFacts()['大小预测'],'—');
assert.equal(protectionFacts()['超限压缩'],'✓');
assert.deepEqual(fields(details('overflow')),{'处理前大小':'1004951','处理后大小':'—'});
context.renderHeaders({...interrupted,transport_content_encoding:'zstd',transport_zstd_level:17});
assert.equal(forwardedFacts()['请求压缩'],'zstd · 17 级');
context.renderHeaders({...interrupted,transport_content_encoding:'zstd',transport_zstd_level:22});
assert.equal(forwardedFacts()['请求压缩'],'zstd · 22 级');
context.renderHeaders({...interrupted,state:'streaming'});
assert.equal(protectionFacts()['大小预测'],'—');
context.renderHeaders({...interrupted,state:'complete',usage_override_applied:true,
  request_token_estimate:{source:'codex_estimate',estimated:true,state:'available',input_tokens:230000},
  upstream_usage:{input_tokens:19643},reported_usage:{input_tokens:500000},relay_wire_bytes:95000,outbound_wire_bytes:91000});
assert.equal(originalFacts()['输入 Token（估算）'],'≈ 230,000');
assert.equal(originalFacts()['上游实际输入 Token'],undefined);
assert.equal(forwardedFacts()['上游实际输入 Token'],'19,643');
assert.equal(forwardedFacts()['输入 Token（估算）'],undefined);
assert.equal(protectionFacts()['大小预测'],'✓');
assert.deepEqual(fields(details('predictive')),{'原始 Token':'19,643','改写 Token':'500,000'});
assert.deepEqual(fields(details('overflow')),{'处理前大小':'1004951','处理后大小':'95000'});
context.renderHeaders({...interrupted,usage_override_applied:true});
assert.deepEqual(fields(details('predictive')),{'原始 Token':'—','改写 Token':'—'});
context.renderHeaders({...interrupted,state:'complete',usage_override_applied:false,reported_usage:{input_tokens:500000}});
assert.equal(protectionFacts()['大小预测'],'—');
assert.equal(details('predictive'),undefined);
context.renderHeaders({source:'live',relay_content_encoding:'zstd',relay_zstd_level:1});
assert.equal(forwardedFacts()['请求压缩'],'zstd · 1 级');
assert.equal(forwardedFacts()['文本压缩'],undefined);
assert.equal(forwardedFacts()['压缩方式'],undefined);
assert.equal(forwardedFacts()['压缩等级'],undefined);
for(const meta of [{},{relay_zstd_level:1}]) {
  context.renderHeaders(meta);
  assert.equal(forwardedFacts()['请求压缩'],'-','Missing encoding must remain unknown');
}
context.renderHeaders({relay_content_encoding:'identity',relay_zstd_level:1});
assert.equal(forwardedFacts()['请求压缩'],'-');
context.renderHeaders({relay_content_encoding:'gzip'});
assert.equal(forwardedFacts()['请求压缩'],'gzip');
const historicalImageRequest={source:'live',model:'example',state:'complete',status:200,duration_ms:1300,response_summary:{status:'completed'},
  decoded_bytes:2806092,image_isolation_removed:3,image_isolation_applied:true,
  image_isolation_decoded_before:2806092,image_isolation_decoded_after:1931967,
  transport_content_encoding:'zstd',transport_zstd_level:16,overflow_original_wire_bytes:954743};
context.renderHeaders(historicalImageRequest);
assert.deepEqual(originalFacts(),{'模型':'example','原始请求':'2806092','图片大小':'≈ 874125'});
assert.deepEqual(forwardedFacts(),{'结果':'完成 · HTTP 200','总耗时':'1.3 秒',
  '转发请求':'954743','请求压缩':'zstd · 16 级','压缩比例':'49.4%'});
assert.deepEqual(overviewTitles(),['原始请求','转发请求','请求保护']);
for(const section of rendered.children.slice(0,2)) {
  const list=section.children[1];
  assert.equal(list.tag,'dl');
  assert(list.children.every(row=>row.children[0].tag==='dt'&&row.children[1].tag==='dd'));
}
context.renderHeaders({source:'history',details_evicted:true,state:'relay_error',network_error:{message:'Test network failure'}});
assert.equal(originalFacts()['数据来源'],'历史重建（大小为估算）');
assert.equal(originalFacts()['请求详情'],'已回收');
assert.equal(forwardedFacts()['错误原因'],'Test network failure');
context.renderHeaders({...historicalImageRequest,transport_content_encoding:'identity'});
assert.equal(forwardedFacts()['请求压缩'],'-');
assert.equal(forwardedFacts()['压缩比例'],undefined,'Image-to-path size changes alone do not qualify as compression');
const resizedAndCompacted={decoded_bytes:2000000,inline_image_bytes:800000,image_upload_optimized:true,
  transport_content_encoding:'zstd',transport_input_bytes:1200000,transport_wire_bytes:600000,
  outbound_wire_bytes:100000,overflow_original_wire_bytes:600000,overflow_cache_hit:true};
context.renderHeaders(resizedAndCompacted);
assert.equal(originalFacts()['图片大小'],'800000');
assert.equal(forwardedFacts()['压缩比例'],'50.0%','Use only the measured transport stage');
context.renderHeaders({...resizedAndCompacted,transport_input_bytes:undefined});
assert.equal(forwardedFacts()['压缩比例'],undefined,'A resize with no before-size cannot use the original request size');
context.renderHeaders({relay_content_encoding:'zstd'});
assert.equal(forwardedFacts()['请求压缩'],'zstd','A missing level must not hide a known compression method');
context.renderHeaders({...interrupted,overflow_compaction_calls:0,overflow_cache_hit:true});
assert.equal(forwardedFacts()['请求压缩'],'-');
context.renderHeaders({source:'live',relay_error:'overflow_invalid_json',overflow_original_wire_bytes:1234567});
assert.equal(protectionFacts()['超限压缩'],'✓');
assert.deepEqual(fields(details('overflow')),{'处理前大小':'1234567','处理后大小':'—'});
// A normal generation request must expose its retained images inside protection.
const folder='C:\\retained-images\\',image=folder+'example.png';
context.renderHeaders({source:'live',request_kind:'turn',image_isolation_applied:true,image_paths:[image,image]});
assert.equal(protectionFacts()['解图优化'],'✓');
assert.equal(protectionFacts()['缩图保护'],undefined);
assert.equal(details('resize'),undefined);
const codes=descendants(details('optimization')).filter(child=>child.tag==='code');
assert.deepEqual(codes.map(child=>child.text),['example.png']);
assert.deepEqual(descendants(details('optimization')).filter(child=>child.tag==='button')
  .map(button=>button.attributes['aria-label']),['查看图片','在文件夹中显示']);
context.sessionSummaries=[{thread_id:'image-session',title:'示例会话',title_source:'native'}];
context.renderHeaders({source:'live',request_kind:'image_analysis',thread_id:'image-session',image_paths:[image]});
assert.deepEqual(overviewTitles(),['原始请求','转发请求','独立会话','请求保护']);
const sessionInfo=rendered.children.find(child=>child.id==='request-session-info');
assert.deepEqual(fields(sessionInfo),{'任务类型':'图片解析','会话名称':'示例会话',session_id:'image-session'});
const imageInfo=sessionInfo.children.find(child=>child.id==='request-image-info');
assert.deepEqual(descendants(imageInfo).filter(child=>child.tag==='code').map(child=>child.text),[folder,'example.png']);
context.renderHeaders({source:'live',request_kind:'title_generation',thread_id:'title-session'});
assert.deepEqual(fields(rendered.children[2]),{'任务类型':'自动标题','会话名称':'—',session_id:'title-session'});
assert.equal(descendants(rendered.children[2]).some(child=>child.id==='request-image-info'),false);
context.renderHeaders({source:'live',image_paths:[image]});
assert.deepEqual(overviewTitles(),['原始请求','转发请求','请求保护']);
assert.equal(protectionFacts()['解图优化'],'—','A path in a request does not prove that this request optimized an image');
assert.equal(details('optimization'),undefined);
context.renderHeaders({source:'live',image_isolation_applied:true});
assert.deepEqual(fields(details('optimization')),{'图片路径':'—'});
context.renderHeaders({source:'live',image_upload_optimized:true,image_upload_before_bytes:1200000,image_upload_images:[
  {original_width:2560,original_height:1440,width:1600,height:900},
  {mask:true,original_width:1000,original_height:1000,width:800,height:800}]});
assert.equal(protectionFacts()['缩图保护'],undefined);
assert.equal(details('resize'),undefined);
assert.equal(rendered.children.length,3,'Resolution details belong inside forwarding, without a separate card');
assert.equal(forwardedFacts()['图片压缩'],'参考图 1 · 2560 × 1440 → 1600 × 900\n编辑蒙版 · 1000 × 1000 → 800 × 800');
assert.equal(descendants(rendered).some(child=>child.tag==='h4'),false,'Image compression belongs in a fact row');
context.renderHeaders({source:'live',image_upload_optimized:true});
assert.equal(forwardedFacts()['图片压缩'],undefined,'Missing resolutions must not add an empty row');
context.renderHeaders({image_upload_optimized:true,image_upload_images:[null,{},
  {original_width:2560,original_height:1440,width:0,height:900}]});
assert.equal(forwardedFacts()['图片压缩'],undefined,'Invalid dimensions must not be shown');
context.renderHeaders({source:'live',image_upload_optimized:false,image_upload_images:[{original_width:2560,original_height:1440,width:2560,height:1440}]});
assert.equal(forwardedFacts()['图片压缩'],undefined,'Unmodified images must not show an image compression row');
// Image optimization has its own setting, independent of request-size protection.
context.state.status.packet_size_protection_enabled=false;context.state.status.capacity_retry_enabled=false;
context.renderHeaders({source:'live',image_isolation_applied:true,image_paths:[image]});
assert.equal(rows()[3].classList.contains('protection-inactive'),false);
assert.equal(rows()[0].classList.contains('protection-inactive'),true);
context.state.status.image_analysis_mode='native';context.renderProtectionPanelState();
assert.equal(rows()[3].title,'可在设置中开启解图优化');
assert.equal(descendants(details('optimization')).filter(child=>child.tag==='button').every(button=>!button.disabled),true,
  'Turning a feature off must not disable access to images saved in an earlier request');
english=true;
const englishMarkers=context.renderProtectionMarkers(context.packetProtection({image_isolation_applied:true,image_upload_optimized:true}));
assert.equal(englishMarkers.attributes['aria-label'],'Image analysis optimization · Triggered');
context.renderHeaders({source:'live',transport_content_encoding:'zstd',transport_zstd_level:1,image_upload_optimized:true,image_upload_before_bytes:1200000,
  image_upload_images:[{original_width:2560,original_height:1440,width:1600,height:900}]});
assert.equal(forwardedFacts()['Request compression'],'zstd · Level 1');
assert.equal(forwardedFacts()['Image compression'],'2560 × 1440 → 1600 × 900');
assert.deepEqual(overviewTitles(),['Original request','Forwarded request','Request protection']);
context.renderHeaders({source:'live',request_kind:'image_analysis',thread_id:'image-session',image_paths:[image]});
assert.deepEqual(overviewTitles(),['Original request','Forwarded request','Independent session','Request protection']);
context.renderHeaders({transport_content_encoding:'identity'});
assert.equal(forwardedFacts()['Request compression'],'-');
context.renderHeaders(interrupted);
assert.equal(protectionFacts()['Size prediction'],'—');
context.renderHeaders({...interrupted,state:'streaming'});
assert.equal(protectionFacts()['Size prediction'],'—');
context.renderHeaders({...interrupted,usage_override_applied:false,reported_usage:{input_tokens:500000}});
assert.equal(protectionFacts()['Size prediction'],'—');
context.renderHeaders(packet({outcome:'capacity_error',capacity_error_seen:true,stop_reason:'retry_limit'},3));
assert.deepEqual(Array.from(headings(),row=>row.label),['Size prediction','Over-limit compaction','Capacity retry','Image analysis optimization']);
assert.equal(headings()[2].value,'✓');
assert.deepEqual(fields(details('retry')),{'Retry count':'3'});
(async()=>{
  english=false;
  context.renderHeaders({source:'live',image_isolation_applied:true,image_paths:[image]});
  const actions=descendants(details('optimization')).filter(child=>child.tag==='button');
  for(const action of actions)await action.onclick({currentTarget:action});
  assert.deepEqual(opened,[['/api/open-image',image],['/api/reveal-image',image]]);
  console.log('Image markers, request size and compression fields, protection indicators, file actions, refresh and English labels passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
