import { useCallback, useEffect, useState } from 'react';
import { BookOpen, Boxes, CheckCheck, Database, Download, GitBranch, Layers3, Play, Plus, Power, Settings2, X } from 'lucide-react';
import { api, dataDirectory, download, initialize } from './api';
import type { Profile, Version } from './types';
import { OntologyEditor } from './OntologyEditor';
import { Annotator } from './Annotator';
import { DataImport } from './DataImport';
import { Connections } from './Connections';
import { Runs } from './Runs';
import { confirmDiscard } from './unsaved';

export type WorkProps = { pid: string; version: Version | null; profiles: Profile[]; refresh: (versionId?: string) => Promise<void>; run: (action: () => Promise<void>) => Promise<void> };

const tabs = [
  ['ontology', 'Онтология', Boxes], ['annotation', 'Примеры и разметка', CheckCheck],
  ['data', 'Данные', Database], ['versions', 'Версии', GitBranch], ['runs', 'Запуски', Play], ['connections', 'Подключения', Settings2],
] as const;

export function App() {
  const [ready, setReady] = useState(false);
  const [projects, setProjects] = useState<any[]>([]);
  const [pid, setPid] = useState('');
  const [project, setProject] = useState<any>(null);
  const [version, setVersion] = useState<Version | null>(null);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [tab, setTab] = useState('ontology');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(0);
  const [newName, setNewName] = useState('');
  const [newOpen, setNewOpen] = useState(false);
  const run = useCallback(async (action: () => Promise<void>) => {
    setBusy(b => b + 1); setError('');
    try { await action(); } catch (e) { setError(e instanceof Error ? e.message : 'Не удалось выполнить действие'); }
    finally { setBusy(b => b - 1); }
  }, []);
  useEffect(() => { void run(async () => {
    await initialize(); const ps = await api<any[]>('/projects'); setProjects(ps);
    setProfiles(await api('/profiles')); setPid(ps[0]?.id || ''); setReady(true);
  }); }, [run]);
  const refresh = useCallback(async (versionId?: string) => {
    setProfiles(await api('/profiles'));
    if (!pid) return;
    const p = await api('/projects/' + pid); setProject(p);
    const selected = versionId || (version?.project_id === pid ? version.id : p.versions[0]?.id);
    setVersion(selected ? await api('/versions/' + selected) : null);
  }, [pid, version?.id, version?.project_id]);
  useEffect(() => { if (pid) void run(async () => {
    const p = await api('/projects/' + pid); setProject(p);
    setVersion(p.versions[0] ? await api('/versions/' + p.versions[0].id) : null);
  }); }, [pid, run]);
  const work: WorkProps = { pid, version, profiles, refresh, run };
  async function createProject() {
    if(!confirmDiscard()) return;
    const p = await api('/projects', 'POST', { name: newName });
    setProjects(await api('/projects')); setPid(p.id); setNewOpen(false); setNewName(''); setTab('ontology');
  }
  const total = project?.counts?.reduce((n: number, row: any) => n + row.count, 0) || 0;
  return <div className="app-shell">
    <aside className="sidebar">
      <a className="brand" href="#" onClick={e => { e.preventDefault(); if(confirmDiscard()) setTab('ontology'); }}><span className="brand-mark"><Layers3 size={24}/></span><span>Онтологии<small>рабочая мастерская</small></span></a>
      <div className="project-box"><label htmlFor="project-select">ПРОЕКТ</label>
        <select id="project-select" value={pid} onChange={e => { if(confirmDiscard()) setPid(e.target.value); }}><option value="" disabled>Выберите проект</option>{projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select>
        <button className="text-button" onClick={() => setNewOpen(true)}><Plus size={15}/> Новый проект</button>
      </div>
      <nav aria-label="Разделы приложения">{tabs.map(([key, title, Icon], index) => <button key={key} className={tab === key ? 'active' : ''} onClick={() => { if(confirmDiscard()) setTab(key); }}><Icon size={19}/><span>{title}</span><small>{index + 1}</small></button>)}</nav>
      <div className="sidebar-bottom"><div className="local-dot">Локальное приложение</div><p>Файлы остаются на компьютере. Запросы к модели — только после подтверждения.</p>
        <button className="text-button" onClick={() => void run(async () => { if (confirm('Остановить приложение? Текущая работа сохранена. Незавершённый запрос можно будет повторить.')) { await api('/shutdown', 'POST', {}); setError('Приложение остановлено. Эту вкладку можно закрыть.'); } })}><Power size={14}/> Завершить работу</button>
      </div>
    </aside>
    <main>
      <header className="topbar"><div className="breadcrumb">Мастерская <span>/</span> {project?.name || 'Новый проект'}</div><div className="top-status">{busy > 0 && <span className="spinner"/>}<span className="badge">{version ? `Версия ${version.revision} · ${version.frozen ? 'зафиксирована' : 'черновик'}` : 'Локально · v0.1'}</span></div></header>
      {error && <div role="alert" className="alert"><span>{error}</span><button aria-label="Закрыть сообщение" onClick={() => setError('')}><X size={18}/></button></div>}
      {!ready ? <div className="empty"><span className="spinner"/> Подключение к локальному приложению…</div> : <>
        {tab !== 'connections' && !pid ? <div className="welcome"><span className="eyebrow">ОТ ПОНЯТИЙ К ПРОВЕРЯЕМОЙ РАЗМЕТКЕ</span><h1>Какие различия<br/>важно увидеть в ваших данных?</h1><p>Опишите их, проверьте на примерах и сохраните правила, по которым модель будет размечать тексты.</p><button className="primary" onClick={() => setNewOpen(true)}><Plus size={18}/> Создать первый проект</button><div className="welcome-foot"><BookOpen size={20}/><span>Начать можно без API-ключа: описать понятия и вручную разметить свои примеры.</span></div></div> : null}
        {tab === 'connections' && <Connections profiles={profiles} run={run} refresh={() => refresh()}/>}
        {pid && tab === 'ontology' && <OntologyEditor key={pid} {...work}/>}
        {pid && tab === 'annotation' && <Annotator {...work}/>}
        {pid && tab === 'data' && <DataImport {...work} counts={project?.counts || []}/>}
        {pid && tab === 'versions' && <Versions {...work} versions={project?.versions || []}/>}
        {pid && tab === 'runs' && <Runs {...work}/>}
      </>}
      <footer className="page-footer"><span>{total ? `${total} записей в проекте · ` : ''}Совпадение с экспертом не доказывает истинность онтологии.</span><span title={dataDirectory}>Данные на этом компьютере</span></footer>
    </main>
    {newOpen && <div className="modal-backdrop"><section role="dialog" aria-modal="true" aria-labelledby="new-title" className="modal"><button className="modal-close" onClick={() => setNewOpen(false)} aria-label="Закрыть"><X/></button><span className="eyebrow">НОВОЕ ИССЛЕДОВАНИЕ</span><h2 id="new-title">Назовите проект</h2><form onSubmit={e => { e.preventDefault(); void run(createProject); }}><label>Название<input autoFocus value={newName} onChange={e => setNewName(e.target.value)} placeholder="Например, сообщения жителей" required maxLength={200}/></label><button className="primary" type="submit" disabled={busy > 0}>Создать проект</button></form></section></div>}
    {busy > 0 && <div className="busy-strip" role="status">Выполняется операция… Внешний API может отвечать несколько минут.</div>}
  </div>;
}

function Versions({version, profiles, refresh, run, versions}: WorkProps & {versions:any[]}) {
  const [profile, setProfile] = useState(''); const [confirmed, setConfirmed] = useState(false); const [examples, setExamples] = useState<any[] | null>(null);
  useEffect(() => { setExamples(null); setConfirmed(false); }, [version?.id]);
  return <section className="content"><PageTitle eyebrow="04 / ВОСПРОИЗВОДИМОСТЬ" title="Зафиксируйте правила" description="Версия сохраняет определения, примеры настройки и параметры модели. Новое изменение создаёт отдельную версию."/>
    <div className="two-columns"><div className="panel"><h2>История версий</h2>{versions.length === 0 && <p>Сначала сохраните онтологию.</p>}{versions.map(v => <button key={v.id} className={'version-row ' + (version?.id === v.id ? 'selected' : '')} onClick={() => void run(() => refresh(v.id))}><GitBranch size={20}/><span><strong>Версия {v.revision}</strong><small>{v.note || 'Определения и правила'}</small></span><span className="badge">{v.frozen ? 'Зафиксирована' : 'Черновик'}</span></button>)}</div>
      {version && <div className="panel"><h2>Версия {version.revision}</h2><p>{version.body.unit}</p>{version.frozen ? <><div className="notice success">Эта версия неизменяема. В её запросах {version.examples.length} примеров настройки.</div><p>Модель: <strong>{version.parameters.connection?.model}</strong></p><p className="muted">Изменение ключа не меняет правила. Для другой модели или параметров создайте новую версию.</p><button onClick={() => void run(() => download('/versions/' + version.id + '/export', `ontology-v${version.revision}.json`))}><Download size={17}/> Экспорт онтологии</button><p className="hint">В пакет войдут только открытые синтетические примеры. Реальные и проверочные материалы исключены.</p></> : <>
        <label>Подключение для автоматической разметки<select value={profile} onChange={e => setProfile(e.target.value)}><option value="">Выберите подключение</option>{profiles.map(p => <option key={p.id} value={p.id}>{p.name} · {p.model}</option>)}</select></label>
        <button onClick={() => void run(async () => setExamples(await api('/versions/' + version.id + '/examples-preview')))}>Посмотреть примеры, которые войдут в запрос</button>
        {examples && <div className="examples-preview"><p><strong>{examples.length} примеров</strong> · максимум 20, только подтверждённая настройка</p>{examples.map(e => <details key={e.item_id}><summary>{e.provenance === 'synthetic' ? 'Синтетический' : 'Реальный'} · {e.text.slice(0,80)}</summary><pre>{JSON.stringify(e.annotation,null,2)}</pre></details>)}</div>}
        <label className="check"><input type="checkbox" checked={confirmed} onChange={e => setConfirmed(e.target.checked)}/> Я проверил единицу анализа, определения и примеры этой версии</label>
        <button className="primary" disabled={!confirmed || !profile || examples === null} onClick={() => void run(async () => { await api('/versions/' + version.id + '/freeze','POST',{profile_id:profile,unit_confirmed:true}); await refresh(); })}>Зафиксировать версию</button><p className="hint">Перенесённую разметку предыдущей версии нужно сначала пересмотреть. Проверочные ответы никогда не включаются в примеры запроса.</p></>}</div>}
    </div>
  </section>;
}

export function PageTitle({eyebrow,title,description,action}:{eyebrow:string;title:string;description:string;action?:React.ReactNode}) { return <div className="page-title"><div><span className="eyebrow">{eyebrow}</span><h1>{title}</h1><p>{description}</p></div>{action}</div>; }

export function Consent({title,profile,description,onCancel,onConfirm}:{title:string;profile:Profile;description:React.ReactNode;onCancel:()=>void;onConfirm:()=>void}) {
  return <div className="modal-backdrop"><section role="dialog" aria-modal="true" aria-labelledby="consent-title" className="modal"><span className="eyebrow">ПЕРЕДАЧА ДАННЫХ ВНЕ КОМПЬЮТЕРА</span><h2 id="consent-title">{title}</h2><dl className="details-list"><dt>Получатель</dt><dd>{profile.base_url}</dd><dt>Модель</dt><dd>{profile.model}</dd></dl><div>{description}</div><p className="hint">Убедитесь, что имеете право передать эти материалы. Запрос может быть платным. Правила хранения данных определяет выбранный провайдер.</p><div className="actions"><button onClick={onCancel}>Отмена</button><button className="primary" onClick={onConfirm}>Подтверждаю отправку</button></div></section></div>;
}
