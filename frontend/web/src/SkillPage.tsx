import { useState } from 'react';
import { ArrowUpRight, ChevronRight, Layers, Search } from 'lucide-react';
import { api, errorText, type Skill } from './api';
import { ErrorBanner, Markdown, Modal } from './components';

export default function SkillPage({ skills, refresh, onTry }: {
  skills: Skill[]; refresh: () => Promise<void>; onTry: (text: string) => void;
}) {
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState('all');
  const [detailId, setDetailId] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [working, setWorking] = useState('');
  const [entries, setEntries] = useState<Record<string, string>>({});
  const enabled = skills.filter(s => s.enabled).length;
  const detail = skills.find(s => s.id === detailId);
  const filtered = skills.filter(s => (filter !== 'enabled' || s.enabled) && `${s.label} ${s.description} ${s.name} ${s.source}`.toLowerCase().includes(query.toLowerCase()));
  async function toggle(skill: Skill) {
    setWorking(skill.id); setError('');
    try { await api(`/skills/${skill.id}`, { method: 'PATCH', body: JSON.stringify({ enabled: !skill.enabled }) }); await refresh(); }
    catch (error) { setError(errorText(error)); } finally { setWorking(''); }
  }
  async function loadEntry(skill: Skill) {
    setWorking(skill.id); setError('');
    try {
      const result = await api<{ content: string }>(`/skills/${encodeURIComponent(skill.id)}`);
      setEntries(current => ({ ...current, [skill.id]: result.content }));
    } catch (error) { setError(errorText(error)); } finally { setWorking(''); }
  }
  const sourceLabel = { builtin: '内置', user: '用户', project: '项目' };
  const switchButton = (skill: Skill) => <button className={`toggle ${skill.enabled ? 'on' : ''}`} role="switch" aria-checked={skill.enabled}
    aria-label={`启用 ${skill.label}`} disabled={!!working} onClick={() => toggle(skill)}><span /></button>;
  return <div className="page-content">
    <div className="page-intro"><div><span className="eyebrow">SKILLHUB</span><h2>为研究，添加新的视角</h2><p>发现并启用研究技能，让专业方法融入你的对话。</p></div><span className="summary-pill"><Layers size={16} />{enabled} 个技能已启用</span></div>
    {error && <ErrorBanner message={error} onClose={() => setError('')} />}
    <div className="skill-feature"><div><span className="badge blue">投研技能</span><h3>把方法留给技能，把探索交给对话</h3><p>启用后，助手可在对话中使用对应技能。支持财报解析、事件监控、研报摘要及综合报告；资料不足时明确展示缺口。</p></div><div className="feature-icon"><Layers size={54} strokeWidth={1} /></div></div>
    <div className="filter-toolbar"><div className="tabs"><button className={filter === 'all' ? 'selected' : ''} onClick={() => setFilter('all')}>全部技能 <span>{skills.length}</span></button><button className={filter === 'enabled' ? 'selected' : ''} onClick={() => setFilter('enabled')}>已启用 <span>{enabled}</span></button></div>
      <label className="search-field"><Search size={16} /><input aria-label="搜索技能" value={query} onChange={e => setQuery(e.target.value)} placeholder="搜索技能或研究方向" /></label></div>
    <div className="skill-grid">{filtered.map(skill => <article className="skill-card" key={skill.id}>
      <div className="skill-card-top"><div className="card-icon"><Layers size={23} /></div>{switchButton(skill)}</div>
      <div className="skill-title"><h3>{skill.label}</h3></div>
      <p>{skill.description}</p><div className="skill-tags"><span className="tag">{sourceLabel[skill.source]}</span></div>
      <footer><span>v{skill.version}</span><button className="text-button" onClick={() => setDetailId(skill.id)}>查看详情<ChevronRight size={15} /></button></footer>
    </article>)}</div>
    {!filtered.length && <div className="empty-list"><Search size={26} /><h3>{filter === 'enabled' ? '还没有启用技能' : '没有找到相关技能'}</h3><p>试试其他关键词，或在全部技能中选择一个技能。</p></div>}
    <p className="page-footnote"><Layers size={14} />每个技能独立启停，从下一轮对话开始生效。</p>
    {detail && <Modal title={detail.label} onClose={() => setDetailId(null)} wide>
      <div className="modal-body"><div className="detail-meta"><span className="tag">{sourceLabel[detail.source]}</span><span className="muted">v{detail.version}</span></div><p>{detail.description}</p>
        <div className="skill-instructions"><h3>/{detail.name}</h3>
          <div className="skill-metadata"><p>所有者：{detail.metadata.owner} · 状态：{detail.metadata.status} · 作用域：{detail.metadata.scope}</p>
          <p>必需工具：{detail.metadata.required_tools.join('、') || '无'}；可选工具：{detail.metadata.optional_tools.join('、') || '无'}</p>
          <p>操作声明：{detail.metadata.permissions.join('、')}；模型能力：{detail.metadata.compatible_models.join('、')}</p>
          {detail.metadata.deprecation && <p>废弃说明：{Object.values(detail.metadata.deprecation).join('；')}</p>}
          <small title={detail.metadata.content_hash}>内容指纹：{detail.metadata.content_hash ? detail.metadata.content_hash.slice(0, 16) : '待验证'}</small></div>
          {entries[detail.id] ? <Markdown text={entries[detail.id].replace(/^---\n[\s\S]*?\n---\n/, '')} /> : <button className="text-button" disabled={!!working} onClick={() => loadEntry(detail)}>查看技能流程</button>}
        </div>
      </div><footer className="modal-footer"><span className="toggle-label">{switchButton(detail)}{detail.enabled ? '已启用' : '未启用'}</span>
        <button className="button primary" disabled={!detail.enabled || ['draft', 'retired'].includes(detail.metadata.status)} onClick={() => { onTry(`/${detail.name}`); setDetailId(null); }}>在对话中试用<ArrowUpRight size={16} /></button>
      </footer>
    </Modal>}
  </div>;
}
