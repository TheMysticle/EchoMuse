// Run with the same pinned Babel used by dashboard_globals.test.mjs.
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const Babel = createRequire(import.meta.url)(process.env.BABEL_STANDALONE || '/tmp/babel.min.js');
const source = readFileSync(new URL('../static/dashboard.jsx', import.meta.url), 'utf8');
const component = source.slice(source.indexOf('function SchedulesTab('), source.indexOf('\nfunction Detail('));
const compiled = Babel.transform(component, {presets:['react']}).code;
const initial = {colors:{timer:[0,188,212],alarm:[255,152,0],reminder:[156,39,176]},sounds:{},snooze:540,ramp:20,ring_cap:600,retention:3600,grace:900,volume:0.5,timer_volume:0.25,alarm_volume:0.2};
const event = {preventDefault(){}};

function harness(saved = initial, capabilities = ['local_schedule','local_schedule_sounds']) {
  const snapshot = {revision:1,items:[],settings:structuredClone(saved),time_valid:true};
  const state = [], requests = [], uploads = [];
  let cursor = 0, failUpload = false;
  const context = {
    Intl, Date, Number, Object, Boolean, Error, encodeURIComponent,
    useState(value) {
      const i = cursor++;
      if (!(i in state)) state[i] = i===0?snapshot:i===1?structuredClone(saved):typeof value==='function'?value():value;
      return [state[i], next=>{state[i]=typeof next==='function'?next(state[i]):next;}];
    },
    useCallback: fn=>fn, useEffect(){},
    Panel:'Panel', Pill:'Pill', SectionLabel:'SectionLabel',
    scheduleId:()=>`request-${requests.length}`, ingressPath:path=>path,
    React:{Fragment:'Fragment',createElement:(type,props,...children)=>({type,props:{...props,children}}),cloneElement:(node,props)=>({...node,props:{...node.props,...props}})},
    API:{token:'test',async post(path,body){requests.push({path,body});return {...snapshot,revision:++snapshot.revision,settings:body.settings || snapshot.settings};}},
    async fetch(path, options) {
      uploads.push({path,...options});
      return {status:failUpload?409:200,ok:!failUpload,async json(){return failUpload?{message:'Upload failed'}:{asset:options.body.name.padEnd(64,'a').slice(0,64)};}};
    },
  };
  vm.createContext(context);
  vm.runInContext(compiled, context);
  const render = () => {cursor=0;return context.SchedulesTab({device:{device_id:'satellite-A',capabilities},isAdmin:true});};
  const nodes = root => root && typeof root==='object'?[root,...(root.props?.children || []).flat(Infinity).flatMap(nodes)]:[];
  const all = () => nodes(render());
  const control = label => all().find(node=>node.props?.['aria-label']===label);
  const select = (kind,value) => control(kind+' sound').props.onChange({target:{value}});
  const upload = (kind,file) => control('Upload '+kind+' sound').props.onChange({target:{files:[file]}});
  const submit = async index => {await all().filter(node=>node.type==='form')[index].props.onSubmit(event);};
  return {all,control,select,upload,submit,state,requests,uploads,setFailure(value){failUpload=value;}};
}

// Initial choices and new alerts use defaults; no per-item upload remains.
{
  const h = harness();
  for (const kind of ['timer','alarm','reminder']) assert.equal(h.control(kind+' sound').props.value,'default');
  assert.equal(h.all().filter(node=>node.props?.type==='file').length,0);
  await h.submit(0);
  assert.equal(h.requests[0].body.action,'create');
  assert.equal('sound' in h.requests[0].body.item,false);
  assert.equal(h.uploads.length,0);
  const prune = h.all().find(node=>node.props?.children?.includes('Remove unused cached sounds'));
  assert.equal(prune.props.type,'button'); // Pruning must never submit the settings form.
  await prune.props.onClick();
  assert.equal(h.requests[1].body.action,'prune_assets');
}

// All three selections upload first, then one settings command applies them.
{
  const h = harness();
  for (const [kind,name] of [['timer','timer.WAV'],['alarm','alarm.mp3'],['reminder','reminder.m4a']]) {
    h.select(kind,'custom');
    h.upload(kind,{name,size:512});
  }
  await h.submit(1);
  assert.equal(h.state[2],'');
  assert.equal(h.uploads.length,3);
  assert.ok(h.uploads.every(request=>request.path==='/api/devices/satellite-A/schedule-assets'));
  assert.equal(h.requests.length,1);
  const settings = h.requests[0].body.settings;
  assert.equal(settings.timer_volume,initial.timer_volume);
  assert.equal(settings.alarm_volume,initial.alarm_volume);
  for (const kind of ['timer','alarm','reminder']) assert.ok(settings.sounds[kind]);
  h.select('timer','default');
  await h.submit(1);
  assert.equal(h.requests[1].body.settings.sounds.timer,'');
  assert.equal(h.requests[1].body.settings.sounds.alarm,settings.sounds.alarm);
  assert.equal(h.uploads.length,3);
}

// Invalid or unsuccessful uploads never commit settings; retry remains possible.
for (const file of [null,{name:'bad.exe',size:1},{name:'huge.wav',size:8*1024*1024+1}]) {
  const h = harness();
  h.select('alarm','custom');
  if(file) h.upload('alarm',file);
  await h.submit(1);
  assert.ok(h.state[2]);
  assert.equal(h.requests.length,0);
  assert.equal(h.uploads.length,0);
}
{
  const h = harness({...initial,sounds:{timer:'existing'}});
  h.upload('timer',{name:'new.flac',size:512});
  assert.equal(h.all().find(node=>node.props?.children?.includes('Remove unused cached sounds')).props.disabled,true);
  h.setFailure(true);
  await h.submit(1);
  assert.equal(h.requests.length,0);
  assert.equal(h.state[1].sounds.timer,'existing');
  h.setFailure(false);
  await h.submit(1);
  assert.equal(h.requests.length,1);
}
// Older firmware keeps its settings form, with unsupported sound controls disabled.
{
  const saved = {...initial};
  delete saved.sounds;
  const h = harness(saved,['local_schedule']);
  for (const kind of ['timer','alarm','reminder']) assert.equal(h.control(kind+' sound').props.disabled,true);
  await h.submit(1);
  assert.equal('sounds' in h.requests[0].body.settings,false);
}
console.log('schedule_sounds: all simulations passed');

// Reminder repetition is a satellite-wide setting, with an additional count.
{
  const h = harness();
  assert.equal(h.control('Repeat reminders').props.checked, false);
  h.control('Repeat reminders').props.onChange({target:{checked:true}});
  h.control('Additional reminder repetitions').props.onChange({target:{value:'3'}});
  await h.submit(1);
  assert.equal(h.requests[0].body.settings.reminder_repeat, true);
  assert.equal(h.requests[0].body.settings.reminder_repeats, 3);
}
