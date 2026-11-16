/* The unified folder control preserves edits and existing data locations. */
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('packet_inspector/ui/app.js','utf8');
function extract(start,end) {
  const first=source.indexOf(start),last=source.indexOf(end,first);
  assert(first>=0&&last>first,`Missing function range: ${start}`);
  return source.slice(first,last);
}
const elements=new Map(),requests=[],messages=[];
const context={
  state:{status:{data_dir:'C:\\Inspector',storage_dir_supported:true,storage_layout:'grouped',open_directory_supported:true}},
  settingsEditing:false,settingsSaving:false,settingsBaseline:null,settingsLanguage:null,uiI18n:{language:'zh-CN'},
  directoryOpening:new Set(),updateSettingsSaveState:()=>{},
  api:async(path,data)=>{requests.push({path,data});return {opened:true};},
  setSettingsMessage:(text,error=false)=>messages.push({text,error}),
  selectedImageAnalysisMode:()=>'native',t:value=>value,
  $:id=>{
    if(!elements.has(id))elements.set(id,{value:'',textContent:'',checked:false,disabled:false,dataset:{}});
    return elements.get(id);
  }
};
vm.createContext(context);
vm.runInContext(extract('function fillStorageDirectories()','function fillEntryPort()'),context);
vm.runInContext(extract('function readSettingsDraft()','function setSettingsMessage('),context);

context.fillStorageDirectories();
assert.equal(context.$('storage-dir').value,'C:\\Inspector');
assert.equal(context.$('storage-dir').disabled,false,'Data folder is usable in native image mode');
assert.equal(context.$('storage-dir-open').disabled,false);
context.settingsBaseline=context.readSettingsDraft();
context.settingsEditing=true;
context.$('storage-dir').value='D:\\Data';
context.state.status.storage_dir='C:\\OldData';context.state.status.storage_paths_pending=true;
context.fillStorageDirectories();
assert.equal(context.$('storage-dir').value,'D:\\Data');
assert(context.$('storage-paths-hint').textContent.includes('重启本软件'));
assert.equal(context.readSettingsDraft().storage_dir,'D:\\Data');
assert.deepEqual(Array.from(context.changedSettings()),['storage_dir']);

context.settingsEditing=false;context.fillStorageDirectories();
assert.equal(context.$('storage-dir').value,'C:\\OldData');
context.settingsSaving=true;context.fillStorageDirectories();
assert.equal(context.$('storage-dir').disabled,true);
assert.equal(context.$('storage-dir-reset').disabled,true);

context.settingsSaving=false;context.state.status.storage_dir='C:\\Inspector';
context.state.status.storage_layout='legacy';context.fillStorageDirectories();
context.settingsBaseline=context.readSettingsDraft();
context.resetStorageDirectory();
assert.equal(context.changedSettings().length,0,'Same-path reset does not relocate legacy records');
context.state.status.storage_layout='grouped';
context.resetStorageDirectory();
assert.equal(context.changedSettings().length,0,'Grouped default reset does not dirty unchanged settings');

const html=fs.readFileSync('packet_inspector/ui/index.html','utf8');
const section=html.slice(html.indexOf('<section id="storage-location-settings"')).split('</section>')[0];
assert.equal((section.match(/<input /g)||[]).length,1);
for(const id of ['storage-dir','storage-dir-open','storage-dir-reset'])assert(section.includes(`id="${id}"`));
for(const id of ['log-dir','cache-dir','image-save-dir'])assert(!html.includes(`id="${id}"`));

(async()=>{
  context.settingsEditing=true;context.$('storage-dir').value='D:\\Data';
  await context.openSettingsDirectory('storage-dir');
  assert.equal(context.$('storage-dir').value,'D:\\Data');
  assert.equal(requests.at(-1).path,'/api/open-directory');
  assert.equal(requests.at(-1).data.path,'D:\\Data');
  context.$('storage-dir').value='  ';
  await context.openSettingsDirectory('storage-dir');
  assert.equal(requests.at(-1).data.path,'C:\\Inspector');
  context.settingsSaving=true;
  await context.openSettingsDirectory('storage-dir');
  context.settingsSaving=false;context.state.status.open_directory_supported=false;
  context.fillStorageDirectories();await context.openSettingsDirectory('storage-dir');
  assert.equal(context.$('storage-dir-open').disabled,true);
  assert.equal(requests.length,2);

  context.state.status.open_directory_supported=true;
  let finish,calls=0;
  context.api=()=>{calls++;return new Promise(resolve=>{finish=resolve;});};
  const pending=context.openSettingsDirectory('storage-dir');
  context.fillStorageDirectories();
  assert.equal(context.$('storage-dir-open').disabled,true);
  await context.openSettingsDirectory('storage-dir');
  assert.equal(calls,1);
  finish({opened:true});await pending;
  assert.equal(context.$('storage-dir-open').disabled,false);
  context.api=async()=>{throw Error('目录不存在。');};
  await context.openSettingsDirectory('storage-dir');
  assert.equal(messages.at(-1).text,'目录不存在。');assert.equal(messages.at(-1).error,true);
  assert.equal(context.$('storage-dir-open').disabled,false);
  context.state.status.storage_dir_supported=false;context.fillStorageDirectories();
  assert.equal(context.$('storage-dir').disabled,true);
  console.log('Unified storage: edits, legacy preservation, directory actions and error recovery passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
