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
  await page.evaluate(()=>{fillBadRequestRetry();settingsEditing=true;settingsBaseline=readSettingsDraft();document.getElementById('settings-dialog').showModal();});
  const enabled=page.locator('#bad-request-retry'),count=page.locator('#bad-request-retry-count');
  assert.equal(await enabled.isChecked(),false);assert.equal(await enabled.isDisabled(),false);
  assert.equal(await count.inputValue(),'3');assert.equal(await count.isDisabled(),true);cases++;
  for(const language of ['zh','en']){
    await page.evaluate(language=>{uiI18n.setLanguage(language,{persist:false});settingsLanguage=language;settingsBaseline=readSettingsDraft();},language);
    await enabled.check();assert.equal(await count.isDisabled(),false);
    await count.fill('4');
    const value=await page.evaluate(()=>({draft:readSettingsDraft(),changed:changedSettings()}));
    assert.equal(value.draft.bad_request_retry_enabled,true);
    assert.equal(value.draft.bad_request_retry_max_retries,'4');
    assert.deepEqual(value.changed.sort(),['bad_request_retry_enabled','bad_request_retry_max_retries']);
    await count.fill('10');assert.equal(await count.evaluate(node=>node.checkValidity()),false);
    await count.fill('9');assert.equal(await count.evaluate(node=>node.checkValidity()),true);
    await page.locator('#bad-request-retry-label').scrollIntoViewIfNeeded();
    const layout=await enabled.evaluate(node=>{
      const row=node.closest('.runtime-setting-row'),label=node.parentElement.getBoundingClientRect();
      const options=row.querySelector('.capacity-retry-options').getBoundingClientRect(),bounds=row.getBoundingClientRect();
      return {labelRight:label.right,optionsLeft:options.left,optionsRight:options.right,rowRight:bounds.right};
    });
    assert(layout.labelRight<=layout.optionsLeft,'the label and retry count must not overlap');
    assert(layout.optionsRight<=layout.rowRight+1,'the count fits the settings row');
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
