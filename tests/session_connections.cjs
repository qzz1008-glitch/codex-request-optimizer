/* Exercise sidebar grouping and relationship selection with synthetic session metadata. */
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('packet_inspector/ui/app.js','utf8'),context={t:value=>value};
vm.createContext(context);
const declarations=['OTHER_SESSION_GROUP','auxiliaryLabels'].map(name=>source.match(new RegExp('^const '+name+'=.*$','m'))[0]);
const kind=source.match(/^function auxiliarySessionKind\(row\).*$/m)[0];
const titles=source.slice(source.indexOf('function sessionTitle('),source.indexOf('function sessionGroups('));
vm.runInContext(declarations.join('\n')+'\n'+kind+'\n'+titles+'\n'+source.slice(source.indexOf('function sessionConnections('),source.indexOf('function renderSessionConnections(')),context);
vm.runInContext(source.slice(source.indexOf('function sessionGroups('),source.indexOf('function purposeFromMetadata(')),context);
const group=(id,extra={})=>({thread_id:id,members:[{thread_id:id}],...extra});
const links=(rows,selected)=>JSON.parse(JSON.stringify(context.sessionConnections(rows,selected)));
const session=(id,extra={})=>({thread_id:id,packet_count:1,updated_at:100,...extra});
const grouped=rows=>{
 context.sessionSummaries=rows;
  context.sessionMetadataAt=1;
 context.state={sessions:rows.filter(row=>row.packet_count!==0).map(row=>row.thread_id),filters:{thread:''}};
 return JSON.parse(JSON.stringify(context.sessionGroups()));
};
let passed=0;
function test(name,fn){try{fn();passed++;}catch(error){error.message=name+': '+error.message;throw error;}}

