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
const messageSource=app.slice(app.indexOf('function message('),app.indexOf('function fail('));
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
    window.statusData={data_dir:'test',log_dir:'C:/fixture/logs',connection_diagnostics_supported:true,profile:{active:true,config_path:'config.toml'},entry_port:{pending:false,current_port:47844,configured_port:47844},...initial};
    window.state={status:statusData};window.$=id=>document.getElementById(id);
    window.connectionResumeSeen='';window.connectionProblemKey='';
    window.settingsSaving=false;window.linkSaving='';window.renderConnectionActions=()=>{};
    window.notice=InspectorRestartNotice.create({status:()=>statusData,busy:()=>noticeBusy,
      showConnectionProblem:()=>renderConnectionResume(true),
      openSettings:async()=>document.getElementById('settings-dialog').showModal(),
      request:async(path,body)=>{calls.push([path,body]);if(requestFailure)throw Error(requestFailure);
        return {revision:statusData.restart_notice.revision+1,items:statusData.restart_notice.items.filter(row=>row.target!==body.target)};}});
    notice.ingest();
    window.restartNotices=notice;
  },initial);
  await page.addScriptTag({content:messageSource+'\n'+resumeSource});
  await page.evaluate(()=>{window.feedback=uiI18n.floatingFeedback();renderConnectionResume();});
}
async function run() {
  browser=await chromium.launch({executablePath,headless:true});
  const context=await browser.newContext({viewport:{width:1120,height:900}}),page=await context.newPage();
  await page.route('http://restart.test/**',route=>route.fulfill({contentType:'text/html',body:html}));
  await load(page);
  const headerIds=['request-compression','settings-open'],header=await rects(page,headerIds);
  assert.equal(await page.locator('#restart-notice-open').getAttribute('data-visible'),'false');
  assert.equal(await page.locator('#connection-details').count(),0,'settings contain no connection details section');
  assert.equal(await page.locator('#notice').evaluate(node=>node.hidden),true,'a healthy connection stays quiet');
  await page.evaluate(()=>document.getElementById('settings-dialog').showModal());
  assert.equal(await page.locator('#restart-notice').isVisible(),false,'applied settings have no status card');
  assert.equal(await page.locator('#restart-notice').evaluate(node=>node.getBoundingClientRect().height),0,'ready status reserves no blank space');
  await page.evaluate(()=>document.getElementById('settings-dialog').close());
  await page.evaluate(()=>{statusData.connection_reload_required=true;notice.ingest();});
  assert.equal(await page.locator('#restart-notice-open').getAttribute('data-visible'),'true');
  assert.equal(await page.locator('#restart-notice-open').textContent(),'ChatGPT 待重启');
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
  assert.equal(await page.locator('[data-settings-field="context_tokens"]').getAttribute('data-restart-label'),'ChatGPT 待重启');
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
  assert.equal(await page.locator('#restart-notice-open').textContent(),'WSL 待重新连接','old workers can report simultaneous Windows and WSL changes');cases++;

  await load(page,{data_dir:'wsl'});
  await page.evaluate(()=>{
    const result={reload_required:true,local_reload_required:false,wsl:{targets:[{distribution:'Ubuntu',active:true,changed:true}],
      warnings:[{distribution:'HCE',reason:'relay_unreachable',detail:'bash: connection refused'}]},warnings:[{provider:'<img onerror=alert(1)>',file:'custom/config.toml',reason:'unsupported_upstream',detail:'HTTPS required'}]};
    statusData.wsl_connection=result.wsl;statusData.provider_warnings=result.warnings;notice.connection('activate',result);renderConnectionResume();
  });
  assert((await page.locator('#restart-notice-title').textContent()).includes('WSL'));
  assert.equal(await page.locator('#restart-notice-chatgpt').evaluate(node=>node.hidden),true);
  assert.equal(await page.locator('#app-inline-feedback img').count(),0);
  assert((await page.locator('#app-inline-feedback').textContent()).includes('HTTPS required'));
  const problems=await page.evaluate(()=>InspectorRestartNotice.connectionProblems(statusData));
  assert.equal(problems.length,2);assert(problems.some(row=>row.detail.includes('connection refused')));
  await page.evaluate(()=>{notice.change('compact_tokens',null,200000);});
  await page.evaluate(()=>notice.acknowledge('wsl'));
  assert((await page.locator('#restart-notice-items').textContent()).includes('自动压缩阈值'));
  assert(!(await page.locator('#restart-notice-items').textContent()).includes('Ubuntu'));cases++;

  await load(page,{data_dir:'paths',storage_paths_pending:true,log_dir:'new-log',active_log_dir:'old-log',cache_dir:'cache',active_cache_dir:'cache'});
  assert.equal(await page.locator('#restart-notice-open').textContent(),'本软件待重启');
  assert.equal(await page.locator('#restart-notice-title').textContent(),'重启本软件后生效');
  assert.equal(await page.locator('#restart-notice-chatgpt').evaluate(node=>node.hidden),true);
  assert.equal(await page.locator('[data-settings-field="log_dir"]').getAttribute('data-restart-label'),'本软件待重启');
  await page.evaluate(()=>notice.change('compact_tokens',null,250000));
  assert.equal(await page.locator('#restart-notice-open').textContent(),'ChatGPT 与本软件待重启');
  assert.equal(await page.locator('#restart-notice-title').textContent(),'重启本软件与 ChatGPT App 后生效');
  await page.evaluate(()=>notice.change('compact_tokens',250000,null));
  assert.equal(await page.locator('#restart-notice-open').textContent(),'本软件待重启');
  await page.evaluate(()=>{statusData.storage_paths_pending=false;notice.ingest();});
  assert.equal(await page.locator('#restart-notice-open').getAttribute('data-visible'),'false');cases++;

  await load(page,{data_dir:'resume'});
  await page.evaluate(()=>{
    const wsl={targets:[{distribution:'Ubuntu',changed:true,active:true}],warnings:[{distribution:'HCE',reason:'relay_unreachable',detail:'connection refused'}]};
    statusData.wsl_connection=wsl;statusData.connection_resume={id:'sync-1',state:'recovering',attempt:1,max_attempts:3,result:{changed:true,local_reload_required:false,reload_required:true,wsl}};
    renderConnectionResume();notice.ingest();
  });
  assert.equal(await page.locator('#notice').evaluate(node=>node.hidden),true,'automatic recovery uses no extra notification');
  await page.evaluate(()=>{statusData.connection_resume.state='attention';statusData.connection_resume.attempt=3;renderConnectionResume();notice.ingest();});
  assert.equal(await page.locator('dialog[open]').count(),0,'automatic synchronization must not open a result window');
  assert.equal(await page.locator('#connection-resume-dialog').count(),0);
  assert((await page.locator('#restart-notice-title').textContent()).includes('WSL'));
  assert((await page.locator('#app-inline-feedback').textContent()).includes('WSL'));
  assert((await page.locator('#app-inline-feedback').textContent()).includes('C:/fixture/logs'));
  assert.equal(await page.locator('#app-inline-feedback a[download]').isVisible(),true);
  await page.evaluate(()=>{
    statusData.wsl_connection={targets:[],warnings:[]};statusData.connection_resume={id:'sync-2',state:'complete',result:{changed:false,reload_required:true}};
    renderConnectionResume();notice.ingest();
  });
  await page.evaluate(()=>notice.acknowledge('wsl'));
  assert.equal(await page.locator('#restart-notice-open').getAttribute('data-visible'),'false','old native flags must not resurrect a WSL notice');
  assert.equal(await page.locator('#restart-notice').isVisible(),false);
  assert.equal(await page.locator('#notice').evaluate(node=>node.hidden),true,'successful recovery clears its own notification');
  const syncIds=['records-view','app-inline-feedback'],syncReady=await rects(page,syncIds);
  await page.evaluate(()=>{
    statusData.connection_resume={id:'sync-3',state:'attention',attempt:3,error:'Synthetic sync failure',result:{}};renderConnectionResume();notice.ingest();
  });
  assert((await page.locator('#app-inline-feedback').textContent()).includes('Synthetic sync failure'));
  assert.equal(await page.locator('dialog[open]').count(),0,'errors reuse the existing footer without a window');
  assert.deepEqual(await rects(page,syncIds),syncReady);cases++;

  await load(page,{data_dir:'backend',connection_reload_required:true,restart_notice:{revision:40,items:[{key:'entry_port',target:'chatgpt',scope:''},{key:'wsl',target:'wsl',scope:'Ubuntu'}]},
    entry_port:{pending:true,current_port:47844,configured_port:47848}});
  await page.evaluate(()=>document.getElementById('settings-dialog').showModal());
  assert.equal(await page.locator('#restart-notice-open').textContent(),'ChatGPT 与本软件待重启');
  assert((await page.locator('#restart-notice-title').textContent()).includes('先重启本软件'));
  assert.equal(await page.locator('#restart-notice-chatgpt').isDisabled(),true);
  await page.evaluate(()=>notice.acknowledge('chatgpt'));
  assert.equal(await page.evaluate(()=>calls.length),0,'port changes cannot be acknowledged before applying the port');
  await page.evaluate(()=>{statusData.entry_port.pending=false;notice.ingest();});
  assert.equal(await page.locator('#restart-notice-open').textContent(),'ChatGPT 待重启');
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
    const connection=await page.locator('#connection-settings').boundingBox();
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
  const pendingCases=[
    {name:'chatgpt-target',status:{restart_notice:{revision:1,items:[{key:'connection',target:'chatgpt',scope:''}]}},
      zh:'ChatGPT 待重启',en:'ChatGPT needs a restart'},
    {name:'optimizer-target',status:{storage_paths_pending:true,cache_dir:'new-cache',active_cache_dir:'old-cache'},
      zh:'本软件待重启',en:'This app needs a restart'},
    {name:'both-targets',status:{restart_notice:{revision:1,items:[{key:'context_tokens',target:'chatgpt',scope:''}]},
      storage_paths_pending:true,cache_dir:'new-cache',active_cache_dir:'old-cache'},
      zh:'ChatGPT 与本软件待重启',en:'ChatGPT + this app: restart'},
    {name:'port-targets',status:{restart_notice:{revision:1,items:[{key:'entry_port',target:'chatgpt',scope:''}]},
      entry_port:{pending:true,current_port:47844,configured_port:47848}},
      zh:'ChatGPT 与本软件待重启',en:'ChatGPT + this app: restart'},
    {name:'legacy-wsl-target',status:{restart_notice:{revision:1,items:[{key:'wsl',target:'wsl',scope:'Ubuntu'}]}},
      zh:'WSL 待重新连接',en:'WSL reconnect pending'},
  ];
  for(const example of pendingCases) {
    await load(page,{data_dir:example.name,...example.status});
    await page.evaluate(()=>{document.documentElement.classList.add('desktop-titlebar');document.getElementById('window-controls').hidden=false;});
    for(const language of ['zh','en']) {
      await page.evaluate(language=>{uiI18n.setLanguage(language,{persist:false});notice.render();},language);
      assert.equal(await page.locator('#restart-notice-open').textContent(),example[language]);
      assert((await page.locator('#restart-notice-open').getAttribute('aria-label')).startsWith(example[language]));
      assert.equal(await page.locator('#restart-notice-open').evaluate(node=>node.scrollWidth>node.clientWidth),false,
        'Restart targets must fit in the badge: '+example.name+' / '+language);
      const [views,badge,connect]=await rects(page,['view-toggle','restart-notice-open','request-compression']);
      assert(views[0]+views[2]<=badge[0]&&badge[0]+badge[2]<=connect[0],'Named targets must fit the header');
      const [header]=await page.locator('.app-header').evaluate(node=>{const box=node.getBoundingClientRect();return [[box.y,box.height]];});
      assert(badge[1]>=header[0]&&badge[1]+badge[3]<=header[0]+header[1],'Wrapped restart targets must stay inside the title bar');
      await page.locator('#restart-notice-open').click();
      assert.equal(await page.locator('#settings-dialog').evaluate(node=>node.open),true);
      assert.equal(await page.evaluate(()=>calls.length),0,'Opening the reminder must not restart or acknowledge either app');
      assert.equal(await page.locator('#restart-notice-chatgpt').textContent(),language==='zh'?'ChatGPT 已重启':'ChatGPT restarted');
      assert.equal(await page.locator('#restart-notice-wsl').textContent(),language==='zh'?'WSL 已重新连接':'WSL reconnected');
      await page.evaluate(()=>document.getElementById('settings-dialog').close());
      cases++;
    }
  }
  await load(page,{data_dir:'automatic-restart',connection_reload_required:true,restart_notice:{revision:1,auto_detect_supported:true,
    items:[{key:'image_analysis_mode',target:'chatgpt',scope:''},{key:'wsl',target:'wsl',scope:'Ubuntu'}]}});
  await page.locator('#restart-notice-open').click();
  for(const language of ['zh','en']) {
    await page.evaluate(language=>{uiI18n.setLanguage(language,{persist:false});notice.render();},language);
    const message=await page.locator('#restart-notice-message').textContent();
    assert(message.includes(language==='zh'?'自动清除':'automatically')&&message.includes('WSL'));
    assert.equal(await page.locator('#restart-notice').evaluate(node=>node.scrollHeight>node.clientHeight),false);
    cases++;
  }
  await page.evaluate(()=>{statusData.restart_notice={revision:2,auto_detect_supported:true,items:[{key:'wsl',target:'wsl',scope:'Ubuntu'}]};notice.ingest();});
  assert.equal(await page.locator('#restart-notice-chatgpt').evaluate(node=>node.hidden),true);
  assert.equal(await page.locator('#restart-notice-wsl').isVisible(),true);
  await page.evaluate(()=>{statusData.restart_notice={revision:3,auto_detect_supported:true,items:[]};notice.ingest();});
  assert.equal(await page.locator('#restart-notice-open').getAttribute('data-visible'),'false','backend restart detection removes the badge despite legacy sticky flags');
  assert.equal(await page.locator('#restart-notice').isVisible(),false);
  assert.equal(await page.evaluate(()=>calls.length),0,'automatic refresh must not send a manual acknowledgement');cases++;

  await load(page,{data_dir:'warning-only',restart_notice:{revision:1,items:[]},provider_warnings:[{provider:'fixture',detail:'Synthetic connection warning'}]});
  await page.locator('#restart-notice-open').click();
  assert.equal(await page.locator('#restart-notice').isVisible(),false);
  assert.equal(await page.locator('dialog[open]').count(),0,'the warning badge does not open settings');
  assert((await page.locator('#app-inline-feedback').textContent()).includes('Synthetic connection warning'));
  for(const language of ['zh','en']) {
    await page.evaluate(language=>{uiI18n.setLanguage(language,{persist:false});renderConnectionResume(true);},language);
    const [text,link]=await page.locator('#app-inline-feedback').evaluate(node=>[node.querySelector('span').getBoundingClientRect().right,node.querySelector('a').getBoundingClientRect().left]);
    assert(text<=link,'the diagnostic link stays beside the existing message');
    assert.equal(await page.locator('#app-inline-feedback').evaluate(node=>node.scrollHeight>node.clientHeight),false);
    cases++;
  }
  await page.route('http://restart.test/api/connection-diagnostics?download=1',route=>route.fulfill({contentType:'application/json',headers:{'Content-Disposition':'attachment; filename="codex-connection-diagnostics.json"'},body:'{"connection":{"state":"attention"},"log_directory":"C:/fixture/logs"}'}));
  const download=page.waitForEvent('download');await page.locator('#app-inline-feedback a[download]').click();
  assert.equal((await download).suggestedFilename(),'codex-connection-diagnostics.json');cases++;
  await page.evaluate(()=>{statusData.provider_warnings=[];renderConnectionResume();notice.ingest();});
  assert.equal(await page.locator('#notice').evaluate(node=>node.hidden),true);
  assert.equal(await page.locator('#restart-notice-open').getAttribute('data-visible'),'false');cases++;
  console.log(JSON.stringify({restart_notice:'passed',browser_cases:cases}));
}
run().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();});
