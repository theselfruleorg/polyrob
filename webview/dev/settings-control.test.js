import { describe, it, expect, vi } from 'vitest';
import { groupSave, settingControl } from '../static/app/settings-control.js';
const copy = { read_only:'0', control_saved:'Saved', control_queued:'Removal queued for review:', control_save:'Save', control_confirm:'Confirm', control_cancel:'Cancel', control_confirmation:'Review', unreachable:'Unavailable' };
const reply = body => ({ok:true,status:200,json:async()=>body});
const flush = () => new Promise(resolve => setTimeout(resolve, 0));
function button(root, text) { return [...root.querySelectorAll('button')].find(b=>b.textContent===text); }
describe('real controls', () => {
 it('patches a boolean preference and renders the effective server response', async () => {
  const fetcher=vi.fn(async()=>reply({ok:true,key:'digest.enabled',value:false,source:'pref'}));
  const root=settingControl({key:'digest.enabled',type:'bool',value:true},copy,{fetcher});
  root.querySelector('[role=switch]').click(); button(root,'Save').click(); await flush();
  expect(fetcher.mock.calls[0][0]).toBe('/api/webgate/preferences');
  expect(fetcher.mock.calls[0][1].method).toBe('PATCH');
  expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({key:'digest.enabled',value:false});
  expect(root.querySelector('[role=status]').textContent).toBe('Saved');
 });
 it('requires explicit guarded confirmation and preserves queued response', async () => {
  const fetcher=vi.fn(async()=>({ ...reply({ok:true,queued:[{entry:'send'}],value:['send']}),status:202 }));
  const root=settingControl({key:'approvals.deny',type:'list',value:['send'],sensitivity:'guarded'},copy,{fetcher});
  root.querySelector('textarea').value=''; button(root,'Save').click();
  expect(fetcher).not.toHaveBeenCalled(); button(root,'Confirm').click(); await flush();
  expect(JSON.parse(fetcher.mock.calls[0][1].body).confirm).toBe(true);
  expect(root.querySelector('[role=status]').textContent).toContain('queued');
  expect(root.querySelector('textarea').value).toBe('send');
 });
 it('patches writable flags with the right route and displays refusal verbatim', async () => {
  const fetcher=vi.fn(async()=>({ok:false,status:403,json:async()=>({error:'operator refused'})}));
  const root=settingControl({name:'MEMORY_ENABLED',type:'bool',value:false,console_writable:true},copy,{flag:true,fetcher});
  button(root,'Save').click();await flush();
  expect(fetcher.mock.calls[0][0]).toBe('/api/webgate/config/MEMORY_ENABLED');
  expect(root.querySelector('[role=status]').textContent).toBe('operator refused');
 });
 it('offers no writes when read-only, denied, or permission missing',()=>{
  for (const options of [{copy:{...copy,read_only:'1'},flag:false,spec:{}},{copy,flag:true,spec:{console_writable:false}},{copy,flag:true,spec:{}}]) {
   const root=settingControl(options.spec,options.copy,{flag:options.flag});expect(root.querySelector('button,input,select')).toBeNull();
  }
 });
 it('saves enum preference and signals theme only on server acceptance', async()=>{
  const fetcher=vi.fn(async()=>reply({ok:true,value:'light'}));
  const listener=vi.fn();document.addEventListener('console-theme',listener,{once:true});
  const root=settingControl({key:'ui.theme',type:'enum',enum_values:['auto','dark','light'],value:'auto'},copy,{fetcher});
  root.querySelector('select').value='light';button(root,'Save').click();await flush();
  expect(JSON.parse(fetcher.mock.calls[0][1].body).value).toBe('light');expect(listener.mock.calls[0][0].detail).toBe('light');
 });
 it('uses schema number bounds and sends invalid input for server validation', async()=>{
  const fetcher=vi.fn(async()=>({ok:false,status:400,json:async()=>({error:'invalid number'})}));
  const root=settingControl({key:'goals.daily_quota',type:'int',min:1,max:100,value:10},copy,{fetcher});
  const input=root.querySelector('input');expect(input.min).toBe('1');expect(input.max).toBe('100');input.value='';button(root,'Save').click();await flush();
  expect(root.querySelector('[role=status]').textContent).toBe('invalid number');
 });
});

it('keeps an unknown boolean unselected until explicitly chosen', async () => {
 const fetcher=vi.fn(async()=>reply({ok:true,value:false}));
 const root=settingControl({key:'unknown',type:'bool',value:null},copy,{fetcher});
 expect(root.querySelector('[role=switch]')).toBeNull();
 button(root,'Save').click(); await flush(); expect(fetcher).not.toHaveBeenCalled();
 root.querySelector('select').value='false'; button(root,'Save').click(); await flush();
 expect(JSON.parse(fetcher.mock.calls[0][1].body).value).toBe(false);
});

it('updates the displayed source from the accepted server answer', async () => {
 const row=document.createElement('tr'); row.innerHTML='<td data-source>default</td><td data-editor><span data-effective>true</span></td>';
 const root=settingControl({key:'digest.enabled',type:'bool',value:true},copy,{fetcher:async()=>reply({ok:true,value:true,source:'pref'})});
 row.querySelector('[data-editor]').append(root); button(root,'Save').click(); await flush();
 expect(row.querySelector('[data-source]').textContent).toBe('pref');
});

