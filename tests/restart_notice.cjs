/* Isolated UI contracts: persistence, accurate actions, bilingual fixed layout. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
let chromium;
try{({chromium}=require('playwright'));}catch(_){console.log(JSON.stringify({skipped:'Playwright is not installed'}));process.exit(0);}
const edge=path.join(process.env['ProgramFiles(x86)']||'C:/Program Files (x86)','Microsoft/Edge/Application/msedge.exe');
const executablePath=process.env.CRO_TEST_BROWSER||(process.platform==='win32'&&fs.existsSync(edge)?edge:chromium.executablePath());
if(!fs.existsSync(executablePath)){console.log(JSON.stringify({skipped:'No browser engine is installed'}));process.exit(0);}
const root=path.join(__dirname,'../packet_inspector/ui'),read=name=>fs.readFileSync(path.join(root,name),'utf8');
const app=read('app.js'),moduleSource=app.slice(app.indexOf('globalThis.InspectorRestartNotice = {'),app.indexOf('\nconst $ ='));
const resumeSource=app.slice(app.indexOf('function connectionResumeBusy()'),app.indexOf('function renderConnectPrompt()'));
const html=read('index.html').replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,'').replace(/<link\b[^>]*>/g,'');
let browser,cases=0;
const rects=(page,ids)=>page.evaluate(ids=>ids.map(id=>{const r=document.getElementById(id).getBoundingClientRect();return [r.x,r.y,r.width,r.height];}),ids);
async function load(page,initial={}) {
  await page.goto('http://restart.test/');
  await page.addStyleTag({content:read('style.css')});
  await page.addScriptTag({content:read('i18n.js')});
  await page.addScriptTag({content:moduleSource});
  await page.evaluate(initial=>{
    window.t=value=>uiI18n.t(value);window.calls=[];window.noticeBusy=false;window.requestFailure='';
    window.statusData={data_dir:'test',profile:{active:true,config_path:'config.toml'},entry_port:{pending:false,current_port:47844,configured_port:47844},...initial};
    window.state={status:statusData};window.$=id=>document.getElementById(id);
    window.connectionResumeSeen='';window.connectionResumeRetrying=false;window.connectionResumeError='';
    window.settingsSaving=false;window.linkSaving='';window.renderConnectionActions=()=>{};
    window.notice=InspectorRestartNotice.create({status:()=>statusData,busy:()=>noticeBusy,
      openSettings:async()=>document.getElementById('settings-dialog').showModal(),
      request:async(path,body)=>{calls.push([path,body]);if(requestFailure)throw Error(requestFailure);
        return {revision:statusData.restart_notice.revision+1,items:statusData.restart_notice.items.filter(row=>row.target!==body.target)};}});
    notice.ingest();
    window.restartNotices=notice;
  },initial);
  await page.addScriptTag({content:resumeSource});
}
async function run() {
  browser=await chromium.launch({executablePath,headless:true});
  const context=await browser.newContext({viewport:{width:1120,height:900}}),page=await context.newPage();
  await page.route('http://restart.test/**',route=>route.fulfill({contentType:'text/html',body:html}));
  await load(page);
  const headerIds=['request-compression','settings-open'],header=await rects(page,headerIds);
  assert.equal(await page.locator('#restart-notice-open').getAttribute('data-visible'),'false');
  await page.evaluate(()=>document.getElementById('settings-dialog').showModal());
  assert.equal(await page.locator('#restart-notice').isVisible(),false,'applied settings have no status card');
  assert.equal(await page.locator('#restart-notice').evaluate(node=>node.getBoundingClientRect().height),0,'ready status reserves no blank space');
  await page.evaluate(()=>document.getElementById('settings-dialog').close());
  await page.evaluate(()=>{statusData.connection_reload_required=true;notice.ingest();});
  assert.equal(await page.locator('#restart-notice-open').getAttribute('data-visible'),'true');
  assert.deepEqual(await rects(page,headerIds),header,'pending badges reserve space');
  assert.equal(await page.locator('#connection-inline-feedback').count(),0,'raw text must not return below the connection controls');
  await page.locator('#restart-notice-open').click();
  assert.equal(await page.locator('#settings-dialog').evaluate(node=>node.open),true);
  assert.equal(await page.locator('#restart-notice').isVisible(),true,'unapplied settings reveal the status card');
  assert((await page.locator('#restart-notice-title').textContent()).includes('ChatGPT'));
  const settingsIds=['settings-dialog','restart-notice','settings-save','settings-restore','context-settings'];
  const settings=await rects(page,settingsIds);
  await page.evaluate(()=>{notice.change('context_tokens',null,250000);notice.change('compact_tokens',null,200000);});
  assert.deepEqual(await rects(page,settingsIds),settings,'adding pending settings must not move the form');
  assert.equal(await page.locator('[data-settings-field="context_tokens"]').getAttribute('data-restart-label'),'待重启');
  const top=(await rects(page,['restart-notice']))[0];
  await page.locator('.settings-content').evaluate(node=>node.scrollTop=node.scrollHeight);
  assert.deepEqual((await rects(page,['restart-notice']))[0],top,'the restart card stays above the form scroll area');cases++;

  await load(page,{connection_reload_required:true});
  assert((await page.locator('#restart-notice-items').textContent()).includes('有效上下文'));
  assert((await page.locator('#restart-notice-items').textContent()).includes('自动压缩阈值'));
  await page.evaluate(()=>notice.change('context_tokens',250000,null));
  assert(!(await page.locator('#restart-notice-items').textContent()).includes('有效上下文'));
  await page.evaluate(()=>notice.acknowledge('chatgpt'));
  await page.evaluate(()=>notice.ingest());
  assert.equal(await page.locator('#restart-notice-open').getAttribute('data-visible'),'false');
  await load(page,{connection_reload_required:true});
  assert.equal(await page.locator('#restart-notice-open').getAttribute('data-visible'),'false','legacy sticky flags stay acknowledged after refresh');cases++;
  await page.evaluate(()=>notice.connection('activate',{reload_required:true,backups:['config.toml.bak'],
    wsl:{targets:[{distribution:'Ubuntu',changed:true,active:true}]}}));
  assert((await page.locator('#restart-notice-title').textContent()).includes('ChatGPT'));
  await page.evaluate(()=>notice.acknowledge('chatgpt'));
  assert.equal(await page.locator('#restart-notice-open').textContent(),'待重新连接','old workers can report simultaneous Windows and WSL changes');cases++;

  await load(page,{data_dir:'wsl'});
  await page.evaluate(()=>notice.connection('activate',{reload_required:true,local_reload_required:false,wsl:{targets:[{distribution:'Ubuntu',active:true,changed:true}],
    warnings:[{distribution:'HCE',reason:'relay_unreachable',detail:'bash: connection refused'}]},warnings:[{provider:'<img onerror=alert(1)>',file:'custom/config.toml',reason:'unsupported_upstream',detail:'HTTPS required'}]}));
  assert((await page.locator('#restart-notice-title').textContent()).includes('WSL'));
  assert.equal(await page.locator('#restart-notice-chatgpt').evaluate(node=>node.hidden),true);
  assert.equal(await page.locator('#connection-details-list img').count(),0);
  const details=await page.locator('#connection-details-list').textContent();
  assert(details.includes('Ubuntu')&&details.includes('HTTPS required')&&details.includes('bash: connection refused'));
  await page.evaluate(()=>{notice.change('compact_tokens',null,200000);});
  await page.evaluate(()=>notice.acknowledge('wsl'));
  assert((await page.locator('#restart-notice-items').textContent()).includes('自动压缩阈值'));
  assert(!(await page.locator('#restart-notice-items').textContent()).includes('Ubuntu'));cases++;

  await load(page,{data_dir:'paths',storage_paths_pending:true,log_dir:'new-log',active_log_dir:'old-log',cache_dir:'cache',active_cache_dir:'cache'});
  assert.equal(await page.locator('#restart-notice-title').textContent(),'重启优化器后生效');
  assert.equal(await page.locator('#restart-notice-chatgpt').evaluate(node=>node.hidden),true);
  assert.equal(await page.locator('[data-settings-field="log_dir"]').getAttribute('data-restart-label'),'待重启');
  await page.evaluate(()=>{statusData.storage_paths_pending=false;notice.ingest();});
  assert.equal(await page.locator('#restart-notice-open').getAttribute('data-visible'),'false');cases++;

  await load(page,{data_dir:'resume'});
  await page.evaluate(()=>{
    const wsl={targets:[{distribution:'Ubuntu',changed:true,active:true}],warnings:[{distribution:'HCE',reason:'relay_unreachable',detail:'connection refused'}]};
    statusData.wsl_connection=wsl;statusData.connection_resume={id:'sync-1',state:'attention',result:{changed:true,local_reload_required:false,reload_required:true,wsl}};
    renderConnectionResume();notice.ingest();
  });
  assert.equal(await page.locator('dialog[open]').count(),0,'automatic synchronization must not open a result window');
  assert.equal(await page.locator('#connection-resume-dialog').count(),0);
  assert((await page.locator('#restart-notice-title').textContent()).includes('WSL'));
  await page.locator('#restart-notice-open').click();
  await page.locator('#connection-details').evaluate(node=>node.open=true);
  const syncIds=['settings-dialog','connection-resume-controls','connection-resume-retry'],syncBefore=await rects(page,syncIds);
  await page.evaluate(async()=>{
    window.api=async path=>{if(path!=='/api/connection/resume')throw Error(path);return {id:'sync-2',state:'pending'};};
    await retryConnectionResume();
  });
  assert.equal(await page.locator('#connection-resume-retry').isDisabled(),true);
  assert.deepEqual(await rects(page,syncIds),syncBefore);
  await page.evaluate(()=>{
    statusData.wsl_connection={targets:[],warnings:[]};statusData.connection_resume={id:'sync-2',state:'complete',result:{changed:false,reload_required:true}};
    renderConnectionResume();notice.ingest();
  });
  await page.evaluate(()=>notice.acknowledge('wsl'));
  assert.equal(await page.locator('#restart-notice-open').getAttribute('data-visible'),'false','old native flags must not resurrect a WSL notice');
  assert.equal(await page.locator('#restart-notice').isVisible(),false);
  const syncReady=await rects(page,syncIds);
  await page.evaluate(async()=>{
    statusData.connection_resume={id:'sync-3',state:'attention',result:{}};renderConnectionResume();
    window.api=async()=>{throw Error('Synthetic sync failure');};await retryConnectionResume();
  });
  assert.equal(await page.locator('#connection-resume-status').textContent(),'Synthetic sync failure');
  assert.deepEqual(await rects(page,syncIds),syncReady);cases++;

  await load(page,{data_dir:'backend',connection_reload_required:true,restart_notice:{revision:40,items:[{key:'entry_port',target:'chatgpt',scope:''},{key:'wsl',target:'wsl',scope:'Ubuntu'}]},
    entry_port:{pending:true,current_port:47844,configured_port:47848}});
  await page.evaluate(()=>document.getElementById('settings-dialog').showModal());
  assert((await page.locator('#restart-notice-title').textContent()).includes('先重启优化器'));
  assert.equal(await page.locator('#restart-notice-chatgpt').isDisabled(),true);
  await page.evaluate(()=>notice.acknowledge('chatgpt'));
  assert.equal(await page.evaluate(()=>calls.length),0,'port changes cannot be acknowledged before applying the port');
  await page.evaluate(()=>{statusData.entry_port.pending=false;notice.ingest();});
  assert.equal(await page.locator('#restart-notice-chatgpt').isDisabled(),false);
  await page.evaluate(()=>{requestFailure='配置又有变更，请确认最新待生效项目后重试。';});
  await page.locator('#restart-notice-chatgpt').click();
  await page.evaluate(()=>notice.ingest());
  assert((await page.locator('#restart-notice-message').textContent()).includes('配置又有变更'));
  assert.equal(await page.locator('.floating-feedback:not([hidden])').count(),0);cases++;

  for(const language of ['zh','en'])for(const width of [1120,1600]) {
    await page.setViewportSize({width,height:900});
    await page.evaluate(language=>{uiI18n.setLanguage(language,{persist:false});requestFailure='';notice.render();},language);
    const ids=['settings-dialog','restart-notice','settings-save','settings-restore'];
    const before=await rects(page,ids);
    await page.evaluate(()=>{statusData.restart_notice={revision:41,items:['connection','context_tokens','compact_tokens','capacity_retry','image_analysis_mode','entry_port','sharing'].map(key=>({key,target:'chatgpt',scope:''})).concat([{key:'wsl',target:'wsl',scope:'Ubuntu'}])};notice.ingest();});
    assert.deepEqual(await rects(page,ids),before);
    const [title,actions]=await rects(page,['restart-notice-title','restart-notice-chatgpt']);
    assert(title[0]+title[2]<=actions[0],'the title and confirmation must not overlap');
    const overflow=await page.locator('#restart-notice').evaluate(node=>node.scrollHeight>node.clientHeight);
    assert.equal(overflow,false,'card content stays within its fixed height');
    await page.locator('.settings-content').evaluate(node=>node.scrollTop=0);
    const connection=await page.locator('#connection-details').boundingBox();
    assert(connection.width>0);
    cases++;
  }
  await page.evaluate(()=>{noticeBusy=true;notice.render();});
  assert.equal(await page.locator('#restart-notice-chatgpt').isDisabled(),true);
  await page.evaluate(()=>{noticeBusy=false;notice.render();});
  await page.locator('#restart-notice-chatgpt').click();
  assert.equal(await page.locator('#restart-notice-open').getAttribute('data-visible'),'true','the separate WSL acknowledgement remains pending');
  await page.locator('#restart-notice-wsl').click();
  await page.evaluate(()=>notice.ingest());
  assert.equal(await page.locator('#restart-notice-open').getAttribute('data-visible'),'false');
  assert.equal(await page.locator('#restart-notice').getAttribute('data-state'),'ready');cases++;
  assert.equal(await page.locator('#restart-notice').isVisible(),false,'clearing every pending item hides the entire card');
  assert.equal(await page.locator('#restart-notice').evaluate(node=>node.getBoundingClientRect().height),0);
  await page.evaluate(()=>{document.getElementById('settings-dialog').close();document.documentElement.classList.add('desktop-titlebar');document.getElementById('window-controls').hidden=false;});
  for(const language of ['zh','en']) {
    await page.setViewportSize({width:1120,height:900});
    await page.evaluate(language=>{uiI18n.setLanguage(language,{persist:false});statusData.restart_notice={revision:50,items:[{key:'connection',target:'chatgpt',scope:''}]};notice.ingest();},language);
    const [views,badge,connect,settings,controls]=await rects(page,['view-toggle','restart-notice-open','request-compression','settings-open','window-controls']);
    assert(views[0]+views[2]<=badge[0]&&badge[0]+badge[2]<=connect[0]&&connect[0]+connect[2]<=settings[0]&&settings[0]+settings[2]<=controls[0],
      'header controls must fit beside the centered view switch: '+JSON.stringify({language,views,badge,connect,settings,controls}));
  }cases++;
  await load(page,{data_dir:'warning-only',restart_notice:{revision:1,items:[]},provider_warnings:[{provider:'fixture',detail:'Synthetic connection warning'}]});
  await page.locator('#restart-notice-open').click();
  assert.equal(await page.locator('#restart-notice').isVisible(),false);
  assert.equal(await page.locator('#connection-details').evaluate(node=>node.open),true,'connection details remain accessible without a pending card');
  assert.equal(await page.evaluate(()=>document.activeElement===document.querySelector('#connection-details>summary')),true);cases++;
  console.log(JSON.stringify({restart_notice:'passed',browser_cases:cases}));
}
run().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();});
