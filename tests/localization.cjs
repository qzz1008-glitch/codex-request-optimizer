/* Language previews, live UI messages and cached request panels stay in sync. */
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const translations=fs.readFileSync('packet_inspector/ui/i18n.js','utf8');
const app=fs.readFileSync('packet_inspector/ui/app.js','utf8');
const elements=new Map(),storage=new Map([['packet-inspector-language','zh']]);
const node=id=>{
  if(!elements.has(id))elements.set(id,{id,isConnected:true,textContent:'',value:'',hidden:true,open:false,
    dataset:{},disabled:false,setAttribute(name,value){this[name]=value;}});
  return elements.get(id);
};
const buttons=['zh','en'].map(language=>Object.assign(node('language-'+language),{dataset:{language}}));
const label={isConnected:true,nodeValue:' 设置 ',parentElement:{dataset:{},closest:()=>null}};
const context={document:{documentElement:{lang:'',classList:{contains:()=>false}},
  createTreeWalker:()=>({currentNode:null,nextNode(){if(this.currentNode)return false;this.currentNode=label;return true;}}),
  querySelectorAll:selector=>selector.startsWith('#language-toggle')?buttons:[]},
  NodeFilter:{SHOW_TEXT:4},localStorage:{getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value)}};
vm.createContext(context);vm.runInContext(translations,context);
const i18n=vm.runInContext('uiI18n',context);
const notice=node('notice'),progress=node('progress'),original=node('raw-request');
original.textContent='保存失败：这是一段用户原文';
i18n.text(notice,'未找到 PowerShell，无法安装独立图片工具。');
i18n.text(progress,()=>i18n.t('正在保存设置…')+' 2/3');
i18n.setLanguage('en',{persist:false});
assert.equal(label.nodeValue,' Settings ');
assert.equal(storage.get('packet-inspector-language'),'zh','preview must not change the stored language');
assert.equal(notice.textContent,'PowerShell was not found. The image tool could not be installed.');
assert.equal(progress.textContent,'Saving settings… 2/3');
assert.equal(i18n.t('已接入'),'Connected');assert.equal(i18n.t('未接入'),'Disconnected');
assert.equal(i18n.t('目录无法写入：PermissionError'),'The folder is not writable: PermissionError');
assert.equal(i18n.t('检查或升级失败：正式版文件 SHA-256 校验失败。'),
  'Update check or upgrade failed: The release file failed SHA-256 verification.');
assert.equal(original.textContent,'保存失败：这是一段用户原文');
i18n.setLanguage('en');
assert.equal(storage.get('packet-inspector-language'),'en','saving an already-previewed language must persist it');
i18n.setLanguage('zh');
assert.equal(notice.textContent,'未找到 PowerShell，无法安装独立图片工具。');
assert.equal(progress.textContent,'正在保存设置… 2/3');

// Static interface labels, including titles and accessible names, have entries.
const dictionary=vm.runInNewContext(translations.slice(translations.indexOf('const english = {'),translations.indexOf('\n  };')+5)+'\nenglish;');
for(const file of ['index.html','tray.html']) {
  const html=fs.readFileSync('packet_inspector/ui/'+file,'utf8');
  const texts=[...html.matchAll(/>([^<>]+)</g)].map(match=>match[1].trim());
  const attributes=[...html.matchAll(/(?:title|placeholder|aria-label)="([^"]+)"/g)].map(match=>match[1]);
  for(const text of [...texts,...attributes])if(/[\u4e00-\u9fff]/u.test(text)&&text!=='中文')
    assert(Object.hasOwn(dictionary,text),`${file}: missing translation for ${text}`);
}

const calls=[],handlers=new Map();
Object.assign(context,{
  $:node,interfaceLanguageRevision:0,settingsSaving:false,settingsEditing:true,settingsLanguage:'zh',
  settingsBaseline:{language:'zh'},contextSettings:null,tokenTrendView:null,
  state:{status:{ui_language:'zh',packet_size_protection_enabled:true},detail:{id:'request',raw:original.textContent},
    selected:'request',path:'',tab:'headers'},
  action:(id,handler)=>handlers.set(id,handler),fail:error=>{throw error;},
  loadTree:async path=>{node('tree-query').value=path;},loadDiff:async()=>{},refreshFilters:async()=>{},
});
for(const name of ['renderStatus','renderContextPlaceholders','renderSettingsSaveProgress','renderUpdates',
  'renderConnectPrompt','renderExitRequest','showTab','renderOverview','renderCompression','renderHeaders',
  'renderLanguageSelection','updateSettingsSaveState','resetSettingsSaveProgress','fillSettings',
  'fillContextSettings','setSettingsMessage'])context[name]=(...args)=>calls.push([name,...args]);
function extract(start,end) {
  const first=app.indexOf(start),last=app.indexOf(end,first);
  assert(first>=0&&last>first);return app.slice(first,last);
}
vm.runInContext(extract('async function setInterfaceLanguage(', 'action("settings-open",openSettings);'),context);
vm.runInContext(extract('function discardSettingsDraft()', 'function restoreSettings()'),context);
(async()=>{
  await handlers.get('language-en')();
  assert.equal(i18n.language,'en');assert.equal(context.settingsLanguage,'en');
  assert.equal(storage.get('packet-inspector-language'),'zh');
  assert(calls.some(([name,fill])=>name==='renderStatus'&&fill===true));
  assert(calls.some(([name])=>name==='renderCompression'),'request size and protection panels must repaint');
  assert(calls.some(([name])=>name==='renderUpdates'));
  assert.equal(context.state.detail.raw,original.textContent);
  await context.discardSettingsDraft();
  assert.equal(i18n.language,'zh');assert.equal(context.settingsLanguage,null);
  assert.equal(storage.get('packet-inspector-language'),'zh');
  console.log('Localization: previews, save/restore, live messages, tray labels and request panel repaint covered');
})().catch(error=>{console.error(error);process.exitCode=1;});