it('guarded cancellation never sends a request', () => {
 const fetcher=vi.fn();
 const root=settingControl({key:'approvals.deny',type:'list',value:['send'],sensitivity:'guarded'},copy,{fetcher});
 button(root,'Save').click(); expect(button(root,'Confirm').hidden).toBe(false);
 button(root,'Cancel').click(); expect(button(root,'Confirm').hidden).toBe(true);
 expect(button(root,'Save').hidden).toBe(false); expect(fetcher).not.toHaveBeenCalled();
});

// 113 W5 item 1 (CP-1) — one Save per group.
describe('groupSave', () => {
 const gcopy = { ...copy, nothing_changed: 'Nothing changed.' };
 it('a bare control has no buttons of its own and writes nothing until changed', () => {
  const c = settingControl({key:'digest.enabled',type:'bool',value:true,label:'Daily summary'},gcopy,{bare:true});
  expect(button(c,'Save')).toBeUndefined();
  expect(c.querySelector('[role=switch]').getAttribute('aria-label')).toBe('Daily summary');
  expect(c.setting.changed()).toBe(false);
  c.querySelector('[role=switch]').click();
  expect(c.setting.changed()).toBe(true);
 });
 it('one Save writes every changed row and nothing else', async () => {
  const fetcher = vi.fn(async (url, init) => reply({ok:true, value: JSON.parse(init.body).value}));
  const a = settingControl({key:'digest.enabled',type:'bool',value:true},gcopy,{bare:true,fetcher});
  const b = settingControl({key:'goals.daily_quota',type:'int',value:10},gcopy,{bare:true,fetcher});
  const c = settingControl({key:'ui.theme',type:'enum',enum_values:['auto','dark'],value:'auto'},gcopy,{bare:true,fetcher});
  const actions = groupSave([a,b,c], gcopy);
  button(actions,'Save').click(); await flush();
  expect(fetcher).not.toHaveBeenCalled();
  expect(actions.querySelector('[role=status]').textContent).toBe('Nothing changed.');
  a.querySelector('[role=switch]').click(); b.querySelector('input').value = '12';
  button(actions,'Save').click(); await flush(); await flush();
  expect(fetcher.mock.calls.map(call => JSON.parse(call[1].body).key)).toEqual(['digest.enabled','goals.daily_quota']);
  expect(a.setting.changed()).toBe(false);
  expect(b.setting.changed()).toBe(false);
 });
 it('a guarded change asks once, then writes every change with confirm', async () => {
  const fetcher = vi.fn(async () => reply({ok:true, value:['x']}));
  const a = settingControl({key:'approvals.deny',type:'list',value:[],sensitivity:'guarded'},gcopy,{bare:true,fetcher});
  const b = settingControl({key:'goals.daily_quota',type:'int',value:10},gcopy,{bare:true,fetcher});
  const actions = groupSave([a,b], gcopy);
  a.querySelector('textarea').value = 'x'; b.querySelector('input').value = '11';
  button(actions,'Save').click();
  expect(fetcher).not.toHaveBeenCalled();
  expect(button(actions,'Confirm').hidden).toBe(false);
  button(actions,'Confirm').click(); await flush(); await flush();
  expect(fetcher.mock.calls.map(call => JSON.parse(call[1].body).confirm)).toEqual([true, true]);
  expect(button(actions,'Save').hidden).toBe(false);
 });
 it('Cancel writes nothing', () => {
  const fetcher = vi.fn();
  const a = settingControl({key:'approvals.deny',type:'list',value:[],sensitivity:'guarded'},gcopy,{bare:true,fetcher});
  const actions = groupSave([a], gcopy);
  a.querySelector('textarea').value = 'x';
  button(actions,'Save').click(); button(actions,'Cancel').click();
  expect(fetcher).not.toHaveBeenCalled();
  expect(button(actions,'Confirm').hidden).toBe(true);
 });
});

// 113 W6 item 1: an empty list setting is empty, never the catalog's "empty" word.
describe('an empty list setting', () => {
 it('never puts a non-list value in the box and never posts it', async () => {
  const fetcher=vi.fn(async()=>reply({ok:true,value:[]}));
  const root=settingControl({key:'shell.deny',type:'list',value:'empty'},copy,{fetcher,bare:true});
  expect(root.querySelector('textarea').value).toBe('');
  expect(root.setting.changed()).toBe(false);
  root.querySelector('textarea').value='rm';
  expect(root.setting.changed()).toBe(true);
  await root.setting.write(true);
  expect(JSON.parse(fetcher.mock.calls[0][1].body).value).toEqual(['rm']);
 });
 it('shows an empty list as an empty box', () => {
  const root=settingControl({key:'shell.deny',type:'list',value:[]},copy,{bare:true});
  expect(root.querySelector('textarea').value).toBe('');
  expect(root.setting.changed()).toBe(false);
 });
});
