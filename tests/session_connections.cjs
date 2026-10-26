/* Exercise the actual sidebar relationship selector with synthetic session metadata. */
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('packet_inspector/ui/app.js','utf8'),context={};
vm.createContext(context);
const declarations=['OTHER_SESSION_GROUP','auxiliaryLabels'].map(name=>source.match(new RegExp('^const '+name+'=.*$','m'))[0]);
const kind=source.match(/^function auxiliarySessionKind\(row\).*$/m)[0];
vm.runInContext(declarations.join('\n')+'\n'+kind+'\n'+source.slice(source.indexOf('function sessionConnections('),source.indexOf('function renderSessionConnections(')),context);
const group=(id,extra={})=>({thread_id:id,members:[{thread_id:id}],...extra});
const links=(rows,selected)=>JSON.parse(JSON.stringify(context.sessionConnections(rows,selected)));
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
console.log(JSON.stringify({passed}));
