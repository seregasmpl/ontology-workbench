import { useEffect } from 'react';
const owners = new Set<string>();
export function useUnsaved(owner:string, dirty:boolean) {
  useEffect(() => { if(dirty) owners.add(owner); else owners.delete(owner); return () => {owners.delete(owner);}; },[owner,dirty]);
  useEffect(() => { const listener=(event:BeforeUnloadEvent)=>{if(owners.size){event.preventDefault();event.returnValue='';}};window.addEventListener('beforeunload',listener);return()=>window.removeEventListener('beforeunload',listener);},[]);
}
export function confirmDiscard() {return !owners.size || confirm('Есть несохранённые изменения. Перейти без сохранения?');}
