/* Storage controls preserve unsaved paths while status polls continue. */
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('packet_inspector/ui/app.js','utf8');
function extract(start,end) {
  const first=source.indexOf(start),last=source.indexOf(end,first);
  assert(first>=0&&last>first,`Missing function range: ${start}`);
  return source.slice(first,last);
}
const elements=new Map();
const requests=[],messages=[];
const context={
  state:{status:{data_dir:'C:\\Inspector',storage_paths_supported:true,image_save_dir_supported:true,open_directory_supported:true}},
  settingsEditing:false,settingsSaving:false,settingsLanguage:null,uiI18n:{language:'zh-CN'},
  directoryOpening:new Set(),
  api:async(path,data)=>{requests.push({path,data});return {opened:true};},
  setSettingsMessage:(text,error=false)=>messages.push({text,error}),
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
assert.equal(context.$('image-save-dir-open').disabled,false);

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
for(const id of ['log-dir','cache-dir','image-save-dir'])assert(section.includes(`id="${id}-open"`));

(async()=>{
  context.settingsSaving=false;context.settingsEditing=true;
  const expected=['D:\\Logs','E:\\Cache','F:\\Images'];
  for(const [i,id] of ['log-dir','cache-dir','image-save-dir'].entries()) {
    context.$(id).value=expected[i];
    await context.openSettingsDirectory(id);
    assert.equal(context.$(id).value,expected[i]);
  }
  assert.deepEqual(requests.map(request=>request.path),Array(3).fill('/api/open-directory'));
  assert.deepEqual(requests.map(request=>request.data.path),expected);

  context.$('cache-dir').value='  ';
  await context.openSettingsDirectory('cache-dir');
  assert.equal(requests.at(-1).data.path,context.$('cache-dir').placeholder);
  context.settingsSaving=true;
  await context.openSettingsDirectory('log-dir');
  context.settingsSaving=false;context.state.status.open_directory_supported=false;
  context.fillImageDirectory();await context.openSettingsDirectory('log-dir');
  assert.equal(context.$('log-dir-open').disabled,true);
  assert.equal(requests.length,4);

  context.state.status.open_directory_supported=true;
  let finish,calls=0;
  context.api=()=>{calls++;return new Promise(resolve=>{finish=resolve;});};
  const pending=context.openSettingsDirectory('log-dir');
  context.fillImageDirectory();
  assert.equal(context.$('log-dir-open').disabled,true);
  await context.openSettingsDirectory('log-dir');
  assert.equal(calls,1);
  finish({opened:true});await pending;
  assert.equal(context.$('log-dir-open').disabled,false);

  context.api=async()=>{throw Error('目录不存在。');};
  await context.openSettingsDirectory('image-save-dir');
  assert.equal(messages.at(-1).text,'目录不存在。');assert.equal(messages.at(-1).error,true);
  assert.equal(context.$('image-save-dir-open').disabled,false);
  console.log('Storage paths: draft preservation, directory actions, pending locks and error recovery passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
