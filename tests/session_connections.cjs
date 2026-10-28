/* Exercise sidebar grouping and relationship selection with synthetic session metadata. */
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('packet_inspector/ui/app.js','utf8'),context={t:value=>value};
vm.createContext(context);
const declarations=['OTHER_SESSION_GROUP','auxiliaryLabels'].map(name=>source.match(new RegExp('^const '+name+'=.*$','m'))[0]);
const kind=source.match(/^function auxiliarySessionKind\(row\).*$/m)[0];
vm.runInContext(declarations.join('\n')+'\n'+kind+'\n'+source.slice(source.indexOf('function sessionConnections('),source.indexOf('function renderSessionConnections(')),context);
vm.runInContext(source.slice(source.indexOf('function sessionGroups('),source.indexOf('function purposeFromMetadata(')),context);
const group=(id,extra={})=>({thread_id:id,members:[{thread_id:id}],...extra});
const links=(rows,selected)=>JSON.parse(JSON.stringify(context.sessionConnections(rows,selected)));
const session=(id,extra={})=>({thread_id:id,packet_count:1,updated_at:100,...extra});
const grouped=rows=>{
 context.sessionSummaries=rows;
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
  assert.equal(groups.length,1);assert.equal(groups[0].thread_id,'main');assert.equal(groups[0].title,'main');
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
test('parent metadata refresh moves an image out of other and resolves child selections to the owner',()=>{
 const rows=[session('main',{title:'Frontend'}),session('image',{session_kind:'image_analysis'})];
 assert.equal(grouped(rows).find(group=>group.thread_id==='group:other').members[0].thread_id,'image');
 rows[1]={...rows[1],parent_thread_id:'main'};
 const groups=grouped(rows);context.state.filters.thread='thread:image';
 assert.equal(groups.length,1);assert.equal(context.selectedSessionGroup().thread_id,'main');
 assert.equal(context.otherTaskSessions().length,0);
 rows[0].title='Renamed frontend';assert.equal(grouped(rows)[0].title,'Renamed frontend');
});
console.log(JSON.stringify({passed}));
