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
    window.updateStatus={supported:false,current_version:'0.8.7',state:'idle'};
    window.updateBusy=false;window.updateAction='';window.updateFeedbackVisible=false;
    window.fetch=()=>{throw new Error('Capability changes must not send requests');};
    $('settings-dialog').showModal();
  });
  await page.addScriptTag({content:source.slice(source.indexOf('function imageAnalysisMode('),source.indexOf('function fillZstdLevel('))+
    '\n'+source.slice(source.indexOf('function renderUpdates('),source.indexOf('async function loadUpdates('))});
  const modes=page.locator('#request-protection-settings #image-analysis-mode');
  const hint=page.locator('#image-isolation-hint');
  for(const lang of ['zh','en']){
    await page.evaluate(lang=>{uiI18n.setLanguage(lang);fillImageAnalysisMode();renderUpdates();},lang);
    const groupName=lang==='zh'?'解图优化':'Image analysis optimization';
    assert.equal(await modes.getAttribute('aria-labelledby'),'image-analysis-mode-label');
    assert.equal(await page.getByRole('group',{name:groupName,exact:true}).getAttribute('id'),'image-analysis-mode');
    const modeBox=await modes.boundingBox();
    const protectionBox=await page.locator('#packet-size-protection').boundingBox();
    assert.equal(modeBox.x,protectionBox.x,'Image modes start in the request protection controls column');
    assert((await hint.boundingBox()).x>=modeBox.x+modeBox.width,'The mode hint sits beside the controls');
    assert.equal(await modes.locator('input[value="optimized"]').isDisabled(),true);
    assert.equal(await modes.locator('input[value="native"]').isDisabled(),false);
    assert.equal(await modes.locator('input[value="disabled"]').isDisabled(),false);
    assert.equal(await page.locator('#updates-check').isDisabled(),true);
    assert.equal(await page.locator('#updates-install').isDisabled(),true);
    assert.match(await page.locator('#updates-message').textContent(),/Linux/);cases++;
  }
  await page.evaluate(()=>{delete state.status.image_analysis_modes;delete state.status.updates_supported;state.status.image_analysis_mode='optimized';updateStatus={state:'available'};fillImageAnalysisMode();renderUpdates();});
  assert.equal(await modes.locator('input[value="optimized"]').isDisabled(),false);
  assert.equal(await modes.locator('input[value="optimized"]').isChecked(),true);
  assert.equal(await hint.textContent(),'Keep only image paths in the main request.');
  assert.equal(await page.locator('#updates-check').isDisabled(),false);
  assert.equal(await page.locator('#updates-install').isDisabled(),false);
  assert.equal(await page.locator('#updates-install').textContent(),'Update');cases++;
  await page.setViewportSize({width:320,height:900});
  assert.equal(await modes.evaluate(node=>node.parentElement.scrollWidth<=node.parentElement.clientWidth+1),true);
  await page.evaluate(()=>{settingsSaving=true;fillImageAnalysisMode();renderUpdates();});
  assert.equal(await modes.locator('input[value="native"]').isDisabled(),true);cases++;
  console.log(JSON.stringify({linux_settings:'passed',browser_cases:cases}));
})().catch(error=>{console.error(error.stack);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();});
