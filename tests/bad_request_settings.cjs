/* Settings switches, dependent fields, old workers and bilingual layout. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
let chromium;
try{({chromium}=require('playwright'));}catch(_){console.log(JSON.stringify({skipped:'Playwright is not installed'}));process.exit(0);}
const edge=path.join(process.env['ProgramFiles(x86)']||'C:/Program Files (x86)','Microsoft/Edge/Application/msedge.exe');
const executablePath=process.env.CRO_TEST_BROWSER||(process.platform==='win32'&&fs.existsSync(edge)?edge:chromium.executablePath());
if(!fs.existsSync(executablePath)){console.log(JSON.stringify({skipped:'No browser engine is installed'}));process.exit(0);}
const root=path.join(__dirname,'../packet_inspector/ui'),read=name=>fs.readFileSync(path.join(root,name),'utf8'),source=read('app.js');
const html=read('index.html').replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,'').replace(/<link\b[^>]*>/g,'');
const controls=source.slice(source.indexOf('function contextMessage('),source.indexOf('let updateStatus=null'));
const protection=source.slice(source.indexOf('function fillPacketProtection()'),source.indexOf('async function applyLinkAction('));
const compressionLevels=source.slice(source.indexOf('function fillZstdLevel()'),source.indexOf('function zstdLevelPickerTarget()'));
const draft=source.slice(source.indexOf('function readSettingsDraft()'),source.indexOf('function setSettingsMessage('));
const formats=source.slice(source.indexOf('const nf = '),source.indexOf('let workspaceView='));
const storage=source.slice(source.indexOf('function renderStorageInfo()'),source.indexOf('function signed('));
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
    window.state={status:{
      bad_request_retry_enabled:false,bad_request_retry_supported:true,
      capacity_retry_supported:true,capacity_retry_mode:'native',capacity_retry_max_retries:15,capacity_retry_delay_seconds:30,
      packet_size_protection_enabled:false,budget_bytes:1000000,auto_zstd_level_enabled:false,
      zstd_level:3,image_zstd_level:11,auto_image_zstd_level:6,
    }};
    window.contextSettings=null;window.contextCustomValues=new Map();
    window.renderRecordScope=()=>{};
    window.setText=(node,value)=>node.textContent=value;
    window.el=(tag,cls,text='')=>Object.assign(document.createElement(tag),{className:cls||'',textContent:text});
    window.settingsBaseline=null;
  });
  await page.addScriptTag({content:[controls,protection,compressionLevels,draft,formats,storage].join('\n')});
  await page.evaluate(()=>{
    fillBadRequestRetry();fillCapacityRetry();fillPacketProtection();
    fillContextSettings({context_tokens:null,compact_tokens:220000,max_context_tokens:500000,model_defaults:[
      {model:'fixture',context_tokens:475000,compact_tokens:450000},
      {model:'partial',context_tokens:null,compact_tokens:12345},
    ]});
    state.status.storage={count:10000,stored:711120000};
    $('packet-limit').value='100000';$('storage-limit').value='1000';
    renderStorageInfo();renderRecordLimit($('packet-limit'));renderRecordLimit($('storage-limit'));
    settingsEditing=true;settingsBaseline=readSettingsDraft();
    $('sharing-settings').hidden=false;$('proxy-settings').hidden=false;
    $('sharing-tab-setting').hidden=false;$('sharing-tab-enabled').disabled=false;
    $('settings-dialog').showModal();
  });
  const enabled=page.locator('#bad-request-retry'),count=page.locator('#bad-request-retry-count');
  assert.equal(await enabled.isChecked(),false);assert.equal(await enabled.isDisabled(),false);
  assert.equal(await count.inputValue(),'3');assert.equal(await count.isDisabled(),true);cases++;
  const contextSwitch=page.locator('#context-custom'),context=page.locator('#context-tokens');
  const compactSwitch=page.locator('#compact-custom'),compact=page.locator('#compact-tokens');
  assert.equal(await contextSwitch.isChecked(),false);assert.equal(await context.isDisabled(),true);
  await page.locator('#context-unit').click({force:true});
  assert.equal(await contextSwitch.isChecked(),false,'A disabled input cannot enable the window override');
  await contextSwitch.press('Space');
  assert.equal(await context.isDisabled(),false);
  assert.equal(await context.evaluate(node=>node.validity.valueMissing),true);
  await context.fill('250.5');
  assert.equal(await page.evaluate(()=>readSettingsDraft().context_tokens),'250.5');
  await contextSwitch.uncheck();
  assert.equal(await context.isDisabled(),true);
  assert.equal(await page.evaluate(()=>readSettingsDraft().context_tokens),null);
  await contextSwitch.check();assert.equal(await context.inputValue(),'250.5');
  await contextSwitch.uncheck();
  assert.equal(await compactSwitch.isChecked(),true);assert.equal(await compact.inputValue(),'220');
  await compactSwitch.uncheck();assert.equal(await compact.isDisabled(),true);
  assert.equal(await page.evaluate(()=>readSettingsDraft().compact_tokens),null);
  await compactSwitch.check();assert.equal(await compact.inputValue(),'220');cases++;
  for(const [toggleId,fieldIds] of [
    ['packet-size-protection',['budget']],
    ['capacity-retry',['capacity-retry-count','capacity-retry-delay']],
  ]){
    const toggle=page.locator('#'+toggleId);
    for(const id of fieldIds)assert.equal(await page.locator('#'+id).isDisabled(),true);
    await toggle.check();
    for(const id of fieldIds){
      const field=page.locator('#'+id);
      assert.equal(await field.isDisabled(),false);
      await field.fill('');
    }
    await toggle.uncheck();
    for(const id of fieldIds)assert.equal(await page.locator('#'+id).isDisabled(),true);
    assert.deepEqual(await page.evaluate(()=>changedSettings()),[],'Disabled inputs do not replace saved values');
    cases++;
  }
  await page.evaluate(()=>{settingsEditing=false;fillPacketProtection();fillCapacityRetry();settingsEditing=true;});
  const settings=page.locator('#settings-dialog .settings-content');
  assert.equal(await settings.getByRole('switch').count(),8);
  assert.equal(await settings.getByRole('checkbox').count(),0);
  const compressionMode=settings.getByRole('group',{name:'对话压缩'});
  const manual=compressionMode.getByRole('radio',{name:'手动选择'});
  const automatic=compressionMode.getByRole('radio',{name:'自动选择'});
  const sizeProtection=settings.locator('#packet-size-protection');
  const dialogLevel=settings.locator('#zstd-level-open'),imageLevel=settings.locator('#image-zstd-level-open');
  assert.equal(await manual.isChecked(),true);
  assert.equal(await automatic.isDisabled(),true);
  await sizeProtection.check();
  await manual.press('ArrowRight');
  assert.equal(await automatic.isChecked(),true);
  assert.equal(await dialogLevel.isDisabled(),true);
  assert.equal(await imageLevel.isDisabled(),true);
  assert.equal(await settings.locator('#image-zstd-level-value').textContent(),'6级');
  assert.equal(await page.evaluate(()=>readSettingsDraft().auto_zstd_level_enabled),true);
  await automatic.press('ArrowLeft');
  assert.equal(await manual.isChecked(),true);
  assert.equal(await dialogLevel.isDisabled(),false);
  assert.equal(await imageLevel.isDisabled(),false);
  assert.equal(await settings.locator('#image-zstd-level-value').textContent(),'11级');
  await sizeProtection.uncheck();cases++;
  const interactive='input:not([type=hidden]),select,button,a[href],[tabindex]';
  assert.deepEqual(await settings.locator('.settings-row').locator(interactive).evaluateAll(nodes=>
    nodes.filter(node=>!node.closest('.settings-controls')).map(node=>node.id)),[]);
  assert.deepEqual(await settings.locator('input[type="number"]').evaluateAll(inputs=>
    inputs.filter(input=>getComputedStyle(input).appearance!=='textfield').map(input=>input.id)),[],
  'Numeric settings use an appearance without spinner controls');
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
    await page.evaluate(language=>{uiI18n.setLanguage(language,{persist:false});renderContextPlaceholders();settingsLanguage=language;settingsBaseline=readSettingsDraft();},language);
    const names=await settings.locator('.settings-key').allTextContents();
    if(language==='zh'){
      assert.deepEqual(names.filter(name=>!/^\p{Script=Han}{4}$/u.test(name)),[],'Every Chinese setting name has four characters');
    }else{
      assert.deepEqual(names.filter(name=>/\p{Script=Han}/u.test(name)),[],'Setting names are translated');
    }
    await enabled.check();assert.equal(await count.isDisabled(),false);
    await page.locator('#bad-request-retry-count-unit').click();
    assert.equal(await count.evaluate(input=>input===document.activeElement),true,'The unit focuses the number field');
    await count.fill('4');
    const value=await page.evaluate(()=>({draft:readSettingsDraft(),changed:changedSettings()}));
    assert.equal(value.draft.bad_request_retry_enabled,true);
    assert.equal(value.draft.bad_request_retry_max_retries,'4');
    assert.deepEqual(value.changed.sort(),['bad_request_retry_enabled','bad_request_retry_max_retries']);
    await count.fill('10');assert.equal(await count.evaluate(node=>node.checkValidity()),false);
    await count.fill('9');assert.equal(await count.evaluate(node=>node.checkValidity()),true);
    assert.equal(await page.getByRole('switch',{name:language==='zh'?'异常重试':'Retry request errors',exact:true}).getAttribute('id'),'bad-request-retry');
    assert.equal(await context.getAttribute('placeholder'),language==='zh'?'默认':'Default');
    assert.equal(await page.getByRole('spinbutton',{name:language==='zh'?'中转入口':'Relay entry',exact:true}).getAttribute('id'),'entry-port');
    assert.equal(await page.getByRole('spinbutton',{name:language==='zh'?'记录条数':'Request count',exact:true}).getAttribute('id'),'packet-limit');
    assert.equal(await page.getByRole('spinbutton',{name:language==='zh'?'存储容量':'Storage capacity',exact:true}).getAttribute('id'),'storage-limit');
    assert.equal(await page.locator('#budget-label').textContent(),language==='zh'?'大小上限':'Size limit');
    assert.deepEqual(await settings.locator('#context-unit,#compact-unit').allTextContents(),['k','k']);
    assert.equal(await page.locator('#packet-limit-unit').textContent(),language==='zh'?'条':'req.');
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
        ['#packet-size-protection','#budget-label','#budget'],
        ['#capacity-retry','#capacity-retry-count-label','#capacity-retry-count'],
        ['#bad-request-retry','#bad-request-retry-count-label','#bad-request-retry-count'],
      ].map(([toggle,label,input])=>[
        document.querySelector(toggle),document.querySelector(label),document.querySelector(input).closest('.settings-value-unit'),
      ].map(element=>element.getBoundingClientRect().left)));
      const [sizeLimit,...retryLimits]=columns;
      for(const limit of retryLimits)assert.deepEqual(limit,sizeLimit,'The switches, number labels and fields share columns');
      const contextRows=await settings.locator('.context-value-controls').evaluateAll(rows=>rows.map(row=>{
        const [toggle,field]=Array.from(row.children,child=>child.getBoundingClientRect());
        return {fieldLeft:field.left,aligned:toggle.right<field.left&&Math.abs(toggle.top+toggle.height/2-field.top-field.height/2)<1};
      }));
      assert(contextRows.every(row=>row.aligned&&row.fieldLeft===contextRows[0].fieldLeft),'Context fields line up next to their switches');
      const unitFields=await settings.locator('.settings-value-unit').evaluateAll(fields=>fields.map(field=>{
        const [input,unit]=field.children;
        const box=field.getBoundingClientRect(),number=input.getBoundingClientRect(),suffix=unit.getBoundingClientRect();
        const style=getComputedStyle(field),border=parseFloat(style.borderRightWidth);
        const unitIsInside=border>0&&number.width>0&&number.right<suffix.left&&suffix.top>=box.top&&suffix.bottom<=box.bottom
          &&Math.abs(box.right-suffix.right-border-parseFloat(style.paddingRight))<1;
        return {id:field.querySelector('input').id,unitIsInside};
      }));
      assert.equal(unitFields.length,8);
      for(const {id,unitIsInside} of unitFields){
        assert(unitIsInside,id+' shows its unit on the right inside the field');
      }
      const limits=await settings.locator('.record-limit-value').evaluateAll(fields=>fields.map(field=>{
        const [prefix,edit]=field.children;
        const formatted=edit.querySelector('.record-limit-formatted');
        const [row,left,right]=[field,prefix,edit].map(node=>node.getBoundingClientRect());
        return {prefix:prefix.textContent,formatted:formatted.textContent,
          fits:left.right<right.left&&right.right<=row.right+1&&formatted.scrollWidth<=formatted.clientWidth+1,
          separate:getComputedStyle(field).borderInlineEndWidth==='0px'&&parseFloat(getComputedStyle(edit).borderInlineEndWidth)>0};
      }));
      assert.deepEqual(limits,[
        {prefix:'10,000/',formatted:'100,000',fits:true,separate:true},
        {prefix:'711.12/',formatted:'1,000',fits:true,separate:true},
      ],`Only the limit and unit share a field at ${width}px in ${language}`);
      cases++;
    }
    assert.equal(await enabled.getAttribute('data-settings-field'),null,'no native restart is needed');
    await enabled.uncheck();await count.evaluate(node=>{node.value='';});
    assert.equal(await count.isDisabled(),true);
    assert.deepEqual(await page.evaluate(()=>changedSettings()),[],'an unchecked count does not leave a dirty draft');cases++;
  }
  const defaultsTrigger=page.locator('#context-defaults-open'),defaultsDialog=page.locator('#context-defaults-dialog');
  for(const [language,width] of [['zh',1120],['en',320]]){
    await page.evaluate(language=>uiI18n.setLanguage(language,{persist:false}),language);
    await page.setViewportSize({width,height:900});
    const draft=await page.evaluate(()=>readSettingsDraft());
    const recommendation=language==='zh'?'开启大小保护时，建议使用':'With size protection on, prefer';
    assert((await defaultsTrigger.locator('..').textContent()).startsWith(recommendation));
    assert.equal(await defaultsTrigger.textContent(),language==='zh'?'模型默认值':'Model defaults');
    await defaultsTrigger.click();
    assert(await defaultsDialog.evaluate(node=>node.matches(':modal')));
    assert.deepEqual(await defaultsDialog.locator('tbody tr').evaluateAll(rows=>rows.map(row=>
      Array.from(row.cells,cell=>cell.textContent))),[['fixture','475','450'],['partial','—','12.345']]);
    const close=defaultsDialog.locator('button');
    assert(await close.evaluate(node=>node===document.activeElement));
    assert(await defaultsDialog.locator('#context-defaults-scroll').evaluate(node=>node.scrollWidth<=node.clientWidth+1));
    if(language==='zh')await close.click();
    else await page.keyboard.press('Escape');
    assert.equal(await defaultsDialog.isVisible(),false);
    assert(await defaultsTrigger.evaluate(node=>node===document.activeElement));
    assert.deepEqual(await page.evaluate(()=>readSettingsDraft()),draft);
    cases++;
  }
  await page.evaluate(()=>{delete contextSettings.model_defaults;});
  await defaultsTrigger.click();
  assert.equal(await defaultsDialog.locator('#context-defaults-message').textContent(),'This relay version does not provide model defaults');
  assert.equal(await defaultsDialog.locator('table').isVisible(),false);
  await page.keyboard.press('Escape');cases++;
  const storageLimit=page.locator('#storage-limit');
  const displayedLimit=storageLimit.locator('..').locator('.record-limit-formatted');
  assert.equal(await displayedLimit.isVisible(),true);
  await storageLimit.fill('1200');
  assert.equal(await displayedLimit.isVisible(),false,'Editing shows the native numeric input');
  const outlines=await storageLimit.evaluate(input=>[
    getComputedStyle(input).outlineStyle,
    getComputedStyle(input.closest('.settings-value-unit')).outlineStyle,
  ]);
  assert.deepEqual(outlines,['none','solid'],'A focused unit field only highlights its outer frame');
  await page.evaluate(()=>{
    renderRecordLimit($('storage-limit'));
    state.status.storage={count:10001,stored:1711120000};renderStorageInfo();
  });
  assert.equal(await storageLimit.inputValue(),'1200','A storage refresh preserves the edit');
  await storageLimit.blur();
  assert.equal(await displayedLimit.isVisible(),true);
  assert.equal(await displayedLimit.textContent(),'1,200');
  assert.equal(await page.locator('#storage-limit-usage').textContent(),'1,711.12');
  assert.equal(await page.evaluate(()=>readSettingsDraft().max_storage_mb),'1200');cases++;
  await page.evaluate(()=>{state.status.bad_request_retry_supported=false;fillBadRequestRetry();});
  assert.equal(await enabled.isDisabled(),true);assert.equal(await count.isDisabled(),true);cases++;
  await page.evaluate(()=>{settingsEditing=false;state.status={bad_request_retry_supported:true,bad_request_retry_enabled:true,bad_request_retry_max_retries:7};fillBadRequestRetry();});
  assert.equal(await enabled.isChecked(),true);assert.equal(await count.inputValue(),'7');assert.equal(await count.isDisabled(),false);cases++;
  console.log(JSON.stringify({bad_request_settings:'passed',browser_cases:cases}));
}
run().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();});
