/* Synthetic contracts for metadata-only records and detail cache removal. */
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('packet_inspector/ui/app.js','utf8');
function extract(start,end) {
  const first=source.indexOf(start),last=source.indexOf(end,first);
  assert(first>=0&&last>first,`Missing function range: ${start}`);
  return source.slice(first,last);
}
function fixture() {
  const elements=new Map();
  const element=id=>{
    if(!elements.has(id))elements.set(id,{id,hidden:false,disabled:false,textContent:'',value:'',children:[],
      get firstChild(){return this.children[0];},classList:{add(){}},querySelectorAll:()=>[],
      setAttribute(){},removeAttribute(){},append(...values){this.children.push(...values);},replaceChildren(...values){this.children=values;}});
    return elements.get(id);
  };
  const calls=[];
  const context={
    state:{selected:'basic',detail:{id:'basic',details_evicted:true},tab:'headers',diffAvailable:null,detailRequest:0,listRequest:0},
    requestBodyView:{id:'basic',body:{input:'cached private body'}},
    requestBodyCache:{id:'basic',promise:Promise.resolve({input:'cached private body'})},
    requestBodyTextMode:false,requestBodyExportUrl:"",
    URL:{createObjectURL:()=>'',revokeObjectURL(){}},Blob,
    requestBodySequence:0,treeQueryTimer:0,tokenWindow:{input_tokens:123},
    $:element,t:value=>value,clearTimeout(){},URLSearchParams,
    clearJsonFocus(){calls.push('focus-cleared');},clearCopyFeedback(){},
    fail(error){throw error;},
    cancelTokenRead(){calls.push('token-cancelled');},renderList(){calls.push('list-rendered');},showTab(){},
    el:(tag,cls,text)=>({...element(Symbol(tag)),tag,cls,text}),
    reconcileContent:(node,content)=>node.replaceChildren(content),
    document:{createDocumentFragment:()=>element(Symbol('fragment'))},
    kind:value=>value,signed:value=>String(value),bytes:value=>String(value),calls,elements,tables:[],
    panel:title=>({...element(Symbol('panel')),title}),fact(node,label,value){node.append({label,value});},
    table(headers,rows){const result={...element(Symbol('table')),headers,rows};context.tables.push(result);return result;}
  };
  vm.createContext(context);
  vm.runInContext(extract('function requestTextMessages(','function copyRequestBody('),context);
  vm.runInContext(extract('function requestTextSearchHits(','async function searchBody('),context);
  vm.runInContext(extract('async function loadTree(path)','async function goPath(path)'),context);
  vm.runInContext(extract('function retainedReasoningEncryptedBytes(','function focusRequestBodyLine('),context);
  vm.runInContext(source.split(/\r?\n/).filter(line=>/^function compressedBytes\(/.test(line)).join('\n'),context);
  vm.runInContext(extract('function packetDiffRows(','function requestImagePaths('),context);
  vm.runInContext(extract('function packetDetailVersion(','function renderProtectionPanelState('),context);
  vm.runInContext(extract('function inputBreakdown(','function jsonValueNode('),context);
  return context;
}
(async()=>{
  let passed=0;
  {
    const c=fixture();
    c.requestBodyView.body.input=[{role:'user',content:'cached private body'},{role:'assistant',content:'cached reply'}];
    c.setRequestBodyTextMode(true);
    assert.equal(c.$('request-body-text').hidden,false);
    const bubbles=c.$('request-body-text').firstChild.children;
    assert.deepEqual(bubbles.map(bubble=>bubble.children[0].children[0].text),['user','assistant']);
    assert.equal(c.requestBodyView.output.extension,'txt');
    assert.equal(c.requestBodyView.output.text,'user\ncached private body\n\nassistant\ncached reply\n');
    const hits=c.requestTextSearchHits(c.requestBodyView.messages,'cached');
    assert.deepEqual(Array.from(hits,hit=>hit.label),['user · 1','assistant · 2']);
    c.$('body-results').append('cached search preview');
    c.api=async()=>{throw Error('Removed body must not be fetched');};
    await c.loadTree('');
    assert.equal(c.requestBodyView,null);
    assert.equal(c.requestBodyCache,null);
    assert.equal(c.$('tree-query').disabled,true);
    assert(c.$('json-tree').textContent.includes('仅保留基本信息'));
    assert.equal(c.$('json-tree').hidden,false);
    assert.equal(c.$('request-body-text').hidden,true);
    assert.equal(c.$('request-body-text').children.length,0);
    assert.equal(c.$('body-results').children.length,0);
    assert.equal(c.$('json-actions').hidden,true);
    await assert.rejects(c.ensureJsonView('basic',c.requestBodySequence),/仅保留基本信息/);
    c.state.selected='full';c.state.detail={id:'full'};
    c.ensureJsonView=async()=>({root:{},body:{input:'new detail'}});
    await c.loadTree('');
    assert.equal(c.$('tree-query').disabled,false);
    passed++;
  }
  {
    const c=fixture();
    c.api=async()=>({state:'details_unavailable'});
    c.compressedDelta=async()=>{throw Error('Missing details must not generate a diff');};
    await c.loadDiff();
    assert(c.$('diff-content').children[0].text.includes('无法取得可比较的统计信息'));
    assert.equal(c.state.diffAvailable,true);
    passed++;
  }
  {
    const c=fixture();c.api=async()=>null;
    c.compressedDelta=async()=>{throw Error('Missing previous request must not generate a diff');};
    await c.loadDiff();
    assert.equal(c.state.diffAvailable,false);
    assert.equal(c.$('diff-content').children.length,0);
    passed++;
  }
  {
    const c=fixture(),bars=[];
    c.bars=(title,rows,total,available)=>{bars.push({title,rows,total,available});return {title};};
    c.renderOverview({inspection:'json',details_evicted:true,decoded_bytes:150,top_fields:[{name:'input',bytes:120}],
      json_envelope_bytes:30,input_groups:{reasoning:100,message:18},reasoning_encrypted_bytes:80});
    assert.equal(bars.length,2);
    assert.equal(bars[0].total,150);
    assert.equal(bars[0].rows.find(row=>row.name==='input').bytes,120);
    assert.equal(bars[1].total,120);
    assert.deepEqual(Object.fromEntries(bars[1].rows.map(row=>[row.name,row.bytes])),
      {'推理密文':80,'推理非密文':20,message:18});
    assert(bars.every(row=>row.available===false));
    passed++;
  }
  {
    const c=fixture();let input,reads=0;
    const metadata={id:'legacy',inspection:'json',decoded_bytes:200,top_fields:[{name:'input',bytes:170}],
      json_envelope_bytes:30,input_groups:{reasoning:90,compaction:70},encrypted_content_bytes:75};
    const view={body:{input:[{type:'reasoning',encrypted_content:'opaque'},{type:'reasoning',summary:[]},
      {type:'compaction',encrypted_content:'compacted'}]},source_sizes:{'/input/0':60,'/input/1':30,
      '/input/0/encrypted_content':42,'/input/2':70,'/input/2/encrypted_content':37}};
    c.state.selected=metadata.id;c.state.detail=metadata;
    c.bars=(title,rows,total)=>{if(title==='input 构成')input={rows,total};return {title};};
    Object.assign(c,{uiI18n:{language:'zh'},URL:{createObjectURL:()=>''},Blob,
      jsonValueNode:()=>({}),renderHeaders(){}});
    c.api=async path=>{assert.equal(path,'/api/packets/legacy/export');reads++;return {metadata,view};};
    c.renderOverview(metadata);
    assert.equal(input.rows[0].name,'reasoning');
    await c.loadTree('');
    assert.equal(input.total,170);
    assert.deepEqual(Object.fromEntries(input.rows.map(row=>[row.name,row.bytes])),
      {compaction:70,'推理非密文':50,'推理密文':40});
    await c.loadTree('');
    assert.equal(reads,1);
    c.renderOverview({...metadata,details_evicted:true});
    assert.equal(input.rows[0].name,'reasoning','Unavailable historical detail keeps the combined number');
    passed++;
  }
  {
    const c=fixture();
    c.api=async()=>{throw Object.assign(Error('packet_not_found'),{status:404});};
    await c.selectPacket('basic',true);
    assert.equal(c.state.selected,null);
    assert.equal(c.requestBodyView,null);
    assert.equal(c.requestBodyCache,null);
    assert.equal(c.$('packet-detail').hidden,true);
    assert.equal(c.$('empty-state').hidden,false);
    passed++;
  }
  {
    const c=fixture(),paths=[],previous={id:'old',thread_id:'session',source:'live',created:10,details_evicted:true,
      decoded_bytes:100,outbound_wire_bytes:40,top_fields:[{name:'input',bytes:70}]};
    c.state.detail={id:'basic',thread_id:'session',source:'live',created:30,details_evicted:true,
      decoded_bytes:120,outbound_wire_bytes:50,top_fields:[{name:'input',bytes:90}]};
    c.api=async path=>{paths.push(path);if(path.endsWith('/diff'))return {state:'details_unavailable'};
      if(path.startsWith('/api/packets?'))return[{...previous,id:'wrong-source',source:'other',created:20},previous];
      assert.equal(path,'/api/packets/old');return previous;};
    c.compressedDelta=async data=>data.compressed_delta;await c.loadDiff();
    assert.equal(paths.length,3);const query=new URL(paths[1],'http://fixture').searchParams;
    assert.equal(query.get('thread'),'session');assert.equal(query.get('before'),'30');assert.equal(query.get('include_get'),'1');
    assert.equal(c.tables.length,1);assert.equal(c.tables[0].rows[0][1],'20');assert.equal(c.tables[0].rows[0][2],'90');
    const panels=c.$('diff-content').children[0].children,changes=panels.find(node=>node.title==='变化明细');
    assert(changes?.children.some(node=>node.text?.includes('以下显示统计变化')));passed++;
  }
  {
    const c=fixture();c.compressedDelta=async data=>data.compressed_delta;
    c.api=async path=>{assert(path.endsWith('/diff'));return {state:'summary_only',previous_id:'old',fields:[],fields_available:true,decoded_delta:0,compressed_delta:0};};
    await c.loadDiff();const panels=c.$('diff-content').children[0].children;
    assert(panels.find(node=>node.title==='变化明细').children.some(node=>node.text?.includes('字段大小未变化')));
    const missing=c.summaryPacketDiff({id:'old',top_fields:null,decoded_bytes:null},{top_fields:[{name:'input',bytes:100}]});
    assert.equal(missing.fields_available,false);assert.equal(missing.fields.length,0);assert.equal(missing.decoded_delta,null);assert.equal(missing.compressed_delta,null);passed++;
  }
  {
    const c=fixture(),paths=[];
    c.state.detail={id:'basic',thread_id:'session',source:'live',created:30,decoded_bytes:120,top_fields:[{name:'input',bytes:90}]};
    c.api=async path=>{paths.push(path);if(path.endsWith('/diff'))return {state:'details_unavailable',previous_id:'old'};
      assert.equal(path,'/api/packets/old');return {id:'old',thread_id:'session',source:'live',created:10,details_evicted:true,decoded_bytes:100,top_fields:[{name:'input',bytes:70}]};};
    c.compressedDelta=async data=>data.compressed_delta;await c.loadDiff();
    assert.equal(paths.length,2);assert.equal(c.tables[0].rows[0][1],'20');passed++;
  }
  {
    const c=fixture();let release,entered;const started=new Promise(resolve=>entered=resolve);
    c.state.detail={id:'basic',thread_id:'session',source:'live',created:30,details_evicted:true};
    c.api=async path=>{if(path.endsWith('/diff'))return {state:'details_unavailable'};entered();return new Promise(resolve=>release=resolve);};
    const pending=c.loadDiff();await started;c.state.detail={...c.state.detail};release([]);await pending;
    assert.equal(c.$('diff-content').children.length,0);passed++;
  }
  {
    const c=fixture();let scans=0;c.state.detail={id:'basic',thread_id:'session',source:'live',created:10000,details_evicted:true};
    c.api=async path=>{if(path.endsWith('/diff'))return {state:'details_unavailable'};scans++;
      return Array.from({length:50},(_,index)=>({id:String(scans*50+index),thread_id:'session',source:'other',created:9000-scans*50-index}));};
    await c.loadDiff();assert.equal(scans,8);assert.equal(c.tables.length,0);
    assert(c.$('diff-content').children[0].text.includes('无法取得可比较的统计信息'));passed++;
  }
  console.log(JSON.stringify({passed}));
})().catch(error=>{console.error(error);process.exitCode=1;});
