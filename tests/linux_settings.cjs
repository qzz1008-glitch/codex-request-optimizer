/* Linux capability gating and compatibility with existing Windows workers. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
let chromium;
try{({chromium}=require('playwright'));}catch(_){console.log(JSON.stringify({skipped:'Playwright is not installed'}));process.exit(0);}
const edge=path.join(process.env['ProgramFiles(x86)']||'C:/Program Files (x86)','Microsoft/Edge/Application/msedge.exe');
const executablePath=process.env.CRO_TEST_BROWSER||(process.platform==='win32'&&fs.existsSync(edge)?edge:chromium.executablePath());
if(!fs.existsSync(executablePath)){console.log(JSON.stringify({skipped:'No browser engine is installed'}));process.exit(0);}
const root=path.join(__dirname,'../packet_inspector/ui'),read=name=>fs.readFileSync(path.join(root,name),'utf8'),source=read('app.js');
const html=read('index.html').replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,'').replace(/<link\b[^>]*>/g,'');
let browser,cases=0;
(async()=>{
  browser=await chromium.launch({executablePath,headless:true});
  const page=await browser.newPage({viewport:{width:1120,height:900}});
  await page.setContent(html);await page.addStyleTag({content:read('style.css')});
  await page.addScriptTag({content:read('i18n.js')});
  await page.evaluate(()=>{
    window.$=id=>document.getElementById(id);window.settingsSaving=false;window.settingsEditing=false;
    window.state={status:{image_analysis_mode:'native',image_isolation_supported:true,image_analysis_modes:['native','disabled']}};
    Object.assign(window,{linkSaving:false,settingsLanguage:null,settingsBaseline:null,settingsRevision:0,contextSettings:null,
      settingsSaveProgress:{total:0,steps:[]},imageSaves:[],imageRestarts:[]});
    window.storageLimitSettings=()=>({toBackend:value=>Number(value)*1000000});
    window.renderEntryPortStatus=()=>{};
    window.renderStatus=()=>{};
    window.fillSettings=()=>fillImageAnalysisMode();
    window.el=(tag,cls,text)=>Object.assign(document.createElement(tag),{className:cls,textContent:text});
    window.restartNotices={ingest(){},change(...args){imageRestarts.push(args);}};
    window.api=async(path,values)=>{imageSaves.push({path,values});return values;};
    window.updateStatus={supported:false,current_version:'0.8.7',state:'idle'};
    window.updateBusy=false;window.updateAction='';window.updateFeedbackVisible=false;
    window.fetch=()=>{throw new Error('Capability changes must not send requests');};
    $('storage-limit').value=500;$('packet-limit').value=10000;
    $('settings-dialog').showModal();
  });
  await page.addScriptTag({content:source.slice(source.indexOf('function imageAnalysisMode('),source.indexOf('function fillZstdLevel('))+
    '\n'+source.slice(source.indexOf('function renderLinuxStorageTargets('),source.indexOf('function fillDirectoryOpenButtons('))+
    '\n'+source.slice(source.indexOf('function readSettingsDraft('),source.indexOf('async function loadUpdates('))});
  await page.evaluate(()=>{
    $('image-analysis-mode').addEventListener('change',()=>{fillImageAnalysisMode();updateSettingsSaveState();});
  });
  const toggle=page.locator('#request-protection-settings #image-analysis-mode');
  const hint=page.locator('#image-isolation-hint');
  for(const lang of ['zh','en']){
    await page.evaluate(lang=>{uiI18n.setLanguage(lang);fillImageAnalysisMode();renderUpdates();},lang);
    const name=lang==='zh'?'解图优化':'Image analysis optimization';
    assert.equal(await page.getByRole('switch',{name,exact:true}).getAttribute('id'),'image-analysis-mode');
    const modeBox=await toggle.boundingBox();
    const protectionBox=await page.locator('#packet-size-protection').boundingBox();
    assert.equal(modeBox.x,protectionBox.x,'The image switch aligns with the other protection switches');
    assert((await hint.boundingBox()).x>=modeBox.x+modeBox.width,'The mode hint sits beside the switch');
    assert.equal(await toggle.isChecked(),false);
    assert.equal(await toggle.isDisabled(),true);
    assert.equal(await hint.textContent(),lang==='zh'?'当前中转仅支持原生图片解析。':'This relay supports native image analysis only.');
    assert.equal(await page.locator('#updates-check').isDisabled(),true);
    assert.equal(await page.locator('#updates-install').isVisible(),false);
    assert.match(await page.locator('#updates-message').textContent(),/Linux/);cases++;
  }
  await page.evaluate(()=>{delete state.status.image_analysis_modes;delete state.status.updates_supported;state.status.image_analysis_mode='optimized';updateStatus={state:'available'};fillImageAnalysisMode();renderUpdates();});
  assert.equal(await toggle.isDisabled(),false);
  assert.equal(await toggle.isChecked(),true);
  assert.equal(await hint.textContent(),'A separate session analyzes images, keeping the main request smaller and leaving more context for text. Some visual details may be missed.');
  assert.equal(await page.locator('#updates-check').isDisabled(),false);
  assert(await page.locator('#updates-install').isVisible());
  assert.equal(await page.locator('#updates-install').isDisabled(),false);
  assert.equal(await page.locator('#updates-install').getAttribute('aria-label'),'Install update');cases++;
  const save=page.locator('#settings-save');
  for(const [status,before,after,values] of [
    [{image_analysis_mode:'optimized'},'optimized','native',{image_analysis_mode:'native'}],
    [{image_analysis_mode:'disabled',image_analysis_modes:['native','disabled']},'disabled','native',{image_analysis_mode:'native'}],
    [{image_isolation_supported:true,image_isolation_enabled:false},'native','optimized',{image_isolation_enabled:true}],
  ]){
    await page.evaluate(status=>{
      state.status=status;settingsEditing=false;settingsBaseline=null;fillImageAnalysisMode();
      settingsBaseline=readSettingsDraft();settingsEditing=true;updateSettingsSaveState();
    },status);
    assert.equal(await toggle.isChecked(),before==='optimized');
    if(before==='disabled'){
      assert.equal(await hint.textContent(),'Save to restore native image analysis.');
      assert.equal(await page.locator('#settings-restore').isDisabled(),true);
    }else{
      assert.equal(await save.isDisabled(),true);
      await toggle.press('Space');
    }
    assert.equal(await toggle.isChecked(),after==='optimized');
    if(before==='optimized'){
      assert.equal(await hint.textContent(),'Images are sent with the main request for direct visual analysis. They increase request size and use context space.');
    }
    assert.deepEqual(await page.evaluate(()=>imageSaves),[],'Choosing a mode does not save it');
    assert.equal(await save.isDisabled(),false);
    await page.evaluate(()=>saveSettings());
    assert.deepEqual(await page.evaluate(()=>imageSaves.splice(0)),[{path:'/api/settings',values}]);
    assert.deepEqual(await page.evaluate(()=>imageRestarts.splice(0)),[['image_analysis_mode',before,after]]);
    assert.equal(await page.evaluate(()=>imageAnalysisMode()),after);
    assert.equal(await save.isDisabled(),true);cases++;
  }
  await page.evaluate(()=>{
    state.status={image_analysis_mode:'native',linux_data_directories:{'ssh-build':'/srv/cache','offline':'/srv/offline'},
      wsl_connection:{targets:[{id:'wsl:ubuntu',distribution:'Ubuntu'}]}};
    settingsEditing=false;settingsBaseline=null;
    renderLinuxStorageTargets({targets:[{kind:'ssh',id:'ssh-build',name:'Build server'}]});
    fillImageAnalysisMode();settingsBaseline=readSettingsDraft();settingsEditing=true;updateSettingsSaveState();
  });
  const sshFolder=page.getByRole('textbox',{name:'Build server',exact:true});
  const wslFolder=page.getByRole('textbox',{name:'WSL · Ubuntu',exact:true});
  assert.equal(await sshFolder.inputValue(),'/srv/cache');
  assert.equal(await wslFolder.getAttribute('placeholder'),'~/.cache/codex-request-optimizer');
  assert.equal(await wslFolder.getAttribute('aria-describedby'),'linux-storage-hint');
  await sshFolder.fill('');await wslFolder.fill('~/cache');
  await page.evaluate(()=>{updateSettingsSaveState();return saveSettings();});
  assert.deepEqual(await page.evaluate(()=>imageSaves.splice(0)),
    [{path:'/api/settings',values:{linux_data_directories:{offline:'/srv/offline','wsl:ubuntu':'~/cache'}}}]);cases++;
  await page.setViewportSize({width:320,height:900});
  assert.equal(await toggle.evaluate(node=>node.closest('.settings-controls').scrollWidth<=node.closest('.settings-controls').clientWidth+1),true);
  await page.evaluate(()=>{settingsSaving=true;fillImageAnalysisMode();renderUpdates();});
  assert.equal(await toggle.isDisabled(),true);cases++;
  console.log(JSON.stringify({linux_settings:'passed',browser_cases:cases}));
})().catch(error=>{console.error(error.stack);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();});
