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
    window.state={status:{proxy_settings_supported:true,proxy_settings:{mode:'auto',
      manual:{address:'',bypass:'localhost,127.0.0.1,::1,*.saved.invalid'},
      detected:{source:'environment',http:{address:'http://alternate.invalid:8080'},
        https:{address:'http://regional.invalid:8080'},bypass:'localhost'},
      effective:{}}}};
    window.storageLimitSettings=()=>({toBackend:Number});
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
        if(testState==='api-error')throw Error('Synthetic probe error');
        const status={ok:404,rejected:407}[testState];
        return {state:testState,status,elapsed_ms:32,target:'https://upstream.invalid/responses',
          proxy_used:true,proxy:data.address||'http://regional.invalid:8080',
          error:testState==='failed'?'Connection refused\nTry again':undefined};
      }
      if(data&&holdSave)await new Promise(resolve=>{window.releaseSave=resolve;});
      if(data&&failure)throw Error('Synthetic proxy failure');
      const before=state.status.proxy_settings;
      return {...before,mode:data.mode,manual:data.mode==='manual'?{...before.manual,address:data.address}:before.manual};
    };
    for(const input of $('settings-dialog').querySelectorAll('input'))input.disabled=true;
  });
  await page.addScriptTag({content:[
    extract('function selectedProxyMode()','function fillImageAnalysisMode('),
    extract('function readSettingsDraft()','function setSettingsMessage('),
    extract('function finishSettingsSave()','function renderUpdates('),
    extract('function discardSettingsDraft()','function closeSettings()'),
    extract('action("proxy-test",','action("storage-dir-open",')
  ].join('\n')});
  await page.evaluate(()=>{fillProxySettings();settingsBaseline=readSettingsDraft();settingsEditing=true;$('settings-dialog').showModal();});
  const sections=await page.locator('.settings-content>section').evaluateAll(nodes=>nodes.map(node=>node.id));
  assert.deepEqual(sections.slice(-2),['connection-settings','record-storage-settings']);
  assert.equal(await page.locator('#connection-settings > fieldset#proxy-settings').count(),1);
  assert.deepEqual(await page.locator('#connection-settings .entry-port-row input').evaluateAll(nodes=>nodes.map(node=>node.id)),
    ['entry-port','sharing-port','exit-port']);cases++;
  const detectedAddress=page.locator('#proxy-detected-address');
  const manualAddress=page.locator('#proxy-address');
  const proxyRow=page.locator('#proxy-settings .proxy-inline-row');
  const testStatus=page.locator('#proxy-test-result');
  const testStateText=testStatus.locator('#proxy-test-state');
  const testElapsed=testStatus.locator('#proxy-test-elapsed');
  assert.equal(await detectedAddress.isDisabled(),true);
  assert.equal(await page.locator('#proxy-settings input[type=text]:visible').count(),1);
  assert.equal(await page.locator('#proxy-settings input[type=password]').count(),0);
  assert.match(await detectedAddress.inputValue(),/regional.invalid:8080/);
  assert.doesNotMatch(await page.locator('#proxy-settings').textContent(),/检测来源|代理例外|保存后用于新请求/);
  assert.equal(await detectedAddress.evaluate(output=>output.labels[0].textContent),'代理地址');
  await page.evaluate(()=>{state.status.profile={upstream:'http://upstream.invalid/v1'};fillProxySettings();});
  assert.match(await detectedAddress.inputValue(),/alternate.invalid:8080/);
  await page.evaluate(()=>{state.status.profile.upstream='https://upstream.invalid/v1';fillProxySettings();});
  assert.equal(await manualAddress.isDisabled(),true);
  assert.equal(await page.locator('#proxy-detect').count(),0);
  await page.evaluate(()=>{state.status.proxy_settings.detected.https.address='http://new-region.invalid:8080';fillProxySettings();});
  assert.match(await detectedAddress.inputValue(),/new-region.invalid/);
  assert.equal(await page.evaluate(()=>requests.filter(r=>r.data).length),0);cases++;

  await page.locator('#proxy-mode label').filter({has:page.locator('input[value="manual"]')}).click();
  assert.equal(await manualAddress.isEnabled(),true);
  assert.equal(await page.locator('#proxy-settings input[type=text]:visible').count(),1);
  await page.evaluate(()=>saveSettings());
  await page.locator('#proxy-test').click();
  assert.equal(await page.evaluate(()=>requests.filter(r=>r.data).length),0,'Invalid manual settings must not save');
  await manualAddress.fill('http://manual.invalid:8080');
  await page.locator('#proxy-mode input[value="auto"]').check();
  assert.equal(await detectedAddress.isDisabled(),true);
  await page.locator('#proxy-mode input[value="auto"]').focus();
  await page.keyboard.press('ArrowRight');
  assert.equal(await page.locator('#proxy-mode input[value="manual"]').isChecked(),true);
  await page.evaluate(()=>fillProxySettings());
  assert.equal(await manualAddress.inputValue(),'http://manual.invalid:8080','Mode changes and refreshes preserve the draft');
  assert.equal(await page.evaluate(()=>requests.filter(r=>r.data).length),0,'Changing mode and typing must not auto-save');
  await page.evaluate(()=>{holdSave=true;window.saving=saveSettings();});
  assert.equal(await manualAddress.isDisabled(),true);
  await page.evaluate(async()=>{releaseSave();await saving;holdSave=false;});
  assert.deepEqual(await page.evaluate(()=>requests.at(-1).data),{mode:'manual',address:'http://manual.invalid:8080'});
  assert.equal(await page.evaluate(()=>state.status.proxy_settings.manual.bypass),'localhost,127.0.0.1,::1,*.saved.invalid');
  assert.equal(await page.evaluate(()=>changedSettings().length),0);cases++;

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
  await manualAddress.fill('http://unsaved.invalid:8080');
  const savesBeforeTest=await page.evaluate(()=>requests.filter(r=>r.url==='/api/proxy-settings'&&r.data).length);
  await page.evaluate(()=>{holdTest=true;});
  await page.locator('#proxy-test').click();
  await page.waitForFunction(()=>typeof releaseTest==='function');
  assert.equal(await page.locator('#proxy-test').isDisabled(),true);
  assert.match(await page.locator('#proxy-test').textContent(),/测试中/);
  assert.equal(await testStateText.textContent(),'');
  assert.equal(await testElapsed.isVisible(),false);
  await page.evaluate(()=>{fillProxySettings();releaseTest();holdTest=false;});
  await page.waitForFunction(()=>$('proxy-test').getAttribute('aria-busy')==='false');
  assert.deepEqual(await page.evaluate(()=>requests.at(-1).data),{mode:'manual',address:'http://unsaved.invalid:8080'});
  assert.equal(await testStateText.textContent(),'已连通');
  assert.equal(await testElapsed.textContent(),'32 ms');
  assert.equal(await testElapsed.isVisible(),true);
  assert.equal(await testStatus.getAttribute('role'),'status');
  assert.equal(await testStatus.getAttribute('aria-live'),'polite');
  assert.equal(await testStatus.getAttribute('aria-atomic'),'true');
  assert.equal(await testStatus.evaluate(node=>node.previousElementSibling?.id),'proxy-test');
  await page.evaluate(()=>uiI18n.setLanguage('en',{persist:false}));
  assert.equal(await testStateText.textContent(),'Reachable');
  assert.equal(await testElapsed.textContent(),'32 ms');
  await page.evaluate(()=>uiI18n.setLanguage('zh',{persist:false}));
  assert.equal(await page.evaluate(()=>state.status.proxy_settings.mode),'auto');
  assert.equal(await page.evaluate(()=>requests.filter(r=>r.url==='/api/proxy-settings'&&r.data).length),savesBeforeTest);cases++;

  await page.evaluate(()=>{holdTest=true;window.releaseTest=undefined;});
  await page.locator('#proxy-test').click();
  await page.waitForFunction(()=>typeof releaseTest==='function');
  await page.locator('#proxy-address').fill('http://changed.invalid:8080');
  await page.evaluate(()=>{releaseTest();holdTest=false;});
  await page.waitForFunction(()=>$('proxy-test').getAttribute('aria-busy')==='false');
  assert.equal(await testStateText.textContent(),'需重测');
  assert.equal(await testElapsed.isVisible(),false);
  for(const [state,label] of [['rejected','响应异常'],['failed','连接失败'],['timeout','测试超时'],['api-error','测试失败']]){
    await page.evaluate(state=>{testState=state;},state);
    await page.locator('#proxy-test').click();
    await page.waitForFunction(()=>$('proxy-test').getAttribute('aria-busy')==='false');
    assert.equal(await testStateText.textContent(),label);
    assert.equal(await testElapsed.isVisible(),false);
    assert.equal(await testStatus.evaluate(node=>node.classList.contains('error')),true);cases++;
  }
  await page.locator('#proxy-address').fill('http://changed-again.invalid:8080');
  assert.equal(await testStateText.textContent(),'');
  await page.evaluate(()=>{setProxyTestStatus('ok',32);restoreSettings();});
  assert.equal(await testStateText.textContent(),'');
  assert.equal(await testElapsed.isVisible(),false);cases++;

  for(const width of [320,420,540,600,1120,1440])for(const language of ['zh','en']){
    await page.setViewportSize({width,height:900});
    await page.evaluate(language=>{
      uiI18n.setLanguage(language,{persist:false});
      state.status.proxy_settings.detected={source:'windows_registry',http:{address:'http://long-regional-proxy.invalid:8080'},
        https:null,bypass:'localhost,127.0.0.1,::1,*.example.internal',pac_url:'https://proxy.invalid/proxy.pac',unsupported_all_proxy:true};
      fillProxySettings();
    },language);
    assert.equal(await page.getByRole('group',{name:language==='zh'?'出口代理':'Outbound proxy',exact:true}).getAttribute('id'),'proxy-settings');
    assert.deepEqual(await proxyRow.locator('#proxy-mode label span').allTextContents(),language==='zh'?['自动','手动']:['Auto','Manual']);
    assert.match(await detectedAddress.inputValue(),/long-regional-proxy.invalid:8080/);
    for(const mode of ['auto','manual']){
      await page.locator('#proxy-mode input[value="'+mode+'"]').check();
      assert.equal(await page.locator('#proxy-detection-warning').isVisible(),mode==='auto');
      await page.locator('#proxy-test').scrollIntoViewIfNeeded();
      const address=mode==='auto'?'#proxy-detected-address':'#proxy-address';
      const controls=await proxyRow.locator('#proxy-settings-label,#proxy-mode,'+address+',#proxy-test').evaluateAll(nodes=>nodes.map(node=>{
        const r=node.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height};
      }));
      assert(controls.slice(1).every(r=>r.width>=24&&r.height>=24),'Controls remain usable');
      assert(controls.slice(1).every(r=>r.x>=controls[0].x+controls[0].width),'Proxy actions stay to the right of the key');
      if(width>=420){
        const inlineControls=width>=600?controls:controls.slice(2);
        const rowCenter=inlineControls[0].y+inlineControls[0].height/2;
        assert(inlineControls.every(r=>Math.abs(r.y+r.height/2-rowCenter)<1),`Proxy controls align vertically at ${width}px (${language}, ${mode})`);
        for(let i=1;i<inlineControls.length;i++){
          assert(inlineControls[i].x>=inlineControls[i-1].x+inlineControls[i-1].width,'Controls do not overlap');
        }
      }
      if(width>=600){
        const portLabel=await page.locator('#entry-port-label').boundingBox();
        const port=await page.locator('#entry-port').boundingBox();
        const localDevice=await page.locator('label[for="storage-dir"]').boundingBox();
        assert.equal(controls[0].x,portLabel.x,'The proxy label aligns with settings labels');
        assert.equal(controls[1].x,port.x,'The proxy controls start in the same column as the port input');
        assert.equal(controls[1].x,localDevice.x,'The proxy and directory controls start in the same column');
      }
      const openDirectory=await page.locator('#storage-dir-open').boundingBox();
      assert.equal(controls[3].width,openDirectory.width,'The test and open buttons share a width');
      if(width>=1120){
        const directories=await page.locator('.storage-directory-controls').boundingBox();
        assert.equal(openDirectory.x+openDirectory.width,directories.x+directories.width,'The open button only reserves its own width');
      }
      const rowBox=await proxyRow.boundingBox();
      const actionsBox=await proxyRow.locator('.proxy-test-actions').boundingBox();
      assert(Math.abs(actionsBox.x+actionsBox.width-rowBox.x-rowBox.width)<1,'Test actions sit at the right edge of the row');
      const beforeTest=await page.locator('#proxy-test').boundingBox();
      await page.evaluate(()=>{$('proxy-test').setAttribute('aria-busy','true');fillProxySettings();});
      assert.deepEqual(await page.locator('#proxy-test').boundingBox(),beforeTest,'Testing state does not shift the button');
      await page.evaluate(()=>{
        $('proxy-test').setAttribute('aria-busy','false');fillProxySettings();
        setProxyTestStatus('ok',453);
      });
      assert.equal(await testStateText.textContent(),language==='zh'?'已连通':'Reachable');
      assert.equal(await testElapsed.textContent(),'453 ms');
      assert.deepEqual(await page.locator('#proxy-test').boundingBox(),beforeTest,'The result does not shift the button');
      const statusBox=await testStatus.boundingBox();
      assert(statusBox.x>=beforeTest.x+beforeTest.width,'The result is after the button');
      assert(Math.abs(statusBox.y+statusBox.height/2-(beforeTest.y+beforeTest.height/2))<1,'The result is centered beside the button');
      const stateBox=await testStateText.boundingBox();
      const elapsedBox=await testElapsed.boundingBox();
      assert(elapsedBox,'The duration is visible');
      if(width>=1120)assert(elapsedBox.x>=stateBox.x+stateBox.width,'The duration fits beside the status label');
      const overflow=await page.locator('#proxy-settings').evaluate(node=>node.scrollWidth>node.clientWidth+1);
      assert.equal(overflow,false,'Proxy settings fit the dialog');cases++;
      await page.evaluate(()=>setProxyTestStatus());
    }
  }
  await page.evaluate(()=>{state.status.proxy_settings_supported=false;fillProxySettings();});
  assert.equal(await page.locator('#proxy-settings').isVisible(),false);cases++;
  console.log(JSON.stringify({proxy_settings:'passed',browser_cases:cases}));
})().catch(error=>{console.error(error.stack);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();});
