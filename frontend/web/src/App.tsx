import { useEffect, useState } from 'react';
import { ChevronRight, Cpu, Layers, LoaderCircle, Menu, MessageSquare, Plus, ShieldCheck, Trash2, X } from 'lucide-react';
import { api, errorText, type ModelProfile, type Session, type SessionSummary, type SessionFile, type Skill } from './api';
import ChatPage from './ChatPage';
import ModelPage from './ModelPage';
import SkillPage from './SkillPage';
import { ErrorBanner, Modal } from './components';
import { useConversation } from './useConversation';

type Page = 'chat' | 'models' | 'skills';
const nav = [
  { id: 'chat' as Page, label: '主对话', icon: MessageSquare },
  { id: 'skills' as Page, label: 'SkillHub', icon: Layers },
  { id: 'models' as Page, label: '模型配置', icon: Cpu },
];
function readPage(): Page {
  const path = location.pathname.slice(1);
  return path === 'skills' || path === 'models' ? path : 'chat';
}

export default function App() {
  const [page, setPage] = useState<Page>(readPage);
  const [models, setModels] = useState<ModelProfile[]>([]);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [profile, setProfile] = useState('');
  const [draft, setDraft] = useState('');
  const [researchType, setResearchType] = useState('');
  const [attachments, setAttachments] = useState<SessionFile[]>([]);
  const [artifacts, setArtifacts] = useState<SessionFile[]>([]);
  const [selectedFiles, setSelectedFiles] = useState<string[]>([]);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<SessionSummary | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState('');
  const defaultProfile = models.find(model => model.active && model.supported)?.id;
  useEffect(() => { if (!sessionId && defaultProfile) setProfile(defaultProfile); }, [defaultProfile]);

  async function refreshCatalog() {
    const [catalog, skillCatalog] = await Promise.all([
      api<{ items: ModelProfile[]; active_profile: string }>('/models'),
      api<{ items: Skill[] }>('/skills'),
    ]);
    setModels(catalog.items); setSkills(skillCatalog.items);
    setProfile(current => catalog.items.some(m => m.id === current && m.supported) ? current :
      catalog.items.find(m => m.active && m.supported)?.id || catalog.items.find(m => m.configured && m.supported)?.id || catalog.items.find(m => m.supported)?.id || '');
  }
  async function refreshSessions() {
    const result = await api<{ items: SessionSummary[] }>('/sessions');
    setSessions(result.items);
    return result.items;
  }
  const chat = useConversation(sessionId,
    () => { void Promise.all([refreshSessions(), refreshCatalog(), refreshFiles(sessionId)]).catch(e => setError(errorText(e))); },
    () => { setDraft(''); setSelectedFiles([]); },
    text => setDraft(current => current || text),
    () => { resetConversation(); void refreshSessions().catch(e => setError(errorText(e))); },
  );

  async function refreshFiles(id: string | null) {
    if (!id) { setAttachments([]); setArtifacts([]); return; }
    const [a, b] = await Promise.all([
      api<{items: SessionFile[]}>(`/sessions/${id}/attachments`),
      api<{items: SessionFile[]}>(`/sessions/${id}/artifacts`),
    ]);
    setAttachments(a.items); setArtifacts(b.items);
  }
  useEffect(() => {
    let disposed = false;
    setAttachments([]); setArtifacts([]); setSelectedFiles([]);
    if (sessionId) Promise.all([
      api<{items: SessionFile[]}>(`/sessions/${sessionId}/attachments`),
      api<{items: SessionFile[]}>(`/sessions/${sessionId}/artifacts`),
    ]).then(([a,b]) => { if (!disposed) { setAttachments(a.items); setArtifacts(b.items); } })
      .catch(e => { if (!disposed) setError(errorText(e)); });
    return () => { disposed = true; };
  }, [sessionId]);
  async function ensureSession() {
    if (sessionId) return sessionId;
    const created = await api<Session>('/sessions', { method: 'POST', body: JSON.stringify({ profile_id: selectedProfile }) });
    setSessionId(created.session_id); sessionStorage.setItem('researchx.web.session', created.session_id);
    await refreshSessions();
    return created.session_id;
  }
  async function uploadFiles(files: File[]) {
    if (files.length > 10 || selectedFiles.length + files.length > 10) { setError('每次最多选择10个附件'); return; }
    if (files.some(f => f.size > 30 * 1024 * 1024)) { setError('每个文件最多30 MB'); return; }
    setUploading(true); setError('');
    try {
      const id = await ensureSession();
      const body = new FormData(); files.forEach(file => body.append('files', file));
      const result = await api<{items: SessionFile[]}>(`/sessions/${id}/attachments`, { method: 'POST', body });
      await refreshFiles(id);
      setSelectedFiles(current => [...current, ...result.items.map(item => item.id)]);
    } catch (e) { setError(errorText(e)); } finally { setUploading(false); }
  }
  async function removeFile(id: string) {
    if (!sessionId) return;
    try { await api(`/sessions/${sessionId}/attachments/${id}`, {method: 'DELETE'});
      setSelectedFiles(current => current.filter(key => key !== id)); await refreshFiles(sessionId);
    } catch (e) { setError(errorText(e)); }
  }
  useEffect(() => {
    Promise.all([refreshCatalog(), refreshSessions()]).then(([, items]) => {
      const saved = sessionStorage.getItem('researchx.web.session');
      if (saved && items.some(item => item.session_id === saved)) setSessionId(saved);
    }).catch(e => setError(errorText(e))).finally(() => setLoading(false));
    const onPop = () => setPage(readPage());
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, []);

  function navigate(next: Page) {
    setPage(next); setSidebarOpen(false);
    history.pushState(null, '', `/${next}`);
  }
  function pickSession(id: string) {
    if (chat.busy) return;
    setDraft(''); chat.setError('');
    if (id === sessionId) chat.reconnect();
    else setSessionId(id);
    sessionStorage.setItem('researchx.web.session', id);
    navigate('chat');
  }
  const selectedProfile = chat.session?.profile_id || profile;
  async function changeProfile(id: string) {
    setError('');
    try {
      if (sessionId) {
        const updated = await api<Session>(`/sessions/${sessionId}`, { method: 'PATCH', body: JSON.stringify({ profile_id: id }) });
        chat.setSession(updated); await refreshSessions();
      }
      setProfile(id);
    } catch (error) { setError(errorText(error)); }
  }
  async function submit() {
    if ((!draft.trim() && !selectedFiles.length) || chat.busy || creating || uploading) return;
    if (!models.find(m => m.id === selectedProfile)?.configured) { navigate('models'); return; }
    setCreating(true); setError('');
    try {
      const id = await ensureSession();
      chat.send(id, draft.trim() || '请分析本次提交的附件，先识别资料类型与解析缺口。', selectedProfile, selectedFiles, researchType);
    } catch (error) { setError(errorText(error)); } finally { setCreating(false); }
  }
  function resetConversation() {
    setSessionId(null); sessionStorage.removeItem('researchx.web.session');
    setDraft(''); chat.setError(''); chat.setSession(null);
    if (defaultProfile) setProfile(defaultProfile);
    navigate('chat');
  }
  async function deleteConversation() {
    if (!deleteTarget || deleting) return;
    const id = deleteTarget.session_id;
    setDeleting(true); setDeleteError('');
    try {
      await api<{ ok: boolean }>(`/sessions/${id}`, { method: 'DELETE' });
      setSessions(current => current.filter(item => item.session_id !== id));
      if (id === sessionId) resetConversation();
      setDeleteTarget(null);
    } catch (error) { setDeleteError(errorText(error)); }
    finally { setDeleting(false); }
  }
  const title = nav.find(item => item.id === page)!.label;

  return <div className="app-shell">
    {sidebarOpen && <button className="sidebar-scrim" aria-label="收起导航" onClick={() => setSidebarOpen(false)} />}
    <aside className={`sidebar ${sidebarOpen ? 'open' : ''}`}>
      <div className="brand"><Layers size={36} role="img" aria-label="ResearchX" /><div><strong>ResearchX</strong><span>金融投研工作台</span></div><button className="icon-button mobile-close" aria-label="关闭导航" onClick={() => setSidebarOpen(false)}><X size={18} /></button></div>
      <nav aria-label="主要导航">{nav.map(({ id, label, icon: Icon }) => <button key={id} aria-label={label} className={`nav-item ${page === id ? 'active' : ''}`} onClick={() => navigate(id)}><Icon size={19} strokeWidth={1.7} /><span>{label}</span>{id === 'skills' && skills.filter(s => s.enabled).length > 0 && <span className="nav-count">{skills.filter(s => s.enabled).length}</span>}</button>)}</nav>
      <button className="new-conversation" disabled={chat.busy || creating || uploading} onClick={() => {
        setSessionId(null); sessionStorage.removeItem('researchx.web.session'); setDraft(''); chat.setError('');
        if (defaultProfile) setProfile(defaultProfile); navigate('chat');
      }}><Plus size={18} />新建对话</button>
      <div className="history-heading">研究记录<span>{sessions.length}</span></div>
      <div className="session-list">{sessions.length ? sessions.map(item => <div key={item.session_id} className={`session-row ${sessionId === item.session_id ? 'selected' : ''}`}>
        <button title={item.summary} disabled={chat.busy || uploading} className={`session-item ${sessionId === item.session_id ? 'selected' : ''}`} onClick={() => pickSession(item.session_id)}><MessageSquare size={15} /><span>{item.summary}</span></button>
        <button className="icon-button session-delete" aria-label={`删除对话：${item.summary}`} title={chat.busy && sessionId === item.session_id ? '请先停止生成' : '删除对话'}
          disabled={deleting || (chat.busy && sessionId === item.session_id)} onClick={() => { setDeleteError(''); setDeleteTarget(item); }}><Trash2 size={15} /></button>
      </div>) : <div className="history-empty">你的研究对话会保存在这里</div>}</div>
      <div className="sidebar-footer"><div className="local-avatar"><ShieldCheck size={18} /></div><div><strong>本地工作空间</strong><span>个人 · 数据保存在本机</span></div></div>
    </aside>
    <main className="main-workspace">
      <header className="workspace-header"><div><button className="icon-button mobile-menu" aria-label="打开导航" onClick={() => setSidebarOpen(true)}><Menu size={21} /></button><span className="muted breadcrumb-root">投研工作台</span><ChevronRight className="breadcrumb-root" size={14} /><h1>{title}</h1></div><span className="workspace-label"><span className="status-dot" />本地运行</span></header>
      {(error || chat.error) && <div className="global-error"><ErrorBanner message={error || chat.error} onClose={() => { setError(''); chat.setError(''); }} /></div>}
      {loading ? <div className="app-loading"><LoaderCircle className="spin" size={25} />正在连接本地工作台…</div> : page === 'chat' ?
        <ChatPage session={chat.session} models={models} skills={skills} selectedProfile={selectedProfile} onProfile={changeProfile}
          draft={draft} setDraft={setDraft} researchType={researchType} setResearchType={setResearchType} busy={chat.busy || creating} status={chat.status} connected={chat.connected} hasSession={!!sessionId}
          attachments={attachments} artifacts={artifacts} selectedFiles={selectedFiles} uploading={uploading}
          onUpload={uploadFiles} onRemoveFile={removeFile} onSelectFile={id => setSelectedFiles(current => current.includes(id) ? current.filter(key => key !== id) : current.length < 10 ? [...current, id] : current)}
          onSubmit={submit} onCancel={chat.cancel} onSteer={() => chat.steer(draft, researchType)} onSettings={() => navigate('models')} onSkills={() => navigate('skills')} prompt={chat.prompt} onRespond={chat.respond} /> :
        page === 'models' ? <ModelPage models={models} refresh={refreshCatalog} /> :
        <SkillPage skills={skills} refresh={refreshCatalog} onTry={text => { setDraft(text); navigate('chat'); }} />}
      {page !== 'chat' && chat.busy && <button className="running-banner" onClick={() => navigate('chat')}><LoaderCircle size={16} className="spin" />对话正在运行 · 返回查看进度{chat.prompt && '（等待操作确认）'}<ChevronRight size={16} /></button>}
    </main>
    {deleteTarget && <Modal title="删除对话" onClose={() => { if (!deleting) setDeleteTarget(null); }}>
      <div className="modal-body"><p>确定删除“{deleteTarget.summary}”？</p><p>本次对话、研究记忆和资料快照将被永久删除，无法恢复。</p>
        {deleteError && <ErrorBanner message={deleteError} />}</div>
      <footer className="modal-footer"><button data-autofocus className="button secondary" disabled={deleting} onClick={() => setDeleteTarget(null)}>取消</button>
        <button className="button danger" disabled={deleting} onClick={deleteConversation}>{deleting ? '正在删除…' : '确认删除'}</button></footer>
    </Modal>}
  </div>;
}
