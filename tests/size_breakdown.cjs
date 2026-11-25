const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');

function element(tag,cls='',text='') {
  return {tag,cls,text,children:[],attributes:{},dataset:{},classList:{add(){}},
    append(...children){this.children.push(...children);},setAttribute(key,value){this.attributes[key]=value;}};
}
const source=fs.readFileSync('packet_inspector/ui/app.js','utf8');
const root=element('div');
const context=vm.createContext({
  t:text=>text,kind:text=>text,bytes:String,signed:value=>value===null?'—':(value>0?'+':'')+value+' B',
  requestBodyView:null,requestBodyCache:null,expandedBreakdownRows:new Set(),breakdownComparison:null,state:{},
  $:()=>root,el:element,panel:title=>element('section','',title),link:(text)=>element('button','',text),
  document:{createDocumentFragment:()=>element('fragment')},reconcileContent:(node,content)=>{node.children=[content];},
  isToolSearch:()=>false,fail(error){throw error;}
});
for(const [first,last] of [['bars','jsonValueNode'],['retainedReasoningItemBytes','ensureJsonView'],
  ['reasoningItemCiphertextBytes','localImagePath']]) {
  const start=source.indexOf(`function ${first}(`),end=source.indexOf(`function ${last}(`,start);
  assert(start>=0&&end>start);
  vm.runInContext(source.slice(start,source.lastIndexOf('\n',end)+1),context);
}

const groups=(total,encrypted)=>({input_groups:{reasoning:total},reasoning_encrypted_bytes:encrypted});
const item=(index,bytes,encrypted)=>({index,kind:'reasoning',bytes,reasoning_encrypted_bytes:encrypted});
const breakdown=(previous,current,diff)=>Object.fromEntries(context.inputBreakdown(current,
  {previous,items:context.packetDiffItems(diff)}).map(row=>[row.component,row]));
const changes=row=>Array.from(row.items,({index,change,delta})=>[index,change,delta]);

{
  const previous=groups(515254,498156),current=groups(531603,514012);
  const added=[item(734,11004,10724),item(735,5345,5132)];
  const rows=breakdown(previous,current,{added});
  assert.equal(rows.encrypted.delta,15856);
  assert.equal(rows.plain.delta,493);
  assert.deepEqual(changes(rows.encrypted),[[734,'added',10724],[735,'added',5132]]);
  assert.deepEqual(changes(rows.plain),[[734,'added',280],[735,'added',213]]);

  const missing=breakdown(previous,current,{added:[added[0],item(735,5345)]});
  assert.deepEqual(changes(missing.encrypted),[[734,'added',10724],[735,null,null]]);
  assert.deepEqual(changes(missing.plain),[[734,'added',280],[735,null,null]]);
}

{
  const rows=breakdown(groups(140,80),groups(124,70),{
    removed:[item(0,100,80),item(1,40,0)],added:[item(0,124,70)]
  });
  assert.deepEqual(changes(rows.encrypted),[[0,'shrink',-10],[1,null,0]]);
  assert.deepEqual(changes(rows.plain),[[0,'grow',34],[1,'removed',-40]]);
}

{
  const rows=breakdown(groups(100,80),groups(110,80),{removed:[item(0,100,80)],added:[item(0,110,80)]});
  assert.deepEqual(changes(rows.encrypted),[[0,null,0]]);
  assert.deepEqual(changes(rows.plain),[[0,'grow',10]]);
}

async function checkExpansion() {
  const apiCalls=[];
  const previous={...groups(515254,498156),id:'previous',created:1,thread_id:'session',source:'live'};
  const current={...groups(531603,514012),id:'current',created:2,thread_id:'session',source:'live',inspection:'json'};
  context.state={selected:current.id,detail:current};
  const inputs=[];
  inputs[734]={type:'reasoning',encrypted_content:'retained'};
  inputs[735]={type:'reasoning',encrypted_content:'retained'};
  context.api=async path=>{
    apiCalls.push(path);
    if(path.endsWith('/diff'))return {previous_id:previous.id,added:[item(734,11004),item(735,5345)]};
    if(path.endsWith('/previous'))return previous;
    assert.equal(path,'/api/packets/current/export');
    return {metadata:current,view:{body:{input:inputs},source_sizes:{'/input/734':11004,'/input/735':5345,
      '/input/734/encrypted_content':10726,'/input/735/encrypted_content':5134}}};
  };
  await context.loadBreakdownComparison(current);
  assert.equal(apiCalls.length,2,'The older relay should only need the saved body after expansion');
  const row=component=>root.children[0].children[1].children.find(node=>node.dataset.renderKey==='bar:reasoning/'+component);
  const delta=component=>row(component).children[2];
  const details=component=>row(component).children.at(-1);
  for(const component of ['encrypted','plain']) {
    assert.equal(delta(component).tag,'button');
    delta(component).onclick();
    assert.equal(delta(component).attributes['aria-expanded'],'true');
  }
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(apiCalls.length,3,'Both groups share the same saved-body lookup');
  const values=component=>Array.from(details(component).children[0].children,
    node=>[node.children[0].children[0].text,node.children[2].children.at(-1)]);
  assert.deepEqual(values('encrypted'),[['#734','+10724 B'],['#735','+5132 B']]);
  assert.deepEqual(values('plain'),[['#734','+280 B'],['#735','+213 B']]);

  context.breakdownComparison=null;
  context.state.detail={...current,details_evicted:true};
  context.renderOverview(context.state.detail);
  for(const component of ['encrypted','plain']) {
    assert.equal(delta(component).tag,'button');
    assert.equal(details(component).children[0].text,'请求详情已回收，仅保留基本信息。');
    delta(component).onclick();
    assert.equal(delta(component).attributes['aria-expanded'],'false');
  }
}

checkExpansion().then(()=>console.log('Size breakdown and expansion cases passed')).catch(error=>{
  console.error(error);
  process.exitCode=1;
});
