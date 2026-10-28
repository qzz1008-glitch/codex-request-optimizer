/* Storage controls preserve unsaved paths while status polls continue. */
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('packet_inspector/ui/app.js','utf8');
function extract(start,end) {
  const first=source.indexOf(start),last=source.indexOf(end,first);
  assert(first>=0&&last>first,`Missing function range: ${start}`);
  return source.slice(first,last);
}
const elements=new Map();
const context={
  state:{status:{data_dir:'C:\\Inspector',storage_paths_supported:true,image_save_dir_supported:true}},
  settingsEditing:false,settingsSaving:false,settingsLanguage:null,uiI18n:{language:'zh-CN'},
  selectedImageAnalysisMode:()=>'native',t:value=>value,
  $:id=>{
    if(!elements.has(id))elements.set(id,{value:'',textContent:'',checked:false,disabled:false});
    return elements.get(id);
  }
};
vm.createContext(context);
vm.runInContext(extract('function imageDefaultDirectory()','function fillEntryPort()'),context);
vm.runInContext(extract('function readSettingsDraft()','function changedSettings('),context);

context.fillImageDirectory();
assert.equal(context.$('log-dir').value,'C:\\Inspector');
assert.equal(context.$('cache-dir').value,'C:\\Inspector');
assert.equal(context.$('image-save-dir').value,'C:\\Inspector\\image-attachments');
assert.equal(context.$('log-dir').disabled,false);
assert.equal(context.$('image-save-dir').disabled,true);

context.settingsEditing=true;
context.$('log-dir').value='D:\\Logs';context.$('cache-dir').value='E:\\Cache';
context.state.status.log_dir='C:\\OldLogs';context.state.status.storage_paths_pending=true;
context.fillImageDirectory();
assert.equal(context.$('log-dir').value,'D:\\Logs');
assert.equal(context.$('cache-dir').value,'E:\\Cache');
assert(context.$('storage-paths-hint').textContent.includes('重启本软件'));
const draft=context.readSettingsDraft();
assert.equal(draft.log_dir,'D:\\Logs');assert.equal(draft.cache_dir,'E:\\Cache');

context.settingsEditing=false;context.fillImageDirectory();
assert.equal(context.$('log-dir').value,'C:\\OldLogs');
context.settingsSaving=true;context.fillImageDirectory();
assert.equal(context.$('log-dir').disabled,true);
assert.equal(context.$('cache-dir-reset').disabled,true);

const html=fs.readFileSync('packet_inspector/ui/index.html','utf8');
const section=html.slice(html.indexOf('<section id="storage-location-settings"')).split('</section>')[0];
for(const id of ['log-dir','cache-dir','image-save-dir'])assert(section.includes(`id="${id}"`));
console.log('Storage paths: grouped controls, draft preservation, restart notice and save locking passed.');