test('ordinary fork provenance never creates subagent links',()=>{
 const rows=[group('main',{title:'UI work'}),group('fork',{title:'Feature work',parent_thread_id:'main',session_kind:null})];
 for(const selected of ['thread:main','thread:fork'])assert.deepEqual(links(rows,selected),[]);
});
test('confirmed subagent connects to its parent from either selection',()=>{
 const rows=[group('main'),group('fork',{parent_thread_id:'main'}),group('child',{parent_thread_id:'main',session_kind:'subagent'})];
 for(const selected of ['thread:main','thread:child'])assert.deepEqual(links(rows,selected),[{parent:'main',child:'child'}]);
});
test('a normal fork can parent a subagent without linking its own fork origin',()=>{
 const rows=[group('main'),group('fork',{parent_thread_id:'main'}),group('child',{parent_thread_id:'fork',session_kind:'subagent'})];
 assert.deepEqual(links(rows,'thread:main'),[]);
 for(const selected of ['thread:fork','thread:child'])assert.deepEqual(links(rows,selected),[{parent:'fork',child:'child'}]);
});
test('only direct parents and children connect, never siblings or unrelated descendants',()=>{
 const rows=[group('main'),group('a',{parent_thread_id:'main',session_kind:'subagent'}),
  group('b',{parent_thread_id:'main',session_kind:'subagent'}),group('grandchild',{parent_thread_id:'a',session_kind:'subagent'})];
 assert.deepEqual(links(rows,'thread:a'),[{parent:'main',child:'a'},{parent:'a',child:'grandchild'}]);
 assert.deepEqual(links(rows,'thread:main'),[{parent:'main',child:'a'},{parent:'main',child:'b'}]);
});
test('missing parents, self references and other auxiliary kinds cannot create edges',()=>{
 const rows=[group('main'),group('missing',{parent_thread_id:'absent',session_kind:'subagent'}),
  group('self',{parent_thread_id:'self',session_kind:'subagent'}),group('image',{parent_thread_id:'main',session_kind:'image_analysis'})];
 for(const row of rows)assert.deepEqual(links(rows,'thread:'+row.thread_id),[]);
});
test('cycles and descendants of cycles are rejected',()=>{
 const rows=[group('a',{parent_thread_id:'b',session_kind:'subagent'}),group('b',{parent_thread_id:'a',session_kind:'subagent'}),
  group('child',{parent_thread_id:'a',session_kind:'subagent'})];
 for(const row of rows)assert.deepEqual(links(rows,'thread:'+row.thread_id),[]);
});
test('native subagent title source is accepted but an ordinary title cannot imply a relationship',()=>{
 const rows=[group('main'),group('native',{parent_thread_id:'main',title_source:'subagent'}),
  group('named',{parent_thread_id:'main',title:'Subtask - child'})];
 assert.deepEqual(links(rows,'thread:main'),[{parent:'main',child:'native'}]);
});
test('grouped members resolve their owner while absent selections and the other bucket stay unlinked',()=>{
 const rows=[group('main',{members:[{thread_id:'main'},{thread_id:'aux'}]}),
  group('child',{parent_thread_id:'main',session_kind:'subagent'}),group('group:other',{parent_thread_id:'main',session_kind:'subagent'})];
 assert.deepEqual(links(rows,'thread:aux'),[{parent:'main',child:'child'}]);
 for(const selected of ['thread:absent','group:other',''])assert.deepEqual(links(rows,selected),[]);
 assert.deepEqual(links([rows[1]],'thread:child'),[]);
});
test('image requests join their named parent while keeping their own request identity and purpose',()=>{
 const rows=[session('main',{title:'Frontend',packet_count:2,latest_packet:{id:'turn',created:100}}),
  session('image',{parent_thread_id:'main',session_kind:'image_analysis',title:'Temporary analysis',updated_at:200,latest_packet:{id:'analysis',created:200}})];
 const before=JSON.stringify(rows),groups=grouped(rows);
 assert.equal(groups.length,1);assert.equal(groups[0].thread_id,'main');assert.equal(groups[0].title,'Frontend');
 assert.deepEqual(groups[0].members.map(row=>row.thread_id),['main','image']);
 assert.equal(groups[0].members[1].session_kind,'image_analysis');assert.equal(groups[0].packet_count,3);
 assert.equal(groups[0].updated_at,200);assert.equal(groups[0].latest_packet.id,'analysis');assert.equal(JSON.stringify(rows),before);
});
test('a confirmed image parent remains a group before its name or native record loads',()=>{
 for(const parents of [[],[session('main',{title:null,packet_count:0})]]){
  const groups=grouped([...parents,session('image',{parent_thread_id:'main',session_kind:'image_analysis'})]);
  assert.equal(groups.length,1);assert.equal(groups[0].thread_id,'main');assert.equal(groups[0].title,'会话');
  assert.deepEqual(groups[0].members.map(row=>row.thread_id),['image']);assert.equal(groups[0].packet_count,1);
  assert.equal(context.otherTaskSessions().length,0);
 }
});
test('image requests belong to their direct subtask or fork without merging unrelated parents',()=>{
 const rows=[session('main',{title:'Main'}),session('child',{parent_thread_id:'main',session_kind:'subagent',title:'Builder'}),
  session('fork',{parent_thread_id:'main',title:null}),session('main-image',{parent_thread_id:'main',session_kind:'image_analysis'}),
  session('child-image',{parent_thread_id:'child',session_kind:'image_analysis'}),session('fork-image',{parent_thread_id:'fork',session_kind:'image_analysis'})];
 const groups=grouped(rows);
 assert.deepEqual(groups.map(group=>[group.thread_id,group.members.map(row=>row.thread_id)]),[
  ['main',['main','main-image']],['child',['child','child-image']],['fork',['fork','fork-image']]]);
});
test('missing or cyclic image parent metadata never assigns a request to an arbitrary conversation',()=>{
 const groups=grouped([session('main',{title:'Main'}),session('orphan',{session_kind:'image_analysis'}),
  session('self',{parent_thread_id:'self',session_kind:'image_analysis'}),
  session('a',{parent_thread_id:'b',session_kind:'image_analysis'}),session('b',{parent_thread_id:'a',session_kind:'image_analysis'})]);
 assert.deepEqual(groups.find(group=>group.thread_id==='main').members.map(row=>row.thread_id),['main']);
 assert.deepEqual(groups.find(group=>group.thread_id==='group:other').members.map(row=>row.thread_id),['orphan','self','a','b']);
});

