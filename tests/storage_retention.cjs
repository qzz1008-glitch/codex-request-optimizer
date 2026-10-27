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
      classList:{add(){}},querySelectorAll:()=>[],append(...values){this.children.push(...values);},replaceChildren(...values){this.children=values;}});
    return elements.get(id);
  };
  const calls=[];
  const context={
    state:{selected:'basic',detail:{id:'basic',details_evicted:true},detailRequest:0,listRequest:0},
    requestBodyView:{id:'basic',body:{input:'cached private body'}},
    requestBodyCache:{id:'basic',promise:Promise.resolve({input:'cached private body'})},
    requestBodySequence:0,treeQueryTimer:0,tokenWindow:{input_tokens:123},
    $:element,t:value=>value,clearTimeout(){},URLSearchParams,
    clearJsonFocus(){calls.push('focus-cleared');},clearJsonDownload(){calls.push('download-cleared');},
    cancelTokenRead(){calls.push('token-cancelled');},renderList(){calls.push('list-rendered');},
    el:(tag,cls,text)=>({...element(Symbol(tag)),tag,cls,text}),
    reconcileContent:(node,content)=>node.replaceChildren(content),
    document:{createDocumentFragment:()=>({...element(Symbol('fragment'))})},
    kind:value=>value,signed:value=>String(value),bytes:value=>String(value),calls,elements,tables:[],
    panel:title=>({...element(Symbol('panel')),title}),fact(node,label,value){node.append({label,value});},
    table(headers,rows){const result={...element(Symbol('table')),headers,rows};context.tables.push(result);return result;}
  };
  vm.createContext(context);
  vm.runInContext(extract('async function loadTree(path)','async function goPath(path)'),context);
  vm.runInContext(extract('async function ensureJsonView(id,sequence)','function focusJsonPath('),context);
  vm.runInContext(source.split(/\r?\n/).filter(line=>/^function compressedBytes\(/.test(line)).join('\n'),context);
  vm.runInContext(extract('function packetDiffRows(','function requestImagePaths('),context);
  vm.runInContext(extract('async function selectPacket(id','function renderProtectionPanelState('),context);
  vm.runInContext(extract('function renderOverview(m)','function jsonValueNode('),context);
  return context;
}
(async()=>{
  let passed=0;
  {
    const c=fixture();
    c.api=async()=>{throw Error('Removed body must not be fetched');};
    await c.loadTree('');
    assert.equal(c.requestBodyView,null);
    assert.equal(c.requestBodyCache,null);
    assert.equal(c.$('tree-query').disabled,true);
    assert(c.$('json-tree').textContent.includes('仅保留基本信息'));
    assert(c.calls.includes('download-cleared'));
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
    passed++;
  }
  {
    const c=fixture(),bars=[];
    c.bars=(title,rows,total,available)=>{bars.push({title,rows,total,available});return {title};};
    c.renderOverview({inspection:'json',details_evicted:true,decoded_bytes:150,top_fields:[{name:'input',bytes:120}],
      json_envelope_bytes:30,input_groups:{message:120}});
    assert.equal(bars.length,2);
    assert.equal(bars[0].total,150);
    assert.equal(bars[0].rows.find(row=>row.name==='input').bytes,120);
    assert.equal(bars[1].total,120);
    assert(bars.every(row=>row.available===false));
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
    const panels=c.$('diff-content').children[0].children;assert(panels.some(node=>node.title==='变化明细'));
    assert(panels.some(node=>node.text?.includes('以下显示统计变化')));passed++;
  }
  {
    const c=fixture();c.compressedDelta=async data=>data.compressed_delta;
    c.api=async path=>{assert(path.endsWith('/diff'));return {state:'summary_only',previous_id:'old',fields:[],fields_available:true,decoded_delta:0,compressed_delta:0};};
    await c.loadDiff();const panels=c.$('diff-content').children[0].children;
    assert(panels.find(node=>node.title==='变化明细').children[0].text.includes('字段大小未变化'));
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
