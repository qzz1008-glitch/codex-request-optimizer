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
      append(...values){this.children.push(...values);},replaceChildren(...values){this.children=values;}});
    return elements.get(id);
  };
  const calls=[];
  const context={
    state:{selected:'basic',detail:{id:'basic',details_evicted:true},detailRequest:0,listRequest:0},
    requestBodyView:{id:'basic',body:{input:'cached private body'}},
    requestBodyCache:{id:'basic',promise:Promise.resolve({input:'cached private body'})},
    requestBodySequence:0,treeQueryTimer:0,tokenWindow:{input_tokens:123},
    $:element,t:value=>value,clearTimeout(){},
    clearJsonFocus(){calls.push('focus-cleared');},clearJsonDownload(){calls.push('download-cleared');},
    cancelTokenRead(){calls.push('token-cancelled');},renderList(){calls.push('list-rendered');},
    el:(tag,cls,text)=>({...element(Symbol(tag)),tag,cls,text}),
    reconcileContent:(node,content)=>node.replaceChildren(content),
    document:{createDocumentFragment:()=>({...element(Symbol('fragment'))})},
    kind:value=>value,calls,elements
  };
  vm.createContext(context);
  vm.runInContext(extract('async function loadTree(path)','async function goPath(path)'),context);
  vm.runInContext(extract('async function ensureJsonView(id,sequence)','function focusJsonPath('),context);
  vm.runInContext(extract('async function loadDiff()','function requestImagePaths('),context);
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
    assert(c.$('diff-content').children[0].text.includes('无法比较变化'));
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
  console.log(JSON.stringify({passed}));
})().catch(error=>{console.error(error);process.exitCode=1;});
