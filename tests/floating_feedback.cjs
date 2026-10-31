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

  await page.keyboard.press('Escape');await visible(page,false);
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
  assert.equal(await page.locator('.floating-feedback button').count(),0,'feedback has no close control');
  await visible(page,false);
  assert.equal(await page.evaluate(()=>document.activeElement.id),'sharing-delete-cancel');
  await page.locator('#sharing-delete-cancel').click();
  await page.evaluate(()=>{document.getElementById('notice').hidden=false;});await visible(page);
  assert.equal(await page.locator('.floating-feedback-content').innerText(),'Settings saved.','the same feedback can appear again for a new action');
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
  await fallback.locator('#settings-save').evaluate(node=>{node.disabled=false;});
  await fallback.locator('#settings-save').click();
  const fallbackBefore=await geometry(fallback,['settings-dialog','settings-save']);
  await fallback.evaluate(()=>feedbackText('context-message','Cannot read context settings',true));await visible(fallback);
  assert.equal(await fallback.locator('.floating-feedback').evaluate(node=>node.parentElement.id),'settings-dialog');
  assert.equal(await fallback.locator('.floating-feedback').evaluate(node=>getComputedStyle(node).position),'fixed');
  assert.deepEqual(await geometry(fallback,['settings-dialog','settings-save']),fallbackBefore);
  const fallbackToast=await fallback.locator('.floating-feedback').boundingBox(),saveButton=await fallback.locator('#settings-save').boundingBox();
  assert(Math.abs(fallbackToast.y+fallbackToast.height-saveButton.y)<=12,'dialog feedback should stay near the triggering control');
  cases++;

  const sharing=await load('main');
  await sharing.evaluate(()=>{
    document.getElementById('records-view').hidden=true;document.getElementById('sharing-view').hidden=false;
    const snapshot={supported:true,protocol:4,credentials_supported:true,host:{active:true,running:true},client:{active:false},
      egress:{supported:true,host:{active:false},selection:{mode:'local'}},credentials:[
        {id:'quota1',kind:'quota',direction:'out',label:'Quota',enabled:true,expires_at:null,budget_nanos:10e9,totals:{}},
        {id:'exit1',kind:'exit',direction:'out',label:'Exit',enabled:true,expires_at:null,totals:{}}
      ]};
    window.fetch=async(_,options)=>{
      const action=options.body?JSON.parse(options.body).action:null;
      if(action==='credential_copy')await new Promise(resolve=>{window.finishCopy=resolve;});
      return {ok:true,json:async()=>({...snapshot,...(action==='credential_copy'?{link:'synthetic-secret'}:{})})};
    };
    Object.defineProperty(navigator,'clipboard',{value:{writeText:async value=>{window.copiedValue=value;}},configurable:true});
  });
  await sharing.addScriptTag({content:read('sharing.js')});
  const copyGeometry=()=>sharing.evaluate(()=>[...document.querySelectorAll('.sharing-table th,.sharing-copy-button')].map(node=>{
    const rect=node.getBoundingClientRect();return [rect.x,rect.y,rect.width,rect.height];
  }));
  for(const [language,id] of [['zh','quota1'],['en','exit1']]) {
    await sharing.evaluate(language=>{uiI18n.setLanguage(language,{persist:false});InspectorSharing.setActive(true);},language);
    const selector=`[data-action="copy"][data-id="${id}"]`;
    await sharing.waitForSelector(selector);
    const copyBefore=await copyGeometry(),caption=await sharing.locator(selector).innerText();
    await sharing.locator(selector).click();
    await sharing.waitForFunction(()=>typeof window.finishCopy==='function');
    assert.deepEqual(await copyGeometry(),copyBefore,'copy progress cannot widen a button or a table column');
    assert.equal(await sharing.locator(selector).innerText(),caption);
    await sharing.evaluate(()=>{window.finishCopy();window.finishCopy=null;});await visible(sharing);
    assert.equal(await sharing.locator('.floating-feedback-content').innerText(),
      language==='zh'?'已复制，仅供一台机器使用':'Copied. For use on one machine only.');
    assert.equal(await sharing.evaluate(()=>window.copiedValue),'synthetic-secret');
    assert.deepEqual(await copyGeometry(),copyBefore,'copy success cannot resize the button or the columns');
    assert.equal(await sharing.locator(selector).innerText(),caption);
    assert.equal(await sharing.locator('body').innerText().then(text=>text.includes('synthetic-secret')),false);
    assert.equal(await sharing.locator('.floating-feedback button').count(),0);
    const toast=await sharing.locator('.floating-feedback').boundingBox(),anchor=await sharing.locator(selector).boundingBox();
    assert(toast.x<anchor.x+anchor.width&&toast.x+toast.width>anchor.x,'copy feedback must overlap its button horizontally');
    assert(Math.min(Math.abs(toast.y+toast.height-anchor.y),Math.abs(anchor.y+anchor.height-toast.y))<=12,'copy feedback must stay near the clicked button');
    if(language==='zh') {
      const appeared=Date.now();await sharing.waitForTimeout(1000);
      await sharing.evaluate(()=>{const node=document.getElementById('sharing-message');node.textContent=node.textContent;});
      await visible(sharing,false);
      assert(Date.now()-appeared<3000,'unchanged polling must not keep restarting the dismissal timer');
    }
    else {
      // Replacing a brief success with an error cancels the previous timer.
      await sharing.evaluate(()=>feedbackText('sharing-message','Copy follow-up error',true));
      await sharing.waitForTimeout(2600);await visible(sharing);
      assert.equal(await sharing.locator('.floating-feedback-content').innerText(),'Copy follow-up error');
      await visible(sharing,false);
    }
    cases++;
  }

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
