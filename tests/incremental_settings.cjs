/* The incremental checkbox remains a draft until the normal Save operation. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require('playwright');
const edge='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const executablePath=process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||(fs.existsSync(edge)?edge:chromium.executablePath());
if(!fs.existsSync(executablePath)){throw Error('A browser engine is required for incremental settings verification');}
const root=path.join(__dirname,'../packet_inspector/ui'),read=name=>fs.readFileSync(path.join(root,name),'utf8'),source=read('app.js');
const between=(start,end)=>source.slice(source.indexOf(start),source.indexOf(end,source.indexOf(start)));
const html=read('index.html').replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,'').replace(/<link\b[^>]*>/g,'');
let browser;
async function run(){
  browser=await chromium.launch({executablePath,headless:true});
  const page=await browser.newPage({viewport:{width:1120,height:900}});
  await page.route('http://incremental-settings.test/**',route=>route.fulfill({contentType:'text/html',body:html}));
  await page.goto('http://incremental-settings.test/');
  await page.addStyleTag({content:read('style.css')});
  await page.addScriptTag({content:read('i18n.js')});
  await page.evaluate(()=>{
    window.$=id=>document.getElementById(id);window.settingsSaving=false;window.settingsEditing=false;
    window.settingsLanguage='zh';window.selectedImageAnalysisMode=()=> 'native';
    window.imageDefaultDirectory=()=>'';
    window.settingsBaseline=null;window.linkSaving=false;window.settingsRevision=0;
    window.contextSettings=null;
    window.state={status:{incremental_transport_supported:true,incremental_transport_enabled:false,
      max_storage_bytes:512000000,max_storage_mb:489,storage:{max_bytes:512000000},ui_language:'zh'}};
    window.calls=[];window.restartCalls=[];
    window.api=async(path,data)=>{calls.push({path,data});state.status={...state.status,...data};return state.status;};
    window.runSettingsSaveStep=async(_label,apply)=>apply();
    window.renderSettingsSaveProgress=()=>{};window.setSettingsMessage=()=>{};
    window.updateSettingsSaveState=()=>{};window.restartNotices={ingest(){},change(...args){restartCalls.push(args);}};
    window.renderContextPlaceholders=()=>{};window.renderUpdates=()=>{};window.renderStatus=()=>{};
    window.finishSettingsSave=()=>{};window.trackSettingsRestart=(...args)=>restartCalls.push(args);
    window.fillSettings=()=>fillIncrementalTransport();
    window.t=value=>uiI18n.t(value);
  });
  await page.addScriptTag({content:[
    between('function storageLimitSettings(', 'function fillSettings('),
    between('function readSettingsDraft(', 'function setSettingsMessage('),
    between('async function saveSettings(', 'function renderUpdates('),
    between('function fillIncrementalTransport(', 'for(const key of ["context","compact"])')
  ].join('\n')});
  await page.evaluate(()=>{
    fillIncrementalTransport();settingsEditing=true;settingsBaseline=readSettingsDraft();
    for(const input of document.querySelectorAll('#settings-dialog input'))if(input.id!=='incremental-transport')input.disabled=true;
    document.getElementById('settings-dialog').showModal();
  });
  const input=page.locator('#incremental-transport');
  for(const enabled of [true,false,true]){
    await input.setChecked(enabled);
    const before=await page.evaluate(()=>({calls:calls.length,live:state.status.incremental_transport_enabled,draft:readSettingsDraft().incremental_transport_enabled}));
    assert.equal(before.draft,enabled);
    assert.equal(before.live,!enabled,'clicking must not apply or restart anything');
    await page.evaluate(()=>saveSettings());
    const after=await page.evaluate(()=>({calls,live:state.status.incremental_transport_enabled,dirty:changedSettings(),restartCalls}));
    assert.equal(after.calls.length,before.calls+1);
    assert.deepEqual(after.calls.at(-1),{path:'/api/settings',data:{incremental_transport_enabled:enabled}});
    assert.equal(after.live,enabled);assert.deepEqual(after.dirty,[]);assert.deepEqual(after.restartCalls,[]);
  }
  await page.evaluate(()=>{settingsEditing=false;state.status.incremental_transport_supported=false;fillIncrementalTransport();});
  assert.equal(await input.isDisabled(),true);
  for(const language of ['zh','en']){
    const text=await page.evaluate(language=>{uiI18n.setLanguage(language,{persist:false});return document.getElementById('incremental-transport-hint').textContent;},language);
    assert.match(text,language==='zh'?/保存后对新请求生效/:/Save to apply to new requests/);
  }
  console.log(JSON.stringify({incremental_settings:'passed',save_switches:3,clicks_before_save:'no requests',restart_notices:0,languages:2}));
}
run().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();});
