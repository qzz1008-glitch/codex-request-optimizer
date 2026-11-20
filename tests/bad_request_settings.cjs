/* Real settings controls: opt-in, editable count, old workers and bilingual layout. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
let chromium;
try{({chromium}=require('playwright'));}catch(_){console.log(JSON.stringify({skipped:'Playwright is not installed'}));process.exit(0);}
const edge=path.join(process.env['ProgramFiles(x86)']||'C:/Program Files (x86)','Microsoft/Edge/Application/msedge.exe');
const executablePath=process.env.CRO_TEST_BROWSER||(process.platform==='win32'&&fs.existsSync(edge)?edge:chromium.executablePath());
if(!fs.existsSync(executablePath)){console.log(JSON.stringify({skipped:'No browser engine is installed'}));process.exit(0);}
const root=path.join(__dirname,'../packet_inspector/ui'),read=name=>fs.readFileSync(path.join(root,name),'utf8'),source=read('app.js');
const html=read('index.html').replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,'').replace(/<link\b[^>]*>/g,'');
const controls=source.slice(source.indexOf('function fillBadRequestRetry()'),source.indexOf('function renderContextPlaceholders()'));
const draft=source.slice(source.indexOf('function readSettingsDraft()'),source.indexOf('function setSettingsMessage('));
let browser,cases=0;
async function run(){
  browser=await chromium.launch({executablePath,headless:true});
  const page=await browser.newPage({viewport:{width:1120,height:900}});
  await page.route('http://retry-settings.test/**',route=>route.fulfill({contentType:'text/html',body:html}));
  await page.goto('http://retry-settings.test/');
  await page.addStyleTag({content:read('style.css')});
  await page.addScriptTag({content:read('i18n.js')});
  await page.evaluate(()=>{
    window.$=id=>document.getElementById(id);window.settingsSaving=false;window.settingsEditing=false;
    window.settingsLanguage=null;window.selectedImageAnalysisMode=()=> 'native';
    window.state={status:{bad_request_retry_enabled:false,bad_request_retry_supported:true}};
    window.settingsBaseline=null;
  });
  await page.addScriptTag({content:controls+'\n'+draft});
  await page.evaluate(()=>{
    fillBadRequestRetry();settingsEditing=true;settingsBaseline=readSettingsDraft();
    $('sharing-settings').hidden=false;$('proxy-settings').hidden=false;
    $('settings-dialog').showModal();
  });
  const enabled=page.locator('#bad-request-retry'),count=page.locator('#bad-request-retry-count');
  assert.equal(await enabled.isChecked(),false);assert.equal(await enabled.isDisabled(),false);
  assert.equal(await count.inputValue(),'3');assert.equal(await count.isDisabled(),true);cases++;
  const settings=page.locator('#settings-dialog .settings-content');
  const interactive='input:not([type=hidden]),select,button,a[href],[tabindex]';
  assert.deepEqual(await settings.locator(interactive).evaluateAll(nodes=>
    nodes.filter(node=>!node.closest('.settings-controls')).map(node=>node.id)),[]);
  await settings.evaluate((section,selector)=>{
    window.settingInteractions=[];
    for(const event of ['click','focusin','change']){
      section.addEventListener(event,({target})=>{
        if(target.closest(selector))settingInteractions.push([event,target.id]);
      });
    }
    for(const id of ['auto-zstd-level','zstd-level-open','image-zstd-level-open'])$(id).disabled=false;
  },interactive);
  for(const key of await settings.locator('.settings-key').all()){
    assert.equal(await key.evaluate(node=>node.tagName),'SPAN');
    await key.click();
  }
  assert.deepEqual(await page.evaluate(()=>settingInteractions),[],'Clicking a key never activates or focuses a setting');cases++;
  for(const language of ['zh','en']){
    await page.evaluate(language=>{uiI18n.setLanguage(language,{persist:false});settingsLanguage=language;settingsBaseline=readSettingsDraft();},language);
    const names=await settings.locator('.settings-key').allTextContents();
    if(language==='zh'){
      assert.deepEqual(names.filter(name=>!/^\p{Script=Han}{4}$/u.test(name)),[],'Every Chinese setting name has four characters');
    }else{
      assert.deepEqual(names.filter(name=>/\p{Script=Han}/u.test(name)),[],'Setting names are translated');
    }
    await enabled.check();assert.equal(await count.isDisabled(),false);
    await count.fill('4');
    const value=await page.evaluate(()=>({draft:readSettingsDraft(),changed:changedSettings()}));
    assert.equal(value.draft.bad_request_retry_enabled,true);
    assert.equal(value.draft.bad_request_retry_max_retries,'4');
    assert.deepEqual(value.changed.sort(),['bad_request_retry_enabled','bad_request_retry_max_retries']);
    await count.fill('10');assert.equal(await count.evaluate(node=>node.checkValidity()),false);
    await count.fill('9');assert.equal(await count.evaluate(node=>node.checkValidity()),true);
    assert.equal(await page.getByRole('checkbox',{name:language==='zh'?'异常重试':'Retry request errors',exact:true}).getAttribute('id'),'bad-request-retry');
    assert.equal(await page.getByRole('spinbutton',{name:language==='zh'?'中转入口':'Relay entry',exact:true}).getAttribute('id'),'entry-port');
    assert.equal(await page.locator('#budget-label').textContent(),language==='zh'?'大小上限':'Size limit');
    for(const width of [320,540,600,1120]){
      await page.setViewportSize({width,height:900});
      const rows=await settings.locator('.settings-row').evaluateAll(nodes=>nodes.map(row=>{
        const [key,controls]=row.children;
        const names=key.getBoundingClientRect(),values=controls.getBoundingClientRect();
        return [key.textContent,names.x,names.right,values.x,values.right,row.getBoundingClientRect().right];
      }));
      for(const [name,keyX,keyRight,controlsX,controlsRight,rowRight] of rows){
        assert.equal(keyX,rows[0][1],name+' aligns with the left column');
        assert.equal(controlsX,rows[0][3],name+' aligns with the right column');
        assert(keyRight<controlsX&&controlsRight<=rowRight+1,name+' keeps distinct columns');
      }
      assert.equal(await settings.evaluate(node=>node.scrollWidth<=node.clientWidth+1),true,'Settings fit the dialog');
      const columns=await page.evaluate(()=>[
        ['#packet-size-protection','#budget-label','#budget','#budget-unit'],
        ['#capacity-retry','label[for="capacity-retry-count"]','#capacity-retry-count','#capacity-retry-count+span'],
        ['#bad-request-retry','label[for="bad-request-retry-count"]','#bad-request-retry-count','#bad-request-retry-count+span'],
      ].map(selectors=>selectors.map(selector=>document.querySelector(selector).getBoundingClientRect().left)));
      const [sizeLimit,...retryLimits]=columns;
      for(const limit of retryLimits)assert.deepEqual(limit,sizeLimit,'The checkboxes, number labels, inputs and units share columns');
      cases++;
    }
    assert.equal(await enabled.getAttribute('data-settings-field'),null,'no native restart is needed');
    await enabled.uncheck();await count.evaluate(node=>{node.value='';});
    assert.equal(await count.isDisabled(),true);
    assert.deepEqual(await page.evaluate(()=>changedSettings()),[],'an unchecked count does not leave a dirty draft');cases++;
  }
  await page.evaluate(()=>{state.status.bad_request_retry_supported=false;fillBadRequestRetry();});
  assert.equal(await enabled.isDisabled(),true);assert.equal(await count.isDisabled(),true);cases++;
  await page.evaluate(()=>{settingsEditing=false;state.status={bad_request_retry_supported:true,bad_request_retry_enabled:true,bad_request_retry_max_retries:7};fillBadRequestRetry();});
  assert.equal(await enabled.isChecked(),true);assert.equal(await count.inputValue(),'7');assert.equal(await count.isDisabled(),false);cases++;
  console.log(JSON.stringify({bad_request_settings:'passed',browser_cases:cases}));
}
run().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();});