test('unparented title and recommendation requests stay in other even when a temporary title exists',()=>{
  for(const kind of ['ambient_suggestions','title_generation']){
    const groups=grouped([session('main',{title:'Main'}),
      session('temporary',{session_kind:kind,title:'Temporary task'})]);
    assert.deepEqual(groups.find(group=>group.thread_id==='group:other').members.map(row=>row.thread_id),['temporary']);
    assert.deepEqual(groups.find(group=>group.thread_id==='main').members.map(row=>row.thread_id),['main']);
  }
});
test('parent metadata refresh moves an image out of other and resolves child selections to the owner',()=>{
 const rows=[session('main',{title:'Frontend'}),session('image',{session_kind:'image_analysis'})];
 assert.equal(grouped(rows).find(group=>group.thread_id==='group:other').members[0].thread_id,'image');
 rows[1]={...rows[1],parent_thread_id:'main'};
 const groups=grouped(rows);context.state.filters.thread='thread:image';
 assert.equal(groups.length,1);assert.equal(context.selectedSessionGroup().thread_id,'main');
 assert.equal(context.otherTaskSessions().length,0);
 rows[0].title='Renamed frontend';assert.equal(grouped(rows)[0].title,'Renamed frontend');
});
test('unnamed subagents join their known named parent or other instead of appearing as identical subtask rows',()=>{
 const rows=[session('main',{title:'Main'}),session('child',{parent_thread_id:'main',session_kind:'subagent',title:null}),
  session('unparented',{session_kind:'subagent',title:'  '}),session('named-child',{parent_thread_id:'main',session_kind:'subagent',title:'Builder'})];
 const groups=grouped(rows);
 assert.deepEqual(groups.map(group=>[group.title,group.members.map(row=>row.thread_id)]),[
  ['Main',['main','child']],['其他',['unparented']],['Builder',['named-child']]]);
 assert.deepEqual(links(groups,'thread:main'),[{parent:'main',child:'named-child'}]);
});
test('a cold start without names never presents generic subagent rows as loaded conversations',()=>{
 const groups=grouped([session('main',{title:null}),session('child',{title:null,session_kind:'subagent',parent_thread_id:'main'}),
  session('orphan',{session_kind:'subagent',parent_thread_id:'absent'})]);
 assert.deepEqual(groups.map(group=>[group.thread_id,group.title]),[['group:other','其他']]);
 assert.deepEqual(groups[0].members.map(row=>row.thread_id),['main','child','orphan']);
});
test('temporarily missing native names retain the last loaded title without retaining old request metrics',()=>{
 context.sessionSummaries=[session('main',{title:'Frontend',updated_at:100,latest_packet:{id:'old',created:100}})];
 const rows=[session('main',{title:null,updated_at:200,latest_packet:{id:'new',created:200},token_window:{input_tokens:500}})];
 const before=JSON.stringify(rows),result=JSON.parse(JSON.stringify(context.preserveSessionTitles(rows)));
 assert.equal(result[0].title,'Frontend');assert.equal(result[0].title_cached,true);
 assert.equal(result[0].updated_at,200);assert.equal(result[0].latest_packet.id,'new');assert.equal(result[0].token_window.input_tokens,500);
 assert.equal(JSON.stringify(rows),before);
 assert.equal(grouped(result)[0].thread_id,'main');
});
test('a generated subtask fallback cannot replace a previously loaded native name and fresh names still win',()=>{
 context.sessionSummaries=[session('child',{title:'Review routing',session_kind:'subagent',parent_thread_id:'main'})];
 const fallback=session('child',{title:'子任务 · audit',title_source:'subagent',session_kind:'subagent',parent_thread_id:'main'});
 const cached=JSON.parse(JSON.stringify(context.preserveSessionTitles([fallback])))[0];
 assert.equal(cached.title,'Review routing');assert.equal(cached.title_source,undefined);
 assert.equal(cached.title_cached,true);assert.equal(cached.session_kind,'subagent');assert.equal(cached.parent_thread_id,'main');
 const legacy=JSON.parse(JSON.stringify(context.preserveSessionTitles([{...fallback,session_kind:undefined}])))[0];
 assert.equal(legacy.title,'Review routing');assert.equal(legacy.session_kind,'subagent');
 context.sessionSummaries=[cached];
 const renamed=session('child',{title:'Review the new route',session_kind:'subagent'});
 assert.equal(context.preserveSessionTitles([renamed])[0],renamed);
 assert.equal(context.preserveSessionTitles([renamed])[0].title_cached,undefined);
});
test('title retention does not invent names for new or unidentified sessions or resurrect removed sessions',()=>{
 context.sessionSummaries=[session('',{title:'Unidentified request'}),session('removed',{title:'Removed'})];
 const rows=[session('',{title:null}),session('new',{title:null})];
 const result=context.preserveSessionTitles(rows);
 assert.equal(result[0],rows[0]);assert.equal(result[1],rows[1]);
 assert.equal(context.preserveSessionTitles([]).length,0);
});
(async()=>{
  const saved=Object.fromEntries(['AbortSignal','URLSearchParams','uploadBytes','api','state','sessionSummaries','sessionMetadataAt','sessionMetadataProbeAt','sessionMetadataLoading','sessionMetadataSupported','sessionLegacyCursor','enrichSessionPurposes','renderSessions','renderStorageInfo','setFilterOptions','renderList','selectSession'].map(key=>[key,context[key]]));
  const requests=[],renders=[],selections=[];
  let resolveSummaries,resolveEnrichment;
  const summaries=new Promise(resolve=>{resolveSummaries=resolve;}),enrichment=new Promise(resolve=>{resolveEnrichment=resolve;});
  try {
    vm.runInContext(source.slice(source.indexOf('async function refreshFilters('),source.indexOf('function hasTextSelectionWithin(')),context);
    Object.assign(context,{
      AbortSignal,state:{status:{storage:{revision:1}},sessions:[],filters:{thread:''}},sessionSummaries:[],
      sessionMetadataAt:0,sessionMetadataProbeAt:0,sessionMetadataLoading:false,sessionMetadataSupported:null,
      api:async path=>{requests.push(path);if(path==='/api/packet-filters')return {kinds:[],statuses:[],threads:['main','image']};if(path==='/api/session-summaries')return summaries;throw Error('Unexpected API request: '+path);},
      requestKinds:{generation:'Generation',compaction:'Compaction'},statusLabel:value=>value,
      setFilterOptions(){},renderStorageInfo(){},renderList(){},
      enrichSessionPurposes:async rows=>{await enrichment;return rows;},
      renderSessions:()=>renders.push(JSON.parse(JSON.stringify(context.sessionGroups()))),
      selectSession:async value=>{selections.push(value);context.state.filters.thread=value;context.renderSessions();},
    });
    await context.refreshFilters();
    assert.deepEqual(renders,[[]],'Raw session IDs must not be published as Other during startup');
    const loading=context.refreshSessionSummaries();
    context.renderSessions();
    assert.equal(context.sessionMetadataLoading,true);assert.deepEqual(renders.at(-1),[]);
    resolveSummaries({state:'available',sessions:[session('main',{title:'Frontend'}),session('image',{parent_thread_id:'main',session_kind:'image_analysis'})]});
    await new Promise(resolve=>setImmediate(resolve));context.renderSessions();
    assert.deepEqual(renders.at(-1),[],'Awaiting purpose enrichment must not expose provisional categories');
    resolveEnrichment();await loading;
    assert.deepEqual(selections,['thread:main']);
    assert.deepEqual(renders.at(-1).map(group=>[group.thread_id,group.members.map(row=>row.thread_id)]),[['main',['main','image']]]);
    assert.ok(renders.every(rows=>rows.every(group=>group.thread_id!=='group:other')));
    assert.deepEqual(requests,['/api/packet-filters','/api/session-summaries']);
    assert.equal(context.sessionMetadataLoading,false);passed++;
    const stale=session('main',{title:null,updated_at:200});
    context.api=async path=>{assert.equal(path,'/api/session-summaries');return {state:'available',sessions:[stale,session('image',{parent_thread_id:'main',session_kind:'image_analysis'})]};};
    context.enrichSessionPurposes=async rows=>{assert.equal(rows[0].title,null,'Purpose detection still sees missing native names');return rows;};
    context.sessionMetadataAt=0;
    await context.refreshSessionSummaries();
    assert.equal(context.sessionSummaries[0].title,'Frontend');assert.equal(context.sessionSummaries[0].title_cached,true);
    assert.equal(context.sessionSummaries[0].updated_at,200);assert.equal(context.state.filters.thread,'thread:main');
    assert.deepEqual(renders.at(-1).map(group=>[group.thread_id,group.title]),[['main','Frontend']]);passed++;
    Object.assign(context,{URLSearchParams,sessionLegacyCursor:0,uploadBytes:packet=>packet.outbound_wire_bytes,
      enrichSessionPurposes:async rows=>rows,
      api:async path=>{if(path==='/api/session-summaries')throw Error('Fixture metadata unavailable');
        assert.ok(path.startsWith('/api/packets?'));return [{id:'latest',created:300,outbound_wire_bytes:400}];},
      sessionMetadataAt:0,
    });
    await context.refreshSessionSummaries();
    assert.equal(context.sessionMetadataSupported,false);assert.equal(context.sessionSummaries[0].title,'Frontend');
    assert.equal(context.sessionSummaries[0].title_cached,true);assert.equal(context.sessionSummaries[0].updated_at,300);
    assert.equal(context.state.filters.thread,'thread:main');assert.equal(context.sessionSummaries[0].latest_packet.wire_bytes,400);passed++;
  } finally {Object.assign(context,saved);}
  console.log(JSON.stringify({passed}));
})().catch(error=>{console.error(error);process.exitCode=1;});
