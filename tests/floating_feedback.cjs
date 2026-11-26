/* Isolated browser checks; no live service, application or user profile. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
let chromium;
try{({chromium}=require('playwright'));}catch(_){console.log(JSON.stringify({skipped:'Playwright is not installed'}));process.exit(0);}
const edge=path.join(process.env['ProgramFiles(x86)']||'C:/Program Files (x86)','Microsoft/Edge/Application/msedge.exe');
const executablePath=process.env.CRO_TEST_BROWSER||(process.platform==='win32'&&fs.existsSync(edge)?edge:chromium.executablePath());
if(!fs.existsSync(executablePath)){console.log(JSON.stringify({skipped:'No browser engine is installed'}));process.exit(0);}
const root=path.join(__dirname,'../packet_inspector/ui'),read=name=>fs.readFileSync(path.join(root,name),'utf8'),app=read('app.js');
const extract=(start,end)=>app.slice(app.indexOf(start),app.indexOf(end,app.indexOf(start)));
const updateLabels={
 zh:{check:'检查更新',install:'立即升级'},
 en:{check:'Check for updates',install:'Install update'},
};
let browser,cases=0;
async function load(tray=false){
 const page=await browser.newPage({viewport:{width:1280,height:900}});
 await page.setContent(read(tray?'tray.html':'index.html').replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,'').replace(/<link\b[^>]*>/g,''));
 for(const file of tray?['tray.css']:['style.css','sharing.css'])await page.addStyleTag({content:read(file)});
 await page.addScriptTag({content:read('i18n.js')});
 await page.evaluate(()=>{
  window.t=value=>uiI18n.t(value);window.feedback=uiI18n.floatingFeedback();
  const storage=new Map();Object.defineProperty(window,'localStorage',{value:{getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value)}});
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
  "window.updateStatus=null;window.updateBusy=false;window.settingsSaving=false;window.updateAction='';",
  "window.updateRevision=0;window.updateStatusLoading=false;window.settingsOpening=false;window.compressionSaving=false;window.exitSaving=false;window.connectSaving=false;window.connectChecking=false;",
  "window.firstRunChecking=false;window.refreshStatus=async()=>{};window.loadContextSettings=async()=>{};window.readSettingsDraft=()=>({});",
  "window.settingsSaveProgress={total:0,steps:[]};window.renderLanguageSelection=()=>{};window.renderConnectionActions=()=>{};window.updateSettingsSaveState=()=>{};",
  "window.closeZstdLevelPicker=()=>{};window.discardSettingsDraft=()=>{};",
  "window.fail=error=>{throw error;};",
  "function el(tag,cls,value){const node=document.createElement(tag);node.className=cls;node.textContent=value;return node;}",
  app.match(/^function setText\(.*$/m)[0],
  extract('function renderUpdates()','async function selectPacket('),
  app.match(/^function action\(.*$/m)[0],
  app.match(/^action\("updates-(?:check|install|notes-open)",.*$/gm).join('\n'),
  extract('globalThis.InspectorRestartNotice = {','\nconst $ ='),
  "window.restartNotices=InspectorRestartNotice.create({status:()=>state.status,request:async()=>{},openSettings:async()=>{}});",
  extract('function setSettingsMessage(','async function runSettingsSaveStep('),
  extract('function finishSettingsSave()','async function saveSettings()'),
  extract('function closeSettings()','action("settings-close"'),
  extract('function message(','function fail(')
 ].join('\n')});
 await page.evaluate(()=>document.getElementById('settings-dialog').showModal());
 const updates=page.locator('#general-settings > #updates-settings');
 const checkButton=updates.locator('#updates-check'),installButton=updates.locator('#updates-install');
 const notesLink=updates.locator('#updates-notes-open'),notesDialog=page.locator('#updates-notes-dialog');
 const repositoryLink=updates.locator('#updates-repository');
 const feedback=updates.locator('#updates-message'),controls=updates.locator('.updates-controls');
 const continuityHint=updates.locator('#updates-continuity-hint');
 const updateLayout=['updates-check','updates-repository','settings-dialog','settings-save','settings-restore'];
 assert.equal(await updates.locator('button').count(),3);
 for(const width of [320,500,1120,1600]){await page.setViewportSize({width,height:900});
 for(const language of ['zh','en']){
  await page.evaluate(language=>{uiI18n.setLanguage(language,{persist:false});updateStatus={state:'idle',current_version:'1.0.0'};renderUpdates();},language);
  assert.equal(await updates.locator('.settings-key').textContent(),language==='zh'?'软件版本':'Software version');
  assert.equal(await updates.locator('#updates-versions').textContent(),'1.0.0');
  assert.equal(await installButton.isVisible(),false);
  assert.equal(await notesLink.isVisible(),false);
  assert.equal(await continuityHint.isVisible(),false);
  for(const [button,label] of [[checkButton,updateLabels[language].check],[installButton,updateLabels[language].install]]){
   assert.equal((await button.textContent()).trim(),'','The controls use icons only');
   assert.equal(await button.getAttribute('title'),label);
   assert.equal(await button.getAttribute('aria-label'),label);
   assert.equal(await button.locator('svg').getAttribute('aria-hidden'),'true');
  }
  assert(await checkButton.locator('svg').evaluate(icon=>icon.getBBox().width>0),'The refresh icon renders');
  const [budgetFont,versionFont,hintFont,statusFont]=await page.evaluate(()=>['budget-label','updates-versions','startup-hint','updates-message'].map(id=>{
   const {fontFamily,fontSize,fontWeight,lineHeight}=getComputedStyle(document.getElementById(id));
   return {fontFamily,fontSize,fontWeight,lineHeight};
  }));
  assert.deepEqual(versionFont,budgetFont,'The version uses the size limit label typography');
  assert.deepEqual(statusFont,hintFont,'The update status uses the settings hint typography');
  const before=await geometry(page,updateLayout);
  const [button,repository]=before;
  const [current]=await geometry(page,['updates-versions']);
  const [languageToggle]=await geometry(page,['language-toggle']);
  const labelTop=await updates.locator('.settings-key').evaluate(node=>
   node.getBoundingClientRect().top+parseFloat(getComputedStyle(node).paddingBlockStart));
  assert.equal(current[0],languageToggle[0],'The version starts at the shared controls column');
  assert(Math.abs(current[1]-labelTop)<=1,'The version lines up with the first line of its label');
  assert(Math.abs(repository[0]-current[0]-current[2]-6)<=1,'The GitHub link sits next to the version');
  assert(Math.abs(button[0]-repository[0]-repository[2]-6)<=1,'The check icon follows the GitHub link');
  assert.equal(repository[1],button[1],'Both icons share the same row');
  assert.equal(await repositoryLink.getAttribute('aria-label'),language==='zh'?'GitHub 仓库':'GitHub repository');
  const logo=await repositoryLink.locator('svg').boundingBox();
  assert.deepEqual([logo.width,logo.height],[20,20]);
  for(const status of ['checking','current','available','downloading','upgrading','complete','error']){
   await page.evaluate(status=>{updateStatus={state:status,current_version:'1.0.0',latest_version:'1.1.0',error:'Synthetic update error '.repeat(30)};renderUpdates();},status);
   const checking=status==='checking',installing=['downloading','upgrading'].includes(status);
   const busy=checking||installing;
   const hasInstall=status==='available'||installing;
   assert.equal(await checkButton.isDisabled(),busy);
   assert.equal(await checkButton.getAttribute('aria-busy'),String(checking));
   assert.equal(await checkButton.locator('.update-spinner').isVisible(),checking);
   assert.equal(await installButton.isVisible(),hasInstall);
   assert.equal(await notesLink.isVisible(),status==='available');
   assert.equal(await continuityHint.isVisible(),status==='available');
   assert.equal(await installButton.getAttribute('aria-busy'),String(installing));
   if(hasInstall)assert.equal(await installButton.isDisabled(),installing);
   assert.equal(await installButton.locator('.update-spinner').isVisible(),installing);
   await noPopup(page);assert(await feedback.isVisible());
   assert.equal(await feedback.getAttribute('title'),await feedback.textContent());
   const [version,result]=await geometry(page,['updates-versions','updates-message']);
   assert.deepEqual(version,current,'The version stays left-aligned when feedback appears');
   const textGap=result[0]-(button[0]+button[2]);
   assert(Math.abs(textGap-6)<=1,'Update feedback follows the check icon');
   assert(Math.abs(result[1]+result[3]/2-version[1]-version[3]/2)<=1);
   if(hasInstall){
    const [install]=await geometry(page,['updates-install']);
    assert(Math.abs(install[0]-result[0]-result[2]-6)<=1,'The install icon sits directly after the result');
    assert.equal(install[1],button[1],'Both icons stay on the same line');
   }
   if(status==='available'){
    assert(await installButton.locator('svg').evaluate(icon=>icon.getBBox().width>0),'The download icon renders');
    const [notes,install]=await geometry(page,['updates-notes-open','updates-install']);
    assert(Math.abs(notes[0]-install[0]-install[2]-6)<=1,'The notes text follows the install icon');
    assert(await notesLink.evaluate(node=>{
     const {borderTopWidth,backgroundColor}=getComputedStyle(node);
     return borderTopWidth==='0px'&&backgroundColor==='rgba(0, 0, 0, 0)';
    }),'Release notes look like text');
    assert.equal(await continuityHint.textContent(),language==='zh'?'升级时中转服务无缝切换':'Seamless relay handover');
    const hintAtEnd=await continuityHint.evaluate(node=>
     Math.abs(node.parentElement.getBoundingClientRect().right-node.getBoundingClientRect().right)<=1);
    assert(hintAtEnd,'The continuity hint is aligned to the right of the controls column');
    if(width>=1120){
     const [hint,install]=await geometry(page,['updates-continuity-hint','updates-install']);
     assert(hint[0]>=install[0]+install[2],'The hint follows the install button');
     assert(Math.abs(hint[1]+hint[3]/2-install[1]-install[3]/2)<=1,'The desktop hint shares the version row');
    }
    assert(await controls.evaluate(node=>{
     const {left,right}=node.getBoundingClientRect(),notes=node.querySelector('#updates-notes-open');
     const priorScroll=node.scrollLeft;
     node.scrollLeft+=notes.getBoundingClientRect().right-right;
     const bounds=notes.getBoundingClientRect();
     node.scrollLeft=priorScroll;
     return bounds.left>=left-1&&bounds.right<=right+1;
    }),'The notes text can scroll into view on a narrow row');
   }
   if(status==='current'){
    assert.equal(await feedback.textContent(),language==='zh'?'已是最新版本。':'You are up to date.');
    const {scrollWidth,clientWidth}=await feedback.evaluate(node=>({scrollWidth:node.scrollWidth,clientWidth:node.clientWidth}));
    assert.equal(scrollWidth,clientWidth,`The ${language} up-to-date message fits at ${width}px`);
   }
   if(width>=1120)assert(await controls.evaluate(node=>node.scrollWidth===node.clientWidth),'The desktop update row fits without scrolling');
   assert.deepEqual(await geometry(page,updateLayout),before);
  }
  if(width===320){
   await page.evaluate(()=>{updateStatus={state:'idle',current_version:'1.0.0-'+'prerelease'.repeat(10)};renderUpdates();});
   assert(await controls.evaluate(node=>node.scrollWidth===node.clientWidth),'A long version leaves room for both icons');
  }
  cases++;
 }}
 await page.evaluate(()=>closeSettings());
 for(const language of ['zh','en']){
  await page.evaluate(async language=>{
   uiI18n.setLanguage(language,{persist:false});updateStatus=null;
   window.updateRequests=[];window.cachedUpdate={state:'idle',current_version:'1.0.0'};
   window.api=async(path,data)=>{
    if(path!=='/api/updates')throw Error('Unexpected request: '+path);
    updateRequests.push(data?.action||'status');
    if(data?.action==='check')cachedUpdate={state:'available',current_version:'1.0.0',latest_version:'1.1.0',release_notes:[
     {version:'1.1.0',zh:'这个版本的说明\n\n'.repeat(30)+'- **优化** <img src=x>',en:'Notes for this version\n\n'.repeat(30)+'- **Improved** <img src=x>'},
     {version:'1.0.5',en:'Earlier version notes'},
    ]};
    return {...cachedUpdate};
   };
   await openSettings();
  },language);
  assert.equal(await feedback.isVisible(),false,'unchecked settings show only the current version and check icon');
  assert.equal(await checkButton.isDisabled(),false);
  assert.equal(await installButton.isVisible(),false);
  await page.evaluate(()=>performUpdate('install'));
  assert.deepEqual(await page.evaluate(()=>updateRequests.filter(action=>action!=='status')),[]);
  await checkButton.press('Space');
  await page.waitForFunction(()=>updateStatus?.state==='available'&&!updateBusy);
  assert(await installButton.isVisible());
  assert.equal(await checkButton.isDisabled(),false);
  assert.deepEqual(await page.evaluate(()=>updateRequests.filter(action=>action!=='status')),['check']);cases++;
  const notesLabel=language==='zh'?'更新内容':'Release notes';
  assert.equal(await notesLink.textContent(),notesLabel);
  assert.equal(await notesDialog.isVisible(),false);
  await notesLink.press('Enter');
  await notesDialog.waitFor({state:'visible'});
  assert.equal(await page.getByRole('dialog',{name:notesLabel,exact:true}).count(),1);
  assert.deepEqual(await notesDialog.locator('section h3').allTextContents(),['v1.1.0','v1.0.5']);
  assert.equal(await notesDialog.locator('strong').textContent(),language==='zh'?'优化':'Improved');
  assert.equal(await notesDialog.locator('img').count(),0,'External Markdown is rendered as text');
  assert((await notesDialog.textContent()).includes(language==='zh'?'此版本未提供当前语言的更新内容':'Earlier version notes'));
  const closeNotes=notesDialog.getByRole('button',{name:language==='zh'?'关闭':'Close',exact:true});
  const navigation=notesDialog.getByRole('navigation',{name:language==='zh'?'版本导航':'Version navigation'});
  const firstVersion=navigation.getByRole('link',{name:'v1.1.0'}),earlierVersion=navigation.getByRole('link',{name:'v1.0.5'});
  const scroller=notesDialog.getByRole('region',{name:language==='zh'?'版本说明':'Version notes'});
  assert.deepEqual(await navigation.getByRole('link').allTextContents(),['v1.1.0','v1.0.5']);
  assert.equal(await firstVersion.getAttribute('aria-current'),'location');
  const navBox=await navigation.boundingBox(),contentBox=await scroller.boundingBox();
  assert(navBox.x+navBox.width<contentBox.x,'The version links are left of the release notes');
  assert(await closeNotes.evaluate(node=>node===document.activeElement));
  const smooth=language==='zh';
  await page.emulateMedia({reducedMotion:smooth?'no-preference':'reduce'});
  const scrollEnd=await scroller.evaluate(node=>{
   window.notesScrollOffsets=[];
   node.onscroll=()=>window.notesScrollOffsets.push(node.scrollTop);
   return node.scrollHeight-node.clientHeight;
  });
  assert(scrollEnd>0);
  const hash=await page.evaluate(()=>location.hash);
  await earlierVersion.press('Enter');
  assert.equal(await earlierVersion.getAttribute('aria-current'),'location');
  assert.equal(await page.evaluate(()=>location.hash),hash);
  const target=notesDialog.getByRole('region',{name:'v1.0.5'});
  await page.waitForFunction(()=>Math.abs($('updates-notes-content').scrollTop-
   ($('updates-notes-content').scrollHeight-$('updates-notes-content').clientHeight))<=1);
  await page.waitForFunction(id=>document.activeElement.id===id,await target.getAttribute('id'));
  assert.equal(await page.evaluate(end=>window.notesScrollOffsets.some(offset=>offset>1&&offset<end-1),scrollEnd),smooth);
  assert((await target.boundingBox()).y<contentBox.y+contentBox.height,'The earlier version is visible after selecting it');
  await firstVersion.click();
  assert.equal(await firstVersion.getAttribute('aria-current'),'location');
  await page.waitForFunction(()=>$('updates-notes-content').scrollTop===0);
  await page.emulateMedia({reducedMotion:'no-preference'});
  await page.keyboard.press('Escape');
  assert.equal(await notesDialog.isVisible(),false);
  assert(await notesLink.evaluate(node=>node===document.activeElement));
  await notesLink.click();
  await closeNotes.click();
  assert.equal(await notesDialog.isVisible(),false);
  assert.equal(await page.locator('#settings-dialog').isVisible(),true);cases++;
  if(language==='zh'){
   await page.setViewportSize({width:360,height:520});
   await notesLink.press('Space');
   await notesDialog.waitFor({state:'visible'});
   const mobileNav=await navigation.boundingBox(),mobileContent=await scroller.boundingBox();
   assert(mobileNav.y+mobileNav.height<mobileContent.y,'The version links are above the notes on narrow screens');
   assert(await scroller.evaluate(node=>node.scrollHeight>node.clientHeight));
   await scroller.press('End');
   await page.waitForFunction(()=>document.getElementById('updates-notes-content').scrollTop>0);
   await page.waitForFunction(()=>document.querySelector('#updates-notes-navigation [aria-current]').textContent==='v1.0.5');
   const box=await notesDialog.boundingBox();
   assert(box.x>=0&&box.x+box.width<=360&&box.y>=0&&box.y+box.height<=520);
   await page.keyboard.press('Escape');
   await page.setViewportSize({width:1600,height:900});cases++;
  }
  for(let reopen=0;reopen<2;reopen++){
   await page.evaluate(async()=>{closeSettings();await openSettings();});
   assert(await feedback.isVisible(),'an available update remains visible after reopening settings');
   assert(await continuityHint.isVisible(),'the cached update still shows the relay notice');
   assert((await feedback.textContent()).includes('1.1.0'));
   assert(await installButton.isVisible());
   assert.equal(await installButton.isDisabled(),false);
   assert.equal(await page.evaluate(()=>updateRequests.filter(action=>action==='check').length),1,'reopening reads cached status without another release check');cases++;
  }
  await page.evaluate(async()=>{
   closeSettings();updateStatus=null;updateRequests.length=0;
   cachedUpdate={state:'available',current_version:'0.9.0',latest_version:'0.10.0'};
   await openSettings();
  });
  assert(await feedback.isVisible(),'a background release result is visible on opening settings');
  assert((await feedback.textContent()).includes('0.10.0'));
  assert.deepEqual(await page.evaluate(()=>updateRequests),['status']);
  assert(await installButton.isVisible());cases++;
  await notesLink.click();
  assert((await notesDialog.textContent()).includes(language==='zh'?'暂无更新内容，可重新检查更新':'Check for updates again to load release notes'));
  assert.equal(await notesDialog.getByRole('navigation').count(),0);
  await page.keyboard.press('Escape');
  assert.deepEqual(await page.evaluate(()=>updateRequests),['status'],'The dialog reads the cached release notes');
  await page.evaluate(async()=>{
   const request=window.api;
   window.api=async()=>{throw Error('offline');};
   try {await loadUpdates();await performUpdate('check');}
   finally {window.api=request;}
  });
  assert((await feedback.textContent()).includes('0.10.0'),'failed checks leave the previously displayed version');
  assert(await checkButton.isEnabled());
  await noPopup(page);cases++;
  await checkButton.click();
  await page.waitForFunction(()=>updateRequests.filter(action=>action==='check').length===1&&!updateBusy);
  assert(await installButton.isVisible());
  await installButton.click();
  await page.waitForFunction(()=>updateStatus?.state==='downloading'&&!updateBusy);
  assert.deepEqual(await page.evaluate(()=>updateRequests.filter(action=>action!=='status')),['check','install']);
  assert.equal(await checkButton.isDisabled(),true);
  assert.equal(await installButton.getAttribute('aria-busy'),'true');
  assert.equal(await installButton.isDisabled(),true);
  await page.evaluate(async()=>{cachedUpdate={state:'complete',current_version:'1.1.0'};await loadUpdates();});
  assert.equal(await checkButton.isDisabled(),false);
  assert.equal(await installButton.isVisible(),false);cases++;
  await page.evaluate(async()=>{closeSettings();cachedUpdate={state:'current',current_version:'1.1.0',latest_version:'1.1.0'};updateStatus=null;await openSettings();});
  assert(await feedback.isVisible(),'an up-to-date result is visible when settings reopen');
  assert.equal(await feedback.textContent(),language==='zh'?'已是最新版本。':'You are up to date.');
  assert.equal(await checkButton.isDisabled(),false);
  assert.equal(await installButton.isVisible(),false);cases++;
  await page.evaluate(()=>closeSettings());
 }
 await page.evaluate(()=>document.getElementById('settings-dialog').showModal());
 await page.setViewportSize({width:1280,height:900});
 await page.evaluate(()=>uiI18n.setLanguage('zh',{persist:false}));
 const settingsIds=['settings-dialog','settings-save','settings-restore','context-settings'],beforeSave=await geometry(page,settingsIds);
 await page.evaluate(()=>{
  settingsSaving=true;
  settingsSaveProgress={total:6,steps:Array.from({length:6},(_,i)=>({label:'Saving section '+i,state:i===5?'error':i===4?'running':'done'}))};
  renderSettingsSaveProgress();setSettingsMessage('正在保存设置… 2/3');
 });
 await noPopup(page);assert.equal(await page.locator('#settings-inline-feedback').getAttribute('role'),'alert');
 assert.equal(await page.locator('#settings-inline-feedback .settings-save-log li').count(),6);
 assert.equal(/处理中|已保存|失败/.test(await page.locator('#settings-inline-feedback .settings-save-log').textContent()),false,'progress rows have no separate status text');
 assert.equal(await page.locator('.settings-step-state').count(),0);
 assert.equal(await page.locator('#settings-inline-feedback [id]').count(),0);
 assert.deepEqual(await geometry(page,settingsIds),beforeSave);
 await page.evaluate(()=>{settingsSaving=false;finishSettingsSave();});
 await noPopup(page);assert(await page.locator('#settings-dialog').evaluate(node=>node.open));
 assert.equal((await page.locator('#settings-inline-feedback').textContent()).trim(),'设置已保存。');
 assert.equal(await page.locator('#settings-inline-feedback .settings-save-log').count(),0,'successful saves leave only the confirmation');
 assert.equal(await page.locator('#settings-save-log li').count(),0);
 assert.equal(await page.locator('#settings-save-log').evaluate(node=>node.hidden),true);
 assert.deepEqual(await geometry(page,settingsIds),beforeSave);cases++;
 for(const id of ['entry-port-message','context-message','packet-size-message','runtime-settings-message','image-isolation-message','storage-dir-message']){
  await page.evaluate(id=>{setSettingsMessage('');feedbackText(id,'Synthetic field error: '+id,true);},id);await noPopup(page);
  assert((await page.locator('#settings-inline-feedback').textContent()).includes(id));
  assert.deepEqual(await geometry(page,settingsIds),beforeSave);await page.evaluate(id=>feedbackText(id,''),id);cases++;
 }
 await page.evaluate(()=>restartNotices.change('compact_tokens',null,250000));await noPopup(page);
 assert((await page.locator('#restart-notice').textContent()).includes('自动压缩阈值'));
 assert.equal(await page.locator('#settings-restart-dialog').count(),0);
 assert(await page.locator('#restart-notice').isVisible());
 assert.deepEqual(await geometry(page,settingsIds.slice(0,3)),beforeSave.slice(0,3),'showing pending items keeps the dialog and footer stable');
 await page.evaluate(()=>closeSettings());await noPopup(page);
 assert.equal(await page.locator('#restart-notice-open').getAttribute('data-visible'),'true');
 assert.equal(await page.locator('#connection-inline-feedback').count(),0);cases++;
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
  document.getElementById('settings-dialog').showModal();document.getElementById('settings-message').textContent='';
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
 const recovery=await load();
 await recovery.addScriptTag({content:"window.$=id=>document.getElementById(id);\n"+extract('function message(','uiI18n.floatingFeedback();')});
 await recovery.evaluate(async()=>{
  window.replies=[];window.serviceFailure={ok:false,status:502,body:{error:'forwarding_service_unavailable'}};
  window.fetch=async()=>{
   const reply=replies.shift();if(!reply)throw Error('Missing synthetic API response');
   if(reply.hold)await new Promise(resolve=>{window.releaseResponse=resolve;});
   return {ok:reply.ok!==false,status:reply.status||200,json:async()=>reply.body||{}};
  };
  window.callApi=async(path,data)=>{try{await api(path,data);}catch(error){fail(error);}};
  replies.push(serviceFailure);await callApi('/api/packets?limit=100');
 });await noPopup(recovery);
 assert.equal(await recovery.locator('#app-inline-feedback').textContent(),'forwarding_service_unavailable');
 await recovery.evaluate(async()=>{replies.push({});await callApi('/api/status');});
 assert.equal(await recovery.locator('#notice').evaluate(node=>node.hidden),false,'another endpoint cannot clear the failed operation');
 await recovery.evaluate(async()=>{replies.push({});await callApi('/api/packets?limit=50');});await noPopup(recovery);
 assert.equal(await recovery.locator('#notice').evaluate(node=>node.hidden),true);
 assert.equal(await recovery.locator('#app-inline-feedback').textContent(),'');cases++;
 await recovery.evaluate(async()=>{
  replies.push(serviceFailure);await callApi('/api/settings',{});
  replies.push({});await callApi('/api/settings');
 });
 assert.equal(await recovery.locator('#notice').evaluate(node=>node.hidden),false,'a read cannot confirm a failed write recovered');
 await recovery.evaluate(async()=>{replies.push({});await callApi('/api/settings',{});});await noPopup(recovery);
 assert.equal(await recovery.locator('#notice').evaluate(node=>node.hidden),true);cases++;
 await recovery.evaluate(async()=>{
  replies.push({ok:false,status:400,body:{error:'Synthetic save failure'}});await callApi('/api/settings',{});
  replies.push({});await callApi('/api/settings',{});
 });await noPopup(recovery);
 assert.equal(await recovery.locator('#notice').evaluate(node=>node.hidden),false);
 assert.equal(await recovery.locator('#app-inline-feedback').textContent(),'Synthetic save failure');cases++;
 await recovery.evaluate(async()=>{
  replies.push(serviceFailure);await callApi('/api/status');message('Synthetic operation failure',true);
  replies.push({});await callApi('/api/status');
 });await noPopup(recovery);
 assert.equal(await recovery.locator('#app-inline-feedback').textContent(),'Synthetic operation failure');cases++;
 await recovery.evaluate(()=>{replies.push({hold:true});window.earlierRequest=api('/api/status');});
 await recovery.evaluate(async()=>{replies.push(serviceFailure);await callApi('/api/status');releaseResponse();await earlierRequest;});
 assert.equal(await recovery.locator('#notice').evaluate(node=>node.hidden),false,'an older success cannot clear a newer failure');
 await recovery.evaluate(async()=>{replies.push({});await callApi('/api/status');});await noPopup(recovery);
 assert.equal(await recovery.locator('#notice').evaluate(node=>node.hidden),true);cases++;
 await recovery.evaluate(async()=>{
  document.getElementById('settings-dialog').showModal();replies.push(serviceFailure);await callApi('/api/status');
 });await noPopup(recovery);
 assert.equal(await recovery.locator('#settings-inline-feedback').textContent(),'forwarding_service_unavailable');
 await recovery.evaluate(async()=>{replies.push({});await callApi('/api/status');});await noPopup(recovery);
 assert.equal(await recovery.locator('#settings-inline-feedback').textContent(),'');cases++;
 const sharing=await load();
 await sharing.emulateMedia({reducedMotion:'reduce'});
 await sharing.evaluate(()=>{
  document.getElementById('records-view').inert=true;document.getElementById('sharing-view').inert=false;
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
