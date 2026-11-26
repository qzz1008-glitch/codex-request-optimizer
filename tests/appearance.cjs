/* Desktop appearance drafts, live theme changes and shared tray colors. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
let chromium;
try {({chromium}=require('playwright'));} catch (_) {console.log('Appearance checks skipped: Playwright unavailable');process.exit(0);}
const edge=path.join(process.env['ProgramFiles(x86)']||'C:/Program Files (x86)','Microsoft/Edge/Application/msedge.exe');
const executablePath=process.env.CRO_TEST_BROWSER||(process.platform==='win32'&&fs.existsSync(edge)?edge:chromium.executablePath());
if(!fs.existsSync(executablePath)){console.log('Appearance checks skipped: browser unavailable');process.exit(0);}

const assets=path.join(__dirname,'../packet_inspector/ui');
const scheme=page=>page.evaluate(()=>getComputedStyle(document.documentElement).colorScheme==='dark'?'dark':'light');
const styles=(page,selector)=>page.locator(selector).evaluate(node=>{
  const style=getComputedStyle(node);
  return {background:style.backgroundColor,text:style.color};
});
function luminance(color){
  const channels=color.match(/\d+/g).slice(0,3).map(value=>Number(value)/255).map(value=>value<=.04045?value/12.92:((value+.055)/1.055)**2.4);
  return channels[0]*.2126+channels[1]*.7152+channels[2]*.0722;
}

async function run(){
  const browser=await chromium.launch({executablePath,headless:true});
  try {
    const context=await browser.newContext({viewport:{width:1280,height:760},colorScheme:'dark'});
    const status={profile:{active:false},storage:{count:0,stored:0,revision:1},max_packets:2000,ui_language:'zh'};
    const responses={
      '/api/status':status,'/api/context-settings':{context_tokens:null,compact_tokens:null,max_context_tokens:500000},
      '/api/packet-filters':{statuses:[],kinds:[],threads:[]},'/api/packets':[],
      '/api/session-summaries':{state:'available',sessions:[]},'/api/connections':{targets:[]},'/api/updates':{state:'idle'}
    };
    let saved='light',rejectSave=false,olderDesktop=false;
    const writes=[];
    await context.route('http://appearance.test/**',async route=>{
      const request=route.request(),url=new URL(request.url()),endpoint=url.pathname;
      if(request.method()==='POST'){
        const data=request.postDataJSON();writes.push([endpoint,data]);
        if(rejectSave)return route.fulfill({status:500,json:{error:'界面主题未保存，请重试。'}});
        saved=data.theme;return route.fulfill({json:{theme:saved}});
      }
      if(endpoint.startsWith('/api/'))return route.fulfill({json:responses[endpoint]??{}});
      const file=endpoint==='/'?'index.html':endpoint.slice(1);
      const extension=path.extname(file),types={'.html':'text/html','.js':'text/javascript','.css':'text/css'};
      if(!types[extension])return route.fulfill({status:404});
      let body=fs.readFileSync(path.join(assets,file),'utf8');
      if(extension==='.html'&&!olderDesktop){
        body=body.replace('name="color-scheme" content="light"',`name="color-scheme" content="${saved==='system'?'light dark':saved}" data-saved-theme="${saved}"`);
      }
      return route.fulfill({contentType:types[extension]+'; charset=utf-8',body});
    });

    const page=await context.newPage(),tray=await context.newPage(),errors=[];
    for(const view of [page,tray])view.on('pageerror',error=>errors.push(error.message));
    await Promise.all([page.goto('http://appearance.test/'),tray.goto('http://appearance.test/tray.html')]);
    assert.equal(await scheme(page),'light','Existing installs remain light even on a dark OS');
    const open=async()=>{
      await page.locator('#settings-open').click();
      await page.locator('#settings-dialog').waitFor({state:'visible'});
    };
    const close=()=>page.locator('#settings-close').click();
    const choice=page.locator('#ui-theme'),dark=page.locator('#ui-theme-dark'),light=page.locator('#ui-theme-light');
    const saveButton=page.locator('#settings-save'),save=()=>saveButton.click();
    const waitForTheme=(view,dark)=>view.waitForFunction(dark=>(getComputedStyle(document.documentElement).colorScheme==='dark')===dark,dark);
    await open();
    assert.equal(await choice.evaluate(node=>node.closest('.settings-row').previousElementSibling.dataset.settingsField),'language');
    const group=page.getByRole('group',{name:'界面主题'});
    assert.equal(await group.getByRole('radio').count(),2);
    assert.equal(await group.getByRole('radio',{name:'浅色'}).isChecked(),true);
    await light.press('ArrowLeft');
    assert.equal(await dark.isChecked(),true,'The arrow key changes the selected theme');
    assert.equal(await dark.evaluate(input=>getComputedStyle(input.nextElementSibling).outlineStyle),'solid');
    assert.equal(await scheme(page),'dark','Changing the selection previews the new theme');
    assert.equal(await scheme(tray),'light','A preview does not change the tray');
    assert.equal(await page.evaluate(()=>uiAppearance.theme),'light');
    await page.locator('#settings-restore').click();
    assert.equal(await light.isChecked(),true);assert.equal(await scheme(page),'light');
    await dark.click();await close();await open();
    assert.equal(await light.isChecked(),true);assert.equal(await scheme(page),'light');assert.deepEqual(writes,[]);

    await dark.click();await save();
    await waitForTheme(tray,true);
    assert.deepEqual(writes,[['/api/appearance',{theme:'dark'}]]);
    for(const [view,selector] of [
      [page,'.app-header'],[page,'.sessions-sidebar'],[page,'.requests-sidebar'],[page,'.detail'],[page,'.app-footer'],
      [page,'.trend-charts'],[page,'.sharing-section:first-child'],[page,'#settings-dialog'],[page,'#ui-theme input:checked+span'],[tray,'.tray-panel']
    ]){
      const {background,text}=await styles(view,selector),luminanceBG=luminance(background);
      assert(!background.startsWith('rgba')&&luminanceBG<.1,`${selector} uses a dark surface (${background})`);
      assert((luminance(text)+.05)/(luminanceBG+.05)>=4.5,`${selector} text contrasts with the dark surface`);
    }
    const button=await styles(page,'#settings-save');
    assert((luminance(button.background)+.05)/(luminance(button.text)+.05)>=4.5,'Filled buttons use a readable foreground');
    await page.emulateMedia({colorScheme:'light'});
    assert.equal(await scheme(page),'dark','An explicit choice does not follow OS changes');
    await close();await page.reload();await open();
    assert.equal(await dark.isChecked(),true);
    await page.evaluate(()=>uiI18n.setLanguage('en',{persist:false}));
    const englishGroup=page.getByRole('group',{name:'Appearance'});
    assert.equal(await englishGroup.getByRole('radio',{name:'Dark'}).isChecked(),true);
    assert.equal(await englishGroup.getByRole('radio',{name:'Light'}).isChecked(),false);
    await page.emulateMedia({forcedColors:'active'});
    assert.equal(await dark.evaluate(input=>getComputedStyle(input.nextElementSibling).forcedColorAdjust),'none');
    await page.emulateMedia({forcedColors:'none'});

    saved='system';
    await close();await page.reload();await open();
    await waitForTheme(page,false);
    assert.equal(await light.isChecked(),true);
    assert.equal(await saveButton.isDisabled(),true,'An existing system preference is preserved until a choice is made');
    await page.emulateMedia({colorScheme:'dark'});await waitForTheme(page,true);
    assert.equal(await dark.isChecked(),true);
    await page.emulateMedia({colorScheme:'light'});await waitForTheme(page,false);
    rejectSave=true;
    await dark.click();await save();
    await page.waitForFunction(()=>document.querySelector('#settings-message').classList.contains('error'));
    assert.equal(await page.evaluate(()=>uiAppearance.theme),'system','A failed write keeps the saved choice');
    await close();assert.equal(await scheme(page),'light','Closing after a failed write discards the preview');

    rejectSave=false;await open();await light.click();await save();
    await page.waitForFunction(()=>uiAppearance.theme==='light'&&!settingsSaving);
    assert.deepEqual(writes.at(-1),['/api/appearance',{theme:'light'}],'Clicking the current system color chooses a fixed theme');
    await page.emulateMedia({colorScheme:'dark'});
    assert.equal(await scheme(page),'light');

    olderDesktop=true;
    await page.goto('http://appearance.test/');await open();
    const count=writes.length;
    await dark.click();await save();
    await page.waitForFunction(()=>uiAppearance.theme==='dark'&&!settingsSaving);
    await page.reload();await waitForTheme(page,true);
    assert.equal(writes.length,count,'A hot reload under an older desktop can use local storage');
    assert.deepEqual(errors,[]);
    console.log('Appearance: segmented controls, keyboard preview, saving, tray synchronization, legacy preferences and contrast passed.');
  } finally {await browser.close();}
}

run().catch(error=>{console.error(error);process.exitCode=1;});
