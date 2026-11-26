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
  showModal() { this.open=true; }
  close() { this.open=false; }
  querySelector(selector) {
    if(selector===".zstd-level-option[aria-pressed='true']")return this.querySelectorAll('.zstd-level-option').find(option=>option.attributes['aria-pressed']==='true')||null;
    return this.querySelectorAll(selector)[0]||null;
  }
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
  uiI18n:{text:(node,value)=>{node.textContent=value;}},
  state:{status:{zstd_level:16,auto_zstd_level_enabled:false,auto_image_zstd_level_enabled:false}},settingsEditing:true,settingsSaving:false};
const manualHint='手动选择 zstd 压缩等级';
const protectionHint='手动选择 zstd 压缩等级。开启大小保护后可切换为自动';
const conversationAutoHint='按大小保护上限动态调整对话 zstd 压缩策略';
const imageAutoHint='按大小保护上限动态调整图片 zstd 压缩策略';
vm.createContext(context);
for(const name of ['zstdLevelEstimates','imageZstdLevelEstimates']) {
  const start=source.indexOf(`const ${name} = [`);
  vm.runInContext(source.slice(start,source.indexOf('\n];',start)+3),context);
}
vm.runInContext(source.slice(source.indexOf('function fillZstdLevel('),source.indexOf('function renderLanguageSelection(')),context);
const estimates=JSON.parse(vm.runInContext('JSON.stringify(zstdLevelEstimates)',context));
const imageEstimates=JSON.parse(vm.runInContext('JSON.stringify(imageZstdLevelEstimates)',context));
assert.deepEqual(estimates.map(row=>row[0]),Array.from({length:22},(_,i)=>i+1));
assert.deepEqual(imageEstimates.map(row=>row[0]),Array.from({length:22},(_,i)=>i+1));
for(const [,small,large,time] of [...estimates,...imageEstimates]) {
  assert(small>0&&small<100&&large>0&&large<100);
  assert(Number.isFinite(time)&&time>=0);
}
assert(estimates.some(([,small,large])=>small!==large));
context.$('zstd-level').value='16';
context.$('settings-dialog').open=true;
context.renderZstdLevelRows();
const rows=context.$('zstd-level-options').children;
assert.equal(rows.length,22);
// The page CSP permits external stylesheets and blocks inline style attributes.
const stylesheet=fs.readFileSync('packet_inspector/ui/style.css','utf8');
const qualityClasses=new Set([...stylesheet.matchAll(/\.zstd-quality-\d+/g)].map(match=>match[0].slice(1)));
for(const row of rows)for(const cell of row.children.slice(1)) {
  assert(!Object.hasOwn(cell.attributes,'style'));
  assert(cell.className.split(/\s+/).some(name=>qualityClasses.has(name)));
}
const last=rows.at(-1),data=estimates.at(-1);
assert.equal(last.dataset.level,'22');
assert.equal(last.children.length,4);
assert.equal(last.children[1].textContent,'-'+data[1].toFixed(2)+'%');
assert.equal(last.children[2].textContent,'-'+data[2].toFixed(2)+'%');
assert.equal(last.children[3].textContent,data[3].toFixed(3)+' ms');
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
context.state.status.image_zstd_level=12;
context.state.status.auto_image_zstd_level=6;
context.fillZstdLevel();
assert.equal(context.$('image-zstd-level').value,'12');
assert.equal(context.$('image-zstd-level-open').disabled,false);
assert.equal(context.$('image-zstd-level-value').textContent,'12级');
context.$('auto-image-zstd-level').checked=true;
context.fillZstdLevel();
assert.equal(context.$('image-zstd-level').disabled,true);
assert.equal(context.$('image-zstd-level-open').disabled,true);
assert.equal(context.$('image-zstd-level-value').textContent,'6级');
assert.equal(context.$('image-zstd-level-hint').textContent,imageAutoHint);
context.settingsEditing=true;
context.$('auto-zstd-level').checked=false;
context.fillZstdLevel();
assert.equal(context.$('zstd-level-open').disabled,false);
assert.equal(context.$('image-zstd-level-open').disabled,true);
context.$('auto-image-zstd-level').checked=false;
context.fillZstdLevel();
assert.equal(context.$('image-zstd-level').disabled,false);
assert.equal(context.$('image-zstd-level-open').disabled,false);
assert.equal(context.$('image-zstd-level-value').textContent,'12级');
assert.equal(context.$('image-zstd-level-hint').textContent,manualHint);
context.$('image-zstd-level').value='17';
context.$('auto-image-zstd-level').checked=true;
context.fillZstdLevel();
assert.equal(context.$('image-zstd-level').value,'17');
assert.equal(context.$('image-zstd-level-value').textContent,'6级');
context.$('packet-size-protection').checked=false;
context.fillZstdLevel();
assert.equal(context.$('image-zstd-level').value,'17');
assert.equal(context.$('image-zstd-level').disabled,false);
assert.equal(context.$('image-zstd-level-value').textContent,'17级');
context.settingsSaving=true;
context.fillZstdLevel();
assert.equal(context.$('image-zstd-level').disabled,true);
context.settingsSaving=false;
context.fillZstdLevel();
context.openZstdLevelPicker('image');
assert.equal(context.$('zstd-level-dialog').open,true);
assert.equal(context.$('zstd-level-title').textContent,'选择图片压缩等级');
assert(context.$('zstd-level-note').textContent.includes('7种生成测试图'));
const imageRows=context.$('zstd-level-options').children;
assert.equal(imageRows.length,22);
assert.equal(imageRows[5].children[1].textContent,'-'+imageEstimates[5][1].toFixed(2)+'%');
assert.equal(imageRows[5].children[3].textContent,'1.871 ms');
assert.equal(context.$('image-zstd-level-open').attributes['aria-expanded'],'true');
context.selectZstdLevel(7);
assert.equal(context.$('image-zstd-level').value,'7');
assert.equal(context.$('zstd-level').value,'22');
assert.equal(document.activeElement,context.$('image-zstd-level-open'));
assert.equal(context.$('zstd-level-dialog').open,false);
context.openZstdLevelPicker('conversation');
assert.equal(context.$('zstd-level-title').textContent,'选择对话压缩等级');
assert(context.$('zstd-level-note').textContent.includes('120条请求'));
assert.equal(context.$('zstd-level-options').children[0].children[3].textContent,'2.827 ms');
context.closeZstdLevelPicker();
context.openZstdLevelPicker('image');
context.$('packet-size-protection').checked=true;
context.$('auto-image-zstd-level').checked=true;
context.fillZstdLevel();
assert.equal(context.$('zstd-level-dialog').open,false);
context.selectZstdLevel(18);
assert.equal(context.$('image-zstd-level').value,'7');
assert.equal(context.$('image-zstd-level-value').textContent,'6级');

