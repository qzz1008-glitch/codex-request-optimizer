const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');

const source=fs.readFileSync('packet_inspector/ui/app.js','utf8');
const context=vm.createContext({t:text=>text,kind:text=>text,requestBodyView:null});
for(const [first,last] of [['inputGroupRows','fieldBreakdown'],['packetDiffItems','isPreviousComparisonPacket']]) {
  const start=source.indexOf(`function ${first}(`),end=source.indexOf(`function ${last}(`,start);
  assert(start>=0&&end>start);
  vm.runInContext(source.slice(start,end),context);
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
  assert.equal(missing.encrypted.items,undefined);
  assert.equal(missing.plain.items,undefined);
}

{
  const rows=breakdown(groups(140,80),groups(124,70),{
    removed:[item(0,100,80),item(1,40,0)],added:[item(0,124,70)]
  });
  assert.deepEqual(changes(rows.encrypted),[[0,'shrink',-10]]);
  assert.deepEqual(changes(rows.plain),[[0,'grow',34],[1,'removed',-40]]);
}

{
  const rows=breakdown(groups(100,80),groups(110,80),{removed:[item(0,100,80)],added:[item(0,110,80)]});
  assert.deepEqual(changes(rows.encrypted),[]);
  assert.deepEqual(changes(rows.plain),[[0,'grow',10]]);
}

console.log('3 size breakdown cases passed');
