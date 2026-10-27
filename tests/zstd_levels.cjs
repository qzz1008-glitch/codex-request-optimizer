const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('packet_inspector/ui/app.js', 'utf8');
const elements = new Map();
const document = {activeElement:null};
class Element {
  constructor(tag='div', className='', text='') {
    Object.assign(this,{tag,className,textContent:text,children:[],dataset:{},attributes:{},value:'',disabled:false,checked:false,open:false,events:[]});
    this.classList={toggle:()=>{}};
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children=children; }
  setAttribute(key,value) { this.attributes[key]=value; }
  dispatchEvent(event) { this.events.push(event.type); }
  focus() { document.activeElement=this; }
  scrollIntoView() {}
  close() { this.open=false; }
  querySelectorAll(selector) {
    const result=[];
    for(const child of this.children) {
      if(selector==='.'+child.className)result.push(child);
      result.push(...child.querySelectorAll(selector));
    }
    return result;
  }
}
const context={document,Event:class {constructor(type){this.type=type;}},
  $:id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},
  el:(...args)=>new Element(...args),t:key=>key,
  state:{status:{zstd_level:16}},settingsEditing:true,settingsSaving:false};
vm.createContext(context);
const start=source.indexOf('const zstdLevelEstimates = [');
vm.runInContext(source.slice(start,source.indexOf('\n];',start)+3),context);
vm.runInContext(source.slice(source.indexOf('function fillZstdLevel('),source.indexOf('function renderLanguageSelection(')),context);
const estimates=JSON.parse(vm.runInContext('JSON.stringify(zstdLevelEstimates)',context));
assert.deepEqual(estimates.map(row=>row[0]),Array.from({length:22},(_,i)=>i+1));
for(const [,small,large,time] of estimates) {
  assert(small>0&&small<100&&large>0&&large<100);
  assert(Number.isFinite(time)&&time>=0);
}
assert(estimates.some(([,small,large])=>small!==large));
context.$('zstd-level').value='16';
context.$('settings-dialog').open=true;
context.renderZstdLevelRows();
const rows=context.$('zstd-level-options').children;
assert.equal(rows.length,22);
const last=rows.at(-1),data=estimates.at(-1);
assert.equal(last.dataset.level,'22');
assert.equal(last.children.length,4);
assert.equal(last.children[1].textContent,'-'+data[1].toFixed(2)+'%');
assert.equal(last.children[2].textContent,'-'+data[2].toFixed(2)+'%');
assert.equal(last.children[3].textContent,'+'+data[3].toFixed(3)+'%');
let prevented=0;
rows[0].children[0].children[0].focus();
context.navigateZstdLevels({key:'End',preventDefault:()=>prevented++});
assert.equal(document.activeElement,last.children[0].children[0]);
assert.equal(prevented,1);
context.$('zstd-level-dialog').open=true;
context.selectZstdLevel('22');
assert.equal(context.$('zstd-level').value,'22');
assert.deepEqual(context.$('zstd-level').events,['change']);
assert.equal(context.$('zstd-level-dialog').open,false);
context.selectZstdLevel(23);
assert.equal(context.$('zstd-level').value,'22');
assert.deepEqual(context.$('zstd-level').events,['change']);
context.$('packet-size-protection').checked=true;
context.$('auto-zstd-level').checked=true;
context.selectZstdLevel(18);
assert.equal(context.$('zstd-level').value,'22');
assert.equal(context.$('zstd-level-open').disabled,true);
assert.deepEqual(context.$('zstd-level').events,['change']);
context.settingsEditing=false;
context.state.status.zstd_level=22;
context.fillZstdLevel();
assert.equal(context.$('zstd-level-value').textContent,'22级');
const html=fs.readFileSync('packet_inspector/ui/index.html','utf8');
assert(html.includes('scope="colgroup" colspan="2"'));
const translations=fs.readFileSync('packet_inspector/ui/i18n.js','utf8');
const dictionary=vm.runInNewContext(translations.slice(translations.indexOf('const english = {'),translations.indexOf('\n  };')+5)+'\nenglish;');
for(const label of ['原始包＜1.5 MB','原始包≥1.5 MB']) {
  assert(html.includes(label));
  assert(dictionary[label]);
}
console.log('zstd level selection: 1–22, two size groups, keyboard, settings and automatic-mode checks passed');