vm.runInContext(source.slice(source.indexOf('function fillPacketProtection()'),
  source.indexOf('$("packet-size-protection").addEventListener')),context);
context.settingsEditing=false;
Object.assign(context.state.status,{packet_size_protection_enabled:true,auto_zstd_level_enabled:true,auto_image_zstd_level_enabled:false,budget_bytes:1000000});
context.fillPacketProtection();
assert.equal(context.$('auto-zstd-level').checked,true);
assert.equal(context.$('manual-zstd-level').checked,false);
assert.equal(context.$('auto-zstd-level-hint').textContent,conversationAutoHint);
assert.equal(context.$('manual-image-zstd-level').checked,true);
assert.equal(context.$('image-zstd-level-open').disabled,false);
context.settingsEditing=true;
context.$('packet-size-protection').checked=false;
context.fillPacketProtection();
assert.equal(context.$('manual-zstd-level').checked,true);
assert.equal(context.$('auto-zstd-level').disabled,true);
assert.equal(context.$('manual-image-zstd-level').checked,true);
assert.equal(context.$('auto-image-zstd-level').disabled,true);
assert.equal(context.$('auto-zstd-level-hint').textContent,protectionHint);
assert.equal(context.$('image-zstd-level-hint').textContent,protectionHint);
assert.equal(context.$('zstd-level-open').disabled,false);
assert.equal(context.$('image-zstd-level-open').disabled,false);

// Older relays still apply one mode to both request types.
delete context.state.status.auto_image_zstd_level_enabled;
context.settingsEditing=false;
context.fillPacketProtection();
assert.equal(context.$('auto-image-zstd-level').checked,true);
assert.equal(context.$('auto-image-zstd-level').disabled,true);
assert.equal(context.$('image-zstd-level-hint').textContent,imageAutoHint);
context.state.status.auto_zstd_level_enabled=false;
context.fillPacketProtection();
assert.equal(context.$('image-zstd-level-hint').textContent,manualHint);

const html=fs.readFileSync('packet_inspector/ui/index.html','utf8');
assert(html.includes('scope="colgroup" colspan="2"'));
assert(html.includes('data-settings-field="image_zstd_level"'));
assert(!html.includes('image-upload-optimization'));
const translations=fs.readFileSync('packet_inspector/ui/i18n.js','utf8');
const englishStart=translations.indexOf('const english = {');
const dictionary=vm.runInNewContext(translations.slice(englishStart,translations.indexOf('\n  };',englishStart)+5)+'\nenglish;');
for(const label of ['原始请求＜1.5 MB','原始请求≥1.5 MB','手动','自动',protectionHint,'压缩耗时']) {
  assert(html.includes(label));
  assert(dictionary[label]);
}
assert(dictionary[conversationAutoHint]&&dictionary[imageAutoHint]&&dictionary[manualHint]);
for(const label of ['对话压缩','图片压缩']) {
  assert(html.includes(`class="settings-key">${label}</span>`));
  assert(dictionary[label]);
}
console.log('Both compression pickers: levels 1–22, separate size/time profiles, keyboard, settings and smart-mode checks passed');
