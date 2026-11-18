/* Isolated settings checks: no live relay, account, or user configuration. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
let chromium;
try{({chromium}=require('playwright'));}catch(_){console.log(JSON.stringify({skipped:'Playwright is not installed'}));process.exit(0);}
const edge=path.join(process.env['ProgramFiles(x86)']||'C:/Program Files (x86)','Microsoft/Edge/Application/msedge.exe');
const executablePath=process.env.CRO_TEST_BROWSER||(process.platform==='win32'?edge:chromium.executablePath());
if(!fs.existsSync(executablePath)){console.log(JSON.stringify({skipped:'No browser engine is installed'}));process.exit(0);}
const root=path.join(__dirname,'../packet_inspector/ui'),read=name=>fs.readFileSync(path.join(root,name),'utf8'),source=read('app.js');
const extract=(start,end)=>{const first=source.indexOf(start),last=source.indexOf(end,first);assert(first>=0&&last>first);return source.slice(first,last);};
let browser,cases=0;
(async()=>{
  browser=await chromium.launch({executablePath,headless:true});
  const page=await browser.newPage({viewport:{width:1120,height:900}});
  await page.setContent(read('index.html').replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,'').replace(/<link\b[^>]*>/g,''));
  await page.addStyleTag({content:read('style.css')});
  await page.addScriptTag({content:read('i18n.js')});
  await page.evaluate(()=>{
    window.$=id=>document.getElementById(id);
    window.settingsEditing=false;window.settingsSaving=false;window.settingsRevision=0;window.linkSaving=false;
    window.settingsLanguage='zh';window.settingsBaseline=null;window.contextSettings=null;
    window.settingsSaveProgress={total:0,steps:[]};window.requests=[];window.failure=false;window.holdSave=false;
    window.holdTest=false;window.testState='ok';
    window.state={status:{proxy_settings_supported:true,proxy_settings:{mode:'auto',password_saved:false,
      manual:{address:'',username:'',bypass:'localhost,127.0.0.1,::1,*.saved.invalid'},
      detected:{source:'environment',http:{address:'http://alternate.invalid:8080'},
        https:{address:'http://regional.invalid:8080',username:'auto-user',password_saved:true},bypass:'localhost'},
      effective:{}}}};
    window.selectedImageAnalysisMode=()=>'native';window.storageLimitSettings=()=>({key:'max_storage_bytes',toBackend:Number});
    window.fillSettings=()=>fillProxySettings();window.renderContextPlaceholders=()=>{};window.renderUpdates=()=>{};
    window.renderStatus=()=>{};window.renderSettingsSaveProgress=()=>{};window.resetSettingsSaveProgress=()=>{};
    window.renderLanguageSelection=()=>{};window.renderConnectionActions=()=>{};window.updateSettingsSaveState=()=>{};
    window.setInterfaceLanguage=async language=>uiI18n.setLanguage(language,{persist:false});
    window.setSettingsMessage=text=>{$('settings-message').textContent=typeof text==='function'?text():text;};
    window.runSettingsSaveStep=async(label,apply)=>apply();window.restartNotices={ingest(){}};
    window.action=(id,fn)=>$(id).addEventListener('click',fn);
    window.fail=error=>{throw error;};
    window.api=async(url,data)=>{
      requests.push({url,data});
      if(url==='/api/proxy-test'){
        if(holdTest)await new Promise(resolve=>{window.releaseTest=resolve;});
        return {state:testState,status:testState==='rejected'?407:401,elapsed_ms:32,target:'https://upstream.invalid/responses',
          proxy_used:true,proxy:data.address||'http://regional.invalid:8080'};
      }
      if(data&&holdSave)await new Promise(resolve=>{window.releaseSave=resolve;});
      if(data&&failure)throw Error('Synthetic proxy failure');
      const before=state.status.proxy_settings;
      if(!data)return {...before,detected:{...before.detected,https:{address:'http://new-region.invalid:8080',username:'',password_saved:false}}};
      return {...before,mode:data.mode,manual:data.mode==='manual'?{...before.manual,address:data.address,username:data.username}:before.manual,
        password_saved:data.mode==='manual'?(data.password===null?before.password_saved:!!data.password):before.password_saved};
    };
    for(const input of $('settings-dialog').querySelectorAll('input'))input.disabled=true;
  });
  await page.addScriptTag({content:[
    extract('function selectedProxyMode()','function imageAnalysisMode('),
    extract('function readSettingsDraft()','function setSettingsMessage('),
    extract('function finishSettingsSave()','function renderUpdates('),
    extract('function discardSettingsDraft()','function closeSettings()'),
    extract('action("proxy-detect",','action("storage-dir-open",')
  ].join('\n')});
  await page.evaluate(()=>{fillProxySettings();settingsBaseline=readSettingsDraft();settingsEditing=true;$('settings-dialog').showModal();});
  const detectedAddress=page.locator('#proxy-detected-address');
  assert.equal(await page.locator('#proxy-detected output').count(),1);
  assert.match(await detectedAddress.textContent(),/regional.invalid:8080.*auto-user/);
  assert.doesNotMatch(await page.locator('#proxy-settings').textContent(),/检测来源|代理例外|保存后用于新请求/);
  assert.equal(await detectedAddress.evaluate(output=>output.labels[0].textContent),'代理地址');
  await page.evaluate(()=>{state.status.profile={upstream:'http://upstream.invalid/v1'};fillProxySettings();});
  assert.match(await detectedAddress.textContent(),/alternate.invalid:8080/);
  await page.evaluate(()=>{state.status.profile.upstream='https://upstream.invalid/v1';fillProxySettings();});
  assert.equal(await page.locator('#proxy-address').isDisabled(),true);
  await page.locator('#proxy-detect').click();
  assert.match(await detectedAddress.textContent(),/new-region.invalid/);
  assert.equal(await page.evaluate(()=>requests.filter(r=>r.data).length),0);cases++;

  await page.locator('#proxy-mode label').filter({has:page.locator('input[value="manual"]')}).click();
  assert.equal(await page.locator('#proxy-address').isEnabled(),true);
  await page.evaluate(()=>saveSettings());
  assert.equal(await page.evaluate(()=>requests.filter(r=>r.data).length),0,'Invalid manual settings must not save');
  await page.locator('#proxy-address').fill('http://manual.invalid:8080');
  await page.locator('#proxy-username').fill('manual-user');
  await page.locator('#proxy-password').fill('manual-secret');
  await page.evaluate(()=>fillProxySettings());
  assert.equal(await page.locator('#proxy-password').inputValue(),'manual-secret','Polling must preserve unsaved authentication');
  assert.equal(await page.evaluate(()=>requests.filter(r=>r.data).length),0,'Changing mode and typing must not auto-save');
  await page.evaluate(()=>{holdSave=true;window.saving=saveSettings();});
  assert.equal(await page.locator('#proxy-password').isDisabled(),true);
  await page.evaluate(async()=>{releaseSave();await saving;holdSave=false;});
  assert.equal(await page.locator('#proxy-password').inputValue(),'');
  assert.equal(await page.evaluate(()=>Object.hasOwn(requests.at(-1).data,'bypass')),false);
  assert.equal(await page.evaluate(()=>state.status.proxy_settings.manual.bypass),'localhost,127.0.0.1,::1,*.saved.invalid');
  assert.equal(await page.evaluate(()=>changedSettings().length),0);
  assert.equal(await page.locator('#proxy-password').getAttribute('type'),'password');
  assert.match(await page.locator('#proxy-password').getAttribute('placeholder'),/已保存/);cases++;

  await page.locator('#proxy-username').fill('renamed-user');
  await page.evaluate(()=>saveSettings());
  assert.equal(await page.evaluate(()=>requests.at(-1).data.password),null,'Blank keeps saved password');
  await page.locator('#proxy-clear-password').check();
  assert.equal(await page.locator('#proxy-password').isDisabled(),true);
  await page.evaluate(()=>saveSettings());
  assert.equal(await page.evaluate(()=>requests.at(-1).data.password),'');
  assert.equal(await page.locator('#proxy-saved-password').isVisible(),false);cases++;

  await page.locator('#proxy-mode label').filter({has:page.locator('input[value="auto"]')}).click();
  const savedRequests=await page.evaluate(()=>requests.length);
  await page.evaluate(()=>restoreSettings());
  assert.equal(await page.locator('#proxy-mode input[value="manual"]').isChecked(),true);
  assert.equal(await page.evaluate(()=>requests.length),savedRequests,'Restore discards draft without network changes');
  await page.locator('#proxy-mode label').filter({has:page.locator('input[value="auto"]')}).click();
  await page.evaluate(()=>{failure=true;return saveSettings();});
  assert.equal(await page.evaluate(()=>state.status.proxy_settings.mode),'manual');
  assert.match(await page.locator('#settings-message').textContent(),/Synthetic proxy failure/);
  assert.deepEqual(await page.evaluate(()=>changedSettings()),['proxy_settings']);
  await page.evaluate(()=>{failure=false;return saveSettings();});
  assert.equal(await page.evaluate(()=>state.status.proxy_settings.mode),'auto');cases++;

  await page.locator('#proxy-mode input[value="manual"]').check();
  await page.locator('#proxy-address').fill('http://unsaved.invalid:8080');
  await page.locator('#proxy-password').fill('test-only-secret');
  const savesBeforeTest=await page.evaluate(()=>requests.filter(r=>r.url==='/api/proxy-settings'&&r.data).length);
  await page.evaluate(()=>{holdTest=true;});
  await page.locator('#proxy-test').click();
  await page.waitForFunction(()=>typeof releaseTest==='function');
  assert.equal(await page.locator('#proxy-test').isDisabled(),true);
  assert.match(await page.locator('#proxy-test').textContent(),/测试中/);
  await page.evaluate(()=>{fillProxySettings();releaseTest();holdTest=false;});
  await page.waitForFunction(()=>$('proxy-test').getAttribute('aria-busy')==='false');
  assert.equal(await page.evaluate(()=>requests.at(-1).data.password),'test-only-secret');
  assert.match(await page.locator('#proxy-test-result').textContent(),/HTTP 401.*32 ms/);
  assert.match(await page.locator('#proxy-test-result').textContent(),/unsaved.invalid:8080/);
  assert.doesNotMatch(await page.locator('#proxy-test-result').textContent(),/test-only-secret/);
  assert.equal(await page.evaluate(()=>state.status.proxy_settings.mode),'auto');
  assert.equal(await page.evaluate(()=>requests.filter(r=>r.url==='/api/proxy-settings'&&r.data).length),savesBeforeTest);cases++;

  await page.evaluate(()=>{holdTest=true;window.releaseTest=undefined;});
  await page.locator('#proxy-test').click();
  await page.waitForFunction(()=>typeof releaseTest==='function');
  await page.locator('#proxy-address').fill('http://changed.invalid:8080');
  await page.evaluate(()=>{releaseTest();holdTest=false;});
  await page.waitForFunction(()=>$('proxy-test').getAttribute('aria-busy')==='false');
  assert.match(await page.locator('#proxy-test-result').textContent(),/配置已更改/);
  await page.evaluate(()=>{testState='rejected';});
  await page.locator('#proxy-test').click();
  await page.waitForFunction(()=>$('proxy-test').getAttribute('aria-busy')==='false');
  assert.match(await page.locator('#proxy-test-result').textContent(),/HTTP 407/);
  assert.equal(await page.locator('#proxy-test-result').evaluate(node=>node.classList.contains('error')),true);
  await page.locator('#proxy-address').fill('http://changed-again.invalid:8080');
  assert.equal(await page.locator('#proxy-test-result').isVisible(),false);
  await page.evaluate(()=>restoreSettings());cases++;

  for(const width of [1120,1440])for(const language of ['zh','en']){
    await page.setViewportSize({width,height:900});
    await page.evaluate(language=>{
      uiI18n.setLanguage(language,{persist:false});
      state.status.proxy_settings.detected={source:'windows_registry',http:{address:'http://long-regional-proxy.invalid:8080',username:'<img onerror="throw 1">',password_saved:true},
        https:null,bypass:'localhost,127.0.0.1,::1,*.example.internal',pac_url:'https://proxy.invalid/proxy.pac',unsupported_all_proxy:true};
      fillProxySettings();
    },language);
    assert.equal(await detectedAddress.locator('img').count(),0,'Detected content is text');
    assert.match(await detectedAddress.textContent(),/long-regional-proxy.invalid:8080/);
    assert.equal(await page.locator('#proxy-detection-warning').isVisible(),true);
    const overflow=await page.locator('#proxy-settings').evaluate(node=>node.scrollWidth>node.clientWidth+1);
    assert.equal(overflow,false,'Proxy settings fit the dialog');cases++;
  }
  await page.evaluate(()=>{state.status.proxy_settings_supported=false;fillProxySettings();});
  assert.equal(await page.locator('#proxy-settings').isVisible(),false);cases++;
  console.log(JSON.stringify({proxy_settings:'passed',browser_cases:cases}));
})().catch(error=>{console.error(error.stack);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();});
