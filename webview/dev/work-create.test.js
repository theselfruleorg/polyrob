import { it, expect, vi } from 'vitest';
import { bindCreate } from '../static/app/work-create.js';
const flush = () => new Promise(resolve => setTimeout(resolve,0));
for (const kind of ['goals','cron']) it(`creates ${kind} through existing writer and preserves refused draft`, async()=>{
 const form=document.createElement('form');form.dataset.create=kind;
 form.innerHTML='<textarea name="title">A task</textarea><input name="schedule" value="every 1h"><button type="submit">Create</button><p role="status"></p>';
 const fetcher=vi.fn(async()=>({ok:false,status:400,json:async()=>({message:'Refused by owner policy'})}));
 bindCreate(form,{fetcher});form.dispatchEvent(new Event('submit',{cancelable:true}));await flush();
 expect(fetcher.mock.calls[0][0]).toBe(`/api/webgate/${kind}`);
 expect(form.querySelector('[role=status]').textContent).toBe('Refused by owner policy');
 expect(form.querySelector('textarea').value).toBe('A task');
});
it('removes a read-only form',()=>{
 const form=document.createElement('form');form.dataset.readOnly='1';document.body.append(form);bindCreate(form);expect(form.isConnected).toBe(false);
});
