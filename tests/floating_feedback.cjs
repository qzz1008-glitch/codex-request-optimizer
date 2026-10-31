/* Isolated browser checks; no live service, application or user profile. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
let chromium;
try{({chromium}=require('playwright'));}catch(_){console.log(JSON.stringify({skipped:'Playwright is not installed'}));process.exit(0);}
const edge=path.join(process.env['ProgramFiles(x86)']||'C:/Program Files (x86)','Microsoft/Edge/Application/msedge.exe');
const executablePath=process.env.CRO_TEST_BROWSER||(process.platform==='win32'&&fs.existsSync(edge)?edge:chromium.executablePath());
if(!fs.existsSync(executablePath)){console.log(JSON.stringify({skipped:'No browser engine is installed'}));process.exit(0);}
const root=path.join(__dirname,'../packet_inspector/ui'),read=name=>fs.readFileSync(path.join(root,name),'utf8'),app=read('app.js');
const extract=(start,end)=>app.slice(app.indexOf(start),app.indexOf(end,app.indexOf(start)));
let browser,cases=0;
async function load(tray=false){
 const page=await browser.newPage({viewport:{width:1280,height:900}});
 await page.setContent(read(tray?'tray.html':'index.html').replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,'').replace(/<link\b[^>]*>/g,''));
 for(const file of tray?['tray.css']:['style.css','sharing.css'])await page.addStyleTag({content:read(file)});
 await page.addScriptTag({content:read('i18n.js')});
 await page.evaluate(()=>{
  window.t=value=>uiI18n.t(value);window.feedback=uiI18n.floatingFeedback();
  window.feedbackText=(id,value,error=false)=>{const node=document.getElementById(id);uiI18n.text(node,value);node.hidden=!value;node.classList.toggle('error',error);};
 });
 return page;
}
const settle=page=>page.evaluate(()=>new Promise(resolve=>setTimeout(resolve,0)));
const geometry=(page,ids)=>page.evaluate(ids=>ids.map(id=>{const r=document.getElementById(id).getBoundingClientRect();return [r.x,r.y,r.width,r.height];}),ids);
async function noPopup(page){await settle(page);assert.equal(await page.locator('.floating-feedback').evaluate(node=>node.hidden),true,'reviewed feedback must not open a floating notification');}
async function run(){
 browser=await chromium.launch({executablePath,headless:true});
 const page=await load();
 await page.addScriptTag({content:[
  "window.$=id=>document.getElementById(id);window.state={status:{version:'1.0.0',entry_port:{pending:false}}};",
  "window.updateStatus=null;window.updateBusy=false;window.settingsSaving=false;window.updateAction='';window.updateFeedbackVisible=true;",
  "window.settingsRestartChanges=new Map();window.connectionRestartNotified=false;window.closeZstdLevelPicker=()=>{};window.discardSettingsDraft=()=>{};",
  "function el(tag,cls,value){const node=document.createElement(tag);node.className=cls;node.textContent=value;return node;}",
  extract('function renderUpdates()','async function loadUpdates()'),
  extract('function showConfigurationFeedback(','function showConnectionFeedback('),
  extract('function setSettingsMessage(','function renderSettingsSaveProgress('),
  extract('function finishSettingsSave()','async function saveSettings()'),
  extract('function closeSettings()','action("settings-close"'),
  extract('function message(','function fail(')
 ].join('\n')});
 await page.evaluate(()=>document.getElementById('settings-dialog').showModal());
 for(const width of [1120,1600]){await page.setViewportSize({width,height:900});
 for(const language of ['zh','en']){
  await page.evaluate(language=>{uiI18n.setLanguage(language,{persist:false});updateStatus={state:'idle',current_version:'1.0.0'};renderUpdates();},language);
  const before=await geometry(page,['updates-settings','settings-dialog','settings-save','settings-restore']);
  for(const status of ['checking','current','available','downloading','upgrading','complete','error']){
   await page.evaluate(status=>{updateStatus={state:status,current_version:'1.0.0',latest_version:'1.1.0',error:'Synthetic update error '.repeat(30)};renderUpdates();},status);
   await noPopup(page);assert(await page.locator('#updates-message').isVisible());
   assert.equal(await page.locator('#updates-message').getAttribute('title'),await page.locator('#updates-message').textContent());
   const [version,result]=await geometry(page,['updates-versions','updates-message']);
   assert(result[0]>=version[0]+version[2]&&Math.abs(result[1]+result[3]/2-version[1]-version[3]/2)<=1);
   assert.deepEqual(await geometry(page,['updates-settings','settings-dialog','settings-save','settings-restore']),before);
  }cases++;
 }}
 await page.setViewportSize({width:1280,height:900});
 await page.evaluate(()=>uiI18n.setLanguage('zh',{persist:false}));
 const settingsIds=['settings-dialog','settings-save','settings-restore','context-settings'],beforeSave=await geometry(page,settingsIds);
 await page.evaluate(()=>{
  setSettingsMessage('正在保存设置… 2/3');const log=document.getElementById('settings-save-log');log.hidden=false;
  for(let i=0;i<6;i++){const li=document.createElement('li');li.textContent='Saving section '+i;li.dataset.state=i===5?'error':'done';log.append(li);}
 });
 await noPopup(page);assert.equal(await page.locator('#settings-inline-feedback').getAttribute('role'),'alert');
 assert.equal(await page.locator('#settings-inline-feedback .settings-save-log li').count(),6);
 assert.equal(await page.locator('#settings-inline-feedback [id]').count(),0);
 assert.deepEqual(await geometry(page,settingsIds),beforeSave);
 await page.evaluate(()=>{document.getElementById('settings-save-log').replaceChildren();document.getElementById('settings-save-log').hidden=true;finishSettingsSave();});
 await noPopup(page);assert(await page.locator('#settings-dialog').evaluate(node=>node.open));
 assert((await page.locator('#settings-inline-feedback').textContent()).includes('设置已保存'));cases++;
 for(const id of ['entry-port-message','context-message','packet-size-message','runtime-settings-message','image-isolation-message','image-save-dir-message']){
  await page.evaluate(id=>{setSettingsMessage('');feedbackText(id,'Synthetic field error: '+id,true);},id);await noPopup(page);
  assert((await page.locator('#settings-inline-feedback').textContent()).includes(id));
  assert.deepEqual(await geometry(page,settingsIds),beforeSave);await page.evaluate(id=>feedbackText(id,''),id);cases++;
 }
 await page.evaluate(()=>showConfigurationFeedback(['自动压缩阈值'],{summary:'已接入。'}));await noPopup(page);
 assert((await page.locator('#settings-inline-feedback').textContent()).includes('自动压缩阈值'));
 assert.equal(await page.locator('#settings-restart-dialog').count(),0);assert.deepEqual(await geometry(page,settingsIds),beforeSave);
 await page.evaluate(()=>closeSettings());await noPopup(page);
 assert((await page.locator('#connection-inline-feedback').textContent()).includes('重启 ChatGPT App'));cases++;
 for(const id of ['compression-confirm-dialog','connect-confirm-dialog','exit-confirm-dialog']){
  await page.evaluate(id=>document.getElementById(id).showModal(),id);const before=await geometry(page,[id]),messageId=id.replace('-dialog','-message');
  await page.evaluate(id=>feedbackText(id,'Synthetic confirmation error '.repeat(25),true),messageId);await noPopup(page);
  assert(await page.locator('#'+messageId).isVisible());assert.deepEqual(await geometry(page,[id]),before);
  await page.evaluate(id=>document.getElementById(id).close(),id);cases++;
 }
 const beforeLoading=await geometry(page,['packet-list','requests-inline-feedback']);
 await page.evaluate(()=>feedbackText('list-loading','正在加载更多记录…'));await noPopup(page);
 assert((await page.locator('#requests-inline-feedback').textContent()).includes('加载'));
 assert.deepEqual(await geometry(page,['packet-list','requests-inline-feedback']),beforeLoading);
 await page.evaluate(()=>feedbackText('list-loading',''));await noPopup(page);assert.equal(await page.locator('#requests-inline-feedback').textContent(),'');cases++;
 const beforeFailure=await geometry(page,['records-view','app-inline-feedback']);
 await page.evaluate(()=>message('Synthetic content failure',true));await noPopup(page);
 assert.equal(await page.locator('#app-inline-feedback').textContent(),'Synthetic content failure');
 assert.deepEqual(await geometry(page,['records-view','app-inline-feedback']),beforeFailure);cases++;
 await page.evaluate(()=>{
  document.getElementById('settings-restart-feedback').hidden=true;document.getElementById('settings-dialog').showModal();document.getElementById('settings-message').textContent='';
  document.addEventListener('invalid',event=>{window.invalidPrevented=event.defaultPrevented;},true);
  const input=document.getElementById('entry-port');input.value='1';input.reportValidity();
 });
 await noPopup(page);assert(await page.evaluate(()=>window.invalidPrevented));
 assert.equal(await page.locator('#entry-port').getAttribute('aria-invalid'),'true');
 assert((await page.locator('#settings-inline-feedback').textContent()).includes('1024'));
 await page.evaluate(()=>uiI18n.setLanguage('en',{persist:false}));await settle(page);
 assert((await page.locator('#settings-inline-feedback').textContent()).includes('Must be at least'));
 await page.locator('#entry-port').fill('47844');assert.equal(await page.locator('#entry-port').getAttribute('aria-invalid'),'false');cases++;
 await page.evaluate(()=>document.getElementById('settings-dialog').close());
 const sharing=await load();
 await sharing.evaluate(()=>{
  document.getElementById('records-view').hidden=true;document.getElementById('sharing-view').hidden=false;
  const credentials=['quota','exit'].map(kind=>({id:kind+'1',kind,direction:'out',label:kind,enabled:true,expires_at:null,weekly_reset:false,budget_nanos:10e9,totals:{},daily:{}}));
  const state={supported:true,protocol:4,credentials_supported:true,weekly_reset_days_supported:true,used_quota_edit_supported:true,host:{active:true,running:true},client:{active:false},egress:{supported:true,host:{active:false},selection:{mode:'local'}},credentials};
  window.fetch=async(_,options)=>{
   const action=options.body?JSON.parse(options.body).action:null;
   if(action==='credential_copy')await new Promise(resolve=>{window.finishCopy=resolve;});
   if(window.failNext){window.failNext=false;return {ok:false,json:async()=>({error:'Synthetic sharing failure'})};}
   return {ok:true,json:async()=>({...state,...(action==='credential_copy'?{link:'synthetic-secret'}:{})})};
  };
  window.setInterval=fn=>{window.sharingPoll=fn;return 1;};window.clearInterval=()=>{};
  Object.defineProperty(navigator,'clipboard',{value:{writeText:async value=>{window.copiedValue=value;}},configurable:true});
 });
 await sharing.addScriptTag({content:read('sharing.js')});
 for(const language of ['zh','en']){
  await sharing.evaluate(language=>{uiI18n.setLanguage(language,{persist:false});InspectorSharing.setActive(true);},language);
  const selector='[data-action=copy][data-id=quota1]';await sharing.waitForSelector(selector);
  const before=await geometry(sharing,['quota-credential-rows','sharing-copy-quota1']),caption=await sharing.locator(selector).textContent();
  await sharing.locator(selector).click();await sharing.waitForFunction(()=>typeof window.finishCopy==='function');
  assert.deepEqual(await geometry(sharing,['quota-credential-rows','sharing-copy-quota1']),before);
  await sharing.evaluate(()=>{window.finishCopy();window.finishCopy=null;});await noPopup(sharing);
  await sharing.waitForFunction(()=>document.getElementById('sharing-copy-quota1').dataset.copied==='true');
  assert.equal(await sharing.locator(selector).textContent(),language==='zh'?'已复制':'Copied');
  assert.equal(await sharing.evaluate(()=>window.copiedValue),'synthetic-secret');
  assert(!await sharing.locator('body').textContent().then(value=>value.includes('synthetic-secret')));
  await sharing.evaluate(()=>window.sharingPoll());await noPopup(sharing);
  assert.equal(await sharing.locator(selector).textContent(),language==='zh'?'已复制':'Copied');
  assert.deepEqual(await geometry(sharing,['quota-credential-rows','sharing-copy-quota1']),before);
  await sharing.waitForFunction(()=>document.getElementById('sharing-copy-quota1').dataset.copied!=='true');
  assert.equal(await sharing.locator(selector).textContent(),caption);cases++;
 }
 await sharing.evaluate(()=>{uiI18n.setLanguage('zh',{persist:false});feedbackText('sharing-message','已更新，新请求使用当前设置。');});await noPopup(sharing);
 assert((await sharing.locator('#quota-inline-feedback').textContent()).includes('已更新'));cases++;
 await sharing.locator('[data-action=field][data-id=quota1][data-field=label]').click();const beforeEdit=await geometry(sharing,['sharing-inline-editor']);
 await sharing.locator('#sharing-inline-value').fill('New name');await sharing.evaluate(()=>window.failNext=true);await sharing.locator('#sharing-inline-save').click();
 await sharing.waitForFunction(()=>!document.getElementById('sharing-inline-message').hidden);await noPopup(sharing);
 assert.deepEqual(await geometry(sharing,['sharing-inline-editor']),beforeEdit);
 await sharing.locator('#sharing-inline-cancel').click();cases++;
 await sharing.locator('[data-action=delete][data-id=quota1]').click();const beforeDelete=await geometry(sharing,['sharing-delete-dialog']);
 await sharing.evaluate(()=>window.failNext=true);await sharing.locator('#sharing-delete-confirm').click();
 await sharing.waitForFunction(()=>!document.getElementById('sharing-delete-message').hidden);await noPopup(sharing);
 assert.deepEqual(await geometry(sharing,['sharing-delete-dialog']),beforeDelete);await sharing.locator('#sharing-delete-cancel').click();cases++;
 await sharing.locator('#quota-budget').fill('0');await sharing.evaluate(()=>document.getElementById('quota-budget').reportValidity());await noPopup(sharing);
 assert.equal(await sharing.locator('#quota-budget').getAttribute('aria-invalid'),'true');assert((await sharing.locator('#quota-inline-feedback').textContent()).includes('1'));cases++;
 const tray=await load(true),beforeTray=await tray.locator('.tray-panel').boundingBox();
 await tray.evaluate(()=>feedbackText('tray-error','无法完成操作，请稍后重试。',true));await noPopup(tray);
 assert(await tray.locator('#tray-error').isVisible());assert(!await tray.locator('#tray-status').isVisible());assert.deepEqual(await tray.locator('.tray-panel').boundingBox(),beforeTray);
 await tray.evaluate(()=>feedbackText('tray-error',''));assert(await tray.locator('#tray-status').isVisible());cases++;
 console.log(JSON.stringify({floating_feedback:'passed',browser_cases:cases,updates:'inline beside version',reviewed_feedback:'inline',copy_acknowledgement:'fixed-width button',validation:'no native bubble',layout:'stable'}));
}
run().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{await browser?.close();});
