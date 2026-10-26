const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('packet_inspector/ui/app.js', 'utf8');
const html = fs.readFileSync('packet_inspector/ui/index.html', 'utf8');
const group = html.match(/id="request-compression"[\s\S]*?<\/div>/)[0];
assert.deepEqual([...group.matchAll(/data-compression="([^"]+)"/g)].map(x=>x[1]), ['disconnected','connected']);
assert(!html.includes('id="header-connect"'));

function fixture(connected) {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id, {id, attrs:{}, open:false, hidden:false, textContent:'',
      setAttribute(key,value){this.attrs[key]=value;}, showModal(){this.open=true;}, close(){this.open=false;},
      replaceChildren(...values){this.children=values;}});
    return elements.get(id);
  }
  const buttons = ['disconnected','connected'].map(value=>Object.assign(element(value), {dataset:{compression:value}}));
  const calls = [];
  const context = {
    state:{status:{profile:{active:connected,can_restore:connected}}},
    compressionSaving:false,settingsSaving:false,settingsOpening:false,settingsEditing:false,
    exitSaving:false,connectSaving:false,connectChecking:false,linkSaving:'',compressionTarget:null,
    compressionRevision:0,connectionRestartNotified:false,connectSource:'header',
    $:element,t:value=>value,setText:(el,value)=>{el.textContent=value;},
    el:(tag,cls,text)=>({tag,text}),
    document:{querySelectorAll:()=>buttons},
    requestConnection:async source=>calls.push(['connect-prompt',source]),
    applyLinkAction:async action=>{calls.push(['link',action]);context.state.status.profile={active:false,can_restore:false};return {reload_required:true};},
    linkAction:async action=>{calls.push(['link',action]);context.state.status.profile={active:true,can_restore:true};return {reload_required:true};},
    renderStatus(){},renderConnectionActions(){context.renderCompressionMode();},renderConnectPrompt(){},
    message:value=>calls.push(['message',value]),
    api:async()=>{throw Error('Compression settings must not be used as a connection mode');}
  };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('function renderCompressionMode()'), source.indexOf('function renderConnectPrompt()')), context);
  vm.runInContext(source.slice(source.indexOf('async function confirmConnection()'), source.indexOf('function renderExitRequest()')), context);
  return {context,element,buttons,calls};
}

(async()=>{
  let {context,element,buttons,calls}=fixture(false);
  context.renderCompressionMode();
  assert.equal(buttons[0].attrs['aria-pressed'],'true');
  assert.equal(buttons[1].disabled,false);
  await context.requestCompressionChange('connected');
  assert.deepEqual(calls,[['connect-prompt','header']]);
  await context.confirmConnection();
  assert.equal(context.state.status.profile.active,true);
  assert.equal(element('settings-restart-dialog').open,true);
  assert.equal(context.connectionRestartNotified,true);

  ({context,element,buttons,calls}=fixture(true));
  context.renderCompressionMode();
  assert.equal(buttons[1].attrs['aria-pressed'],'true');
  assert.equal(buttons[0].disabled,false);
  context.requestCompressionChange('off');
  assert.equal(element('compression-confirm-dialog').open,false);
  context.requestCompressionChange('disconnected');
  assert.equal(element('compression-confirm-dialog').open,true);
  assert.equal(calls.length,0);
  await context.confirmCompressionChange();
  assert.deepEqual(calls.filter(call=>call[0]==='link'),[['link','restore']]);
  assert.equal(context.state.status.profile.active,false);
  assert.equal(element('compression-confirm-dialog').open,false);
  assert.equal(element('settings-restart-dialog').open,true);
  assert.equal(context.compressionSaving,false);

  ({context,buttons}=fixture(true));
  context.state.status.provider_sync_error='OSError';
  context.renderCompressionMode();
  assert.equal(buttons[0].disabled,false,'A failed background scan must not prevent disconnecting');
  context.state.status.profile.config_error='unreadable';
  context.renderCompressionMode();
  assert(buttons.every(button=>button.disabled));
  console.log('Connection controls: two states, confirmations and restart prompts passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
