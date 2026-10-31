/* Isolated browser checks: no live application, relay, server or user profile. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
let chromium;
try {({chromium}=require('playwright'));}
catch (_) {console.log(JSON.stringify({skipped:'Playwright is not installed'}));process.exit(0);}
const edge=path.join(process.env['ProgramFiles(x86)']||'C:/Program Files (x86)','Microsoft/Edge/Application/msedge.exe');
const executablePath=process.env.CRO_TEST_BROWSER||(process.platform==='win32'&&fs.existsSync(edge)?edge:chromium.executablePath());
if(!fs.existsSync(executablePath)){console.log(JSON.stringify({skipped:'No browser engine is installed'}));process.exit(0);}
const root=path.join(__dirname,'../packet_inspector/ui'),read=name=>fs.readFileSync(path.join(root,name),'utf8');
let browser,cases=0;
async function load(name,{fallback=false}={}) {
  const page=await browser.newPage({viewport:{width:name==='tray'?240:1280,height:name==='tray'?230:900}});
  const html=read(name==='tray'?'tray.html':'index.html').replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,'').replace(/<link\b[^>]*>/g,'');
  await page.setContent(html);
  for(const file of name==='tray'?['tray.css']:['style.css','sharing.css'])await page.addStyleTag({content:read(file)});
  await page.addScriptTag({content:read('i18n.js')});
  await page.evaluate(fallback=>{
    if(fallback)Object.defineProperty(HTMLElement.prototype,'showPopover',{value:undefined,configurable:true});
    window.feedback=uiI18n.floatingFeedback();
    window.feedbackText=(id,value,error=false)=>{
      const node=document.getElementById(id);node.textContent=value;node.hidden=!value;node.classList.toggle('error',error);
    };
  },fallback);
  return page;
}
async function geometry(page,ids) {
  return page.evaluate(ids=>ids.map(id=>{
    const rect=(id.startsWith('.')?document.querySelector(id):document.getElementById(id)).getBoundingClientRect();
    return [rect.x,rect.y,rect.width,rect.height];
  }),ids);
}
async function visible(page,value=true) {
  await page.waitForFunction(value=>document.querySelector('.floating-feedback').hidden!==value,value);
}
async function settle(page) {await page.evaluate(()=>new Promise(resolve=>setTimeout(resolve,0)));}
async function run() {
  browser=await chromium.launch({executablePath,headless:true});
  const page=await load('main');
  await page.evaluate(()=>{
    document.getElementById('records-view').hidden=true;document.getElementById('sharing-view').hidden=false;
  });
  const sharingIds=['sharing-view','sharing-cards','quota-credential-rows','exit-credential-rows'];
  const before=await geometry(page,sharingIds);
  const error='当前后台不支持删除凭据，请更新软件后重试。';
  await page.evaluate(value=>{
    for(const id of ['sharing-message','sharing-inline-message','sharing-delete-message'])feedbackText(id,value,true);
  },error);
  await visible(page);
  assert.deepEqual(await geometry(page,sharingIds),before,'sharing errors must not move cards or tables');
  assert.equal(await page.locator('.floating-feedback').count(),1);
  assert.equal(await page.locator('.floating-feedback-content').innerText(),error);
  assert.equal(await page.locator('#sharing-message').evaluate(node=>getComputedStyle(node).display),'none');
  assert(await page.locator('.floating-feedback').evaluate(node=>node.matches(':popover-open')));
  cases++;

  await page.locator('.floating-feedback>button').click();await visible(page,false);
  await page.evaluate(value=>feedbackText('sharing-message',value,true),error);await settle(page);
  await visible(page,false); // Polling the same error cannot undo dismissal.
  await page.evaluate(()=>uiI18n.setLanguage('en',{persist:false}));await settle(page);await visible(page,false);
  await page.evaluate(()=>uiI18n.setLanguage('zh',{persist:false}));
  await page.evaluate(()=>feedbackText('sharing-message',''));await settle(page);
  await page.evaluate(value=>feedbackText('sharing-message',value,true),error);await visible(page);
  cases++;

  await page.evaluate(()=>document.getElementById('sharing-inline-editor').hidden=false);await visible(page);
  assert(await page.locator('.floating-feedback-content>.sharing-edit-message').count(),'inline error should win over its duplicate');
  await page.evaluate(()=>document.getElementById('sharing-inline-editor').hidden=true);await visible(page,false);
  cases++;

  await page.evaluate(()=>document.getElementById('sharing-delete-dialog').showModal());await visible(page);
  const deleteIds=['sharing-delete-dialog','sharing-delete-cancel','sharing-delete-confirm'];
  const deleteBefore=await geometry(page,deleteIds);
  assert.equal(await page.locator('.floating-feedback').evaluate(node=>node.parentElement.id),'sharing-delete-dialog');
  await page.evaluate(()=>feedbackText('sharing-delete-message','A longer deletion error '.repeat(15),true));
  await settle(page);assert.deepEqual(await geometry(page,deleteIds),deleteBefore);
  await page.keyboard.press('Escape');await visible(page,false);
  assert(await page.locator('#sharing-delete-dialog').evaluate(node=>node.open),'Escape dismisses feedback before closing the dialog');
  cases++;

  await page.locator('#sharing-delete-cancel').focus();
  await page.evaluate(()=>{
    uiI18n.text(document.getElementById('notice-text'),'设置已保存。');
    document.getElementById('notice').hidden=false;
  });await visible(page);
  assert.equal(await page.locator('.floating-feedback').getAttribute('role'),'status','informational notice stays informational');
  assert.equal(await page.evaluate(()=>document.activeElement.id),'sharing-delete-cancel','showing feedback must not steal focus');
  await page.evaluate(()=>uiI18n.setLanguage('en',{persist:false}));
  await page.waitForFunction(()=>document.querySelector('.floating-feedback-content').textContent==='Settings saved.');
  assert.equal(await page.locator('.floating-feedback>button').getAttribute('aria-label'),'Dismiss notification');
  await page.locator('.floating-feedback>button').click();
  assert.equal(await page.evaluate(()=>document.activeElement.id),'sharing-delete-cancel');
  cases++;

  await page.evaluate(()=>{
    document.getElementById('sharing-delete-dialog').close();document.getElementById('settings-dialog').showModal();
  });
  const settingsIds=['settings-dialog','settings-save','settings-restore','context-settings'];
  const settingsBefore=await geometry(page,settingsIds);
  await page.evaluate(()=>{
    document.getElementById('settings-message').textContent='Saving settings… 2/3';
    const log=document.getElementById('settings-save-log');log.hidden=false;
    for(let i=0;i<6;i++) {
      const li=document.createElement('li');li.textContent='Saving section '+i;li.dataset.state=i===5?'error':'done';log.append(li);
    }
  });await visible(page);
  assert.deepEqual(await geometry(page,settingsIds),settingsBefore,'save progress cannot grow the footer');
  assert.equal(await page.locator('.floating-feedback').getAttribute('role'),'alert');
  assert.equal(await page.locator('.floating-feedback-content .settings-save-log li').count(),6);
  assert.equal(await page.locator('.floating-feedback-content [id]').count(),0,'copies must not duplicate control IDs');
  cases++;

  // The newest modal can occur earlier in document order (exit from settings).
  await page.evaluate(()=>document.getElementById('exit-confirm-dialog').showModal());await settle(page);
  assert.equal(await page.locator('.floating-feedback').evaluate(node=>node.parentElement.id),'exit-confirm-dialog');
  await page.evaluate(()=>feedbackText('exit-confirm-message','Exit failed',true));await visible(page);
  await page.evaluate(()=>{
    document.getElementById('notice').classList.remove('error');
    document.getElementById('notice-text').textContent='Background information';
  });await settle(page);
  assert.equal(await page.locator('.floating-feedback-content').innerText(),'Exit failed','information cannot overwrite an active error');
  await page.evaluate(()=>document.getElementById('exit-confirm-dialog').close());
  cases++;

  await page.evaluate(()=>{
    document.getElementById('settings-dialog').close();document.getElementById('sharing-view').hidden=true;
    document.getElementById('records-view').hidden=false;feedbackText('list-loading','正在加载更多记录…');
  });await visible(page,false);
  await page.evaluate(()=>feedbackText('list-loading',''));await page.waitForTimeout(240);await visible(page,false);
  await page.evaluate(()=>feedbackText('list-loading','正在加载更多记录…'));await visible(page);
  assert.equal(await page.locator('.floating-feedback').getAttribute('role'),'status');
  await page.evaluate(()=>feedbackText('list-loading',''));await visible(page,false);
  cases++;

  const fallback=await load('main',{fallback:true});
  await fallback.evaluate(()=>document.getElementById('settings-dialog').showModal());
  const fallbackBefore=await geometry(fallback,['settings-dialog','settings-save']);
  await fallback.evaluate(()=>feedbackText('context-message','Cannot read context settings',true));await visible(fallback);
  assert.equal(await fallback.locator('.floating-feedback').evaluate(node=>node.parentElement.id),'settings-dialog');
  assert.equal(await fallback.locator('.floating-feedback').evaluate(node=>getComputedStyle(node).position),'fixed');
  assert.deepEqual(await geometry(fallback,['settings-dialog','settings-save']),fallbackBefore);
  cases++;

  const tray=await load('tray');
  const trayBefore=await geometry(tray,['.tray-panel','tray-open','tray-exit']);
  await tray.evaluate(()=>feedbackText('tray-error','无法完成操作，请稍后重试。',true));await visible(tray);
  assert.deepEqual(await geometry(tray,['.tray-panel','tray-open','tray-exit']),trayBefore,'tray feedback cannot resize the native menu');
  const bounds=await tray.locator('.floating-feedback').boundingBox();
  assert(bounds.x>=0&&bounds.x+bounds.width<=240&&bounds.y>=0&&bounds.y+bounds.height<=230);
  cases++;
  console.log(JSON.stringify({floating_feedback:'passed',browser_cases:cases}));
}
run().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{await browser?.close();});
