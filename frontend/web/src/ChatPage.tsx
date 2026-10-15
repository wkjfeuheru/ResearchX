import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { ArrowUp, BookOpen, Building2, Check, ChevronDown, Copy, Download, Paperclip, Trash2, Layers, MessageSquare, Square } from 'lucide-react';
import { api, type Delivery, type ModelProfile, type Prompt, type ResearchProgress, type Session, type SessionFile, type Skill, type Usage } from './api';
import { Markdown, Modal } from './components';
import ConversationTurn from './ConversationTurn';

const suggestions = [
  { icon: Building2, title: '开展公司研究', text: '我想研究一家上市公司，请先帮我梳理需要准备的资料。' },
  { icon: Layers, title: '比较行业机会', text: '我想比较两个行业，请先帮我明确研究问题和资料范围。' },
  { icon: BookOpen, title: '解读研究资料', text: '我有一份研究资料，希望你帮我区分事实、推断和待验证事项。' },
];

export function UsageLine({ usage }: { usage: Usage }) {
  const read = usage.cache_read_input_tokens;
  const observed = usage.cache_observed_input_tokens || 0;
  const rate = read != null && observed > 0 ? `${(100 * read / observed).toFixed(1)}%` : '未知';
  const partial = read != null && observed < (usage.input_tokens || 0);
  return <div className="usage-line" aria-label="会话用量" role="status">
    <span>输入 {(usage.input_tokens || 0).toLocaleString()}</span>
    <span>输出 {(usage.output_tokens || 0).toLocaleString()}</span>
    <span>缓存读取 {read?.toLocaleString() ?? '未提供'}</span>
    <span>缓存写入 {usage.cache_creation_input_tokens?.toLocaleString() ?? '未提供'}</span>
    <span>命中率 {rate}{partial ? '（统计不完整）' : ''}</span>
  </div>;
}

function ResearchTasks({ progress, sessionId }: { progress: ResearchProgress; sessionId: string }) {
  const labels = { pending: '待执行', ready: '可执行', in_progress: '进行中', validating: '验证中',
    completed: '已完成', blocked: '受阻', failed: '失败', cancelled: '已取消' };
  const projectLabels = { created: '已创建', planning: '规划中', running: '研究中', replanning: '重新规划中',
    suspended: '已暂停', validating: '交付验证中', completed: '研报已完成', failed: '失败', cancelled: '已取消' };
  const storageKey = `researchx:research-progress:${sessionId}:${progress.plan_id || 'replan'}`;
  const hasBlocked = progress.tasks.some(task => task.status === 'blocked');
  const conflicts = progress.conflicts || [];
  const conflictLabels = { open: '发现冲突', investigating: '核查原文中', awaiting_review: '等待审查',
    resolved: '裁决完成', unresolved: '仍未决', interrupted: '核查已中断' };
  const hasPendingConflict = conflicts.some(conflict => conflict.core && conflict.status !== 'resolved');
  const pendingBranches = (progress.dispatches || []).some(batch => batch.branches.some(branch => !branch.reviewed));
  const automaticOpen = pendingBranches || progress.replan_required || progress.completed < progress.total || hasBlocked || hasPendingConflict
    || !!(progress.project_status && progress.project_status !== 'completed');
  const [open, setOpen] = useState(automaticOpen);
  useEffect(() => {
    if (hasBlocked) { setOpen(true); return; }
    const saved = sessionStorage.getItem(storageKey);
    setOpen(saved == null ? automaticOpen : saved === 'open');
  }, [storageKey, hasBlocked]);
  useEffect(() => {
    if (sessionStorage.getItem(storageKey) == null) setOpen(automaticOpen);
  }, [automaticOpen, storageKey]);
  const toggle = () => setOpen(current => {
    if (hasBlocked) return true;
    const next = !current;
    sessionStorage.setItem(storageKey, next ? 'open' : 'closed');
    return next;
  });
  if (!progress.plan_id && !progress.replan_required && !progress.project_status) return null;
  return <div className="research-progress" aria-label="研究任务进度" aria-live="polite">
    <button type="button" className="research-progress-heading" aria-expanded={open}
      aria-disabled={hasBlocked} onClick={toggle}>
      <span className="research-progress-title"><ChevronDown size={14} className={open ? 'expanded' : ''} />
        <strong title={progress.replan_required ? '正在重新规划' : progress.title}>{progress.replan_required ? '正在重新规划' : progress.title}</strong></span>
      <span className="research-progress-count">{progress.project_status && `${projectLabels[progress.project_status]} · `}
        {!progress.replan_required && `${progress.completed}/${progress.total} 项任务完成`}</span>
    </button>
    {open && <ol>{progress.tasks.map(task => <li key={task.id} className={task.status}>
      <span className="task-status-symbol">{task.status === 'completed' ? <Check size={13} /> : '·'}</span>
      <span>{task.title}</span><small>{labels[task.status]}{task.status === 'blocked' && task.blocker ? ` · ${task.blocker}` : ''}</small>
    </li>)}</ol>}
    {open && (progress.dispatches || []).map(batch => <ol key={batch.dispatch_id} aria-label="并行采集进度">
      {batch.branches.map(branch => <li key={branch.task_id} className={branch.reviewed ? 'completed' : branch.status === 'running' ? 'in_progress' : branch.status === 'awaiting_review' ? 'validating' : branch.status}>
        <span className="task-status-symbol">{branch.reviewed ? <Check size={13} /> : '·'}</span>
        <span>{branch.title}</span><small>{branch.reviewed ? '主 Agent 已核对' : ({ queued: '排队中', running: '采集中', awaiting_review: '采集结束 · 待主代理核对', failed: '失败', cancelled: '已取消', interrupted: '已中断' } as Record<string, string>)[branch.status] || branch.status}</small>
      </li>)}
    </ol>)}
    {open && conflicts.length > 0 && <ol aria-label="争议处理进度">{conflicts.map(conflict =>
      <li key={conflict.id} className={conflict.status === 'resolved' ? 'completed' : 'blocked'}>
        <span className="task-status-symbol">{conflict.status === 'resolved' ? <Check size={13} /> : '·'}</span>
        <span>{conflict.question}</span><small>{conflictLabels[conflict.status]}</small>
      </li>)}</ol>}
  </div>;
}

function Approval({ prompt, onRespond }: { prompt: Prompt; onRespond: (answer: string) => void }) {
  const [answer, setAnswer] = useState('');
  return <Modal title={prompt.kind === 'question' ? '补充研究信息' : '操作确认'} onClose={() => onRespond(prompt.kind === 'question' ? '用户取消了本次回答' : 'deny')} wide>
    <div className="modal-body">
      {prompt.tool_name && <span className="tag">{prompt.tool_label || '工具操作'}</span>}
      <p>{prompt.message || (prompt.kind === 'edit' ? `请求编辑：${prompt.path}` : '请确认是否允许此次操作。')}</p>
      {prompt.kind !== 'question' && prompt.session_scope && <p>选择“本会话始终允许”后，{prompt.session_scope}</p>}
      {prompt.diff && <pre className="diff">{prompt.diff}</pre>}
      {prompt.kind === 'question' && <textarea autoFocus aria-label="补充信息" rows={5} value={answer} onChange={e => setAnswer(e.target.value)} placeholder="输入你的回复…" />}
    </div>
    <footer className="modal-footer">
      <button className="button secondary" onClick={() => onRespond(prompt.kind === 'question' ? '用户取消了本次回答' : 'deny')}>取消</button>
      {prompt.kind !== 'question' && prompt.session_scope && <button className="button secondary" onClick={() => onRespond('allow_session')}>本会话始终允许</button>}
      <button className="button primary" disabled={prompt.kind === 'question' && !answer.trim()} onClick={() => onRespond(prompt.kind === 'question' ? answer : 'allow')}>
        {prompt.kind === 'question' ? '回复并继续' : '允许此次操作'}
      </button>
    </footer>
  </Modal>;
}

export default function ChatPage({ session, models, skills, selectedProfile, onProfile, draft, setDraft,
  researchType, setResearchType, busy, status, connected, hasSession, onSubmit, onCancel, onSteer, onSettings, onSkills, prompt, onRespond, attachments, artifacts, selectedFiles, uploading, onUpload, onRemoveFile, onSelectFile }: {
  attachments: SessionFile[]; artifacts: SessionFile[]; selectedFiles: string[]; uploading: boolean;
  onUpload: (files: File[]) => void; onRemoveFile: (id: string) => void; onSelectFile: (id: string) => void;
  session: Session | null; models: ModelProfile[]; skills: Skill[];
  selectedProfile: string; onProfile: (id: string) => void; draft: string; setDraft: (value: string) => void;
  researchType: string; setResearchType: (value: string) => void;
  busy: boolean; status: string; connected: boolean; hasSession: boolean; onSubmit: () => void;
  onCancel: () => void; onSettings: () => void; onSkills: () => void;
  onSteer: () => void;
  prompt: Prompt | null; onRespond: (answer: string) => void;
}) {
  const scroll = useRef<HTMLDivElement>(null);
  const previousScrollHeight = useRef(0);
  const [copied, setCopied] = useState('');
  const [deliveries, setDeliveries] = useState<Delivery[]>([]);
  const model = models.find(m => m.id === selectedProfile);
  const enabled = skills.filter(s => s.enabled);
  const messages = session?.messages || [];
  const finalAnswers = messages.filter(row => row.role === 'assistant' && !!row.answer_id).length;
  useEffect(() => {
    const sessionId = session?.session_id;
    if (!sessionId || busy) { if (!sessionId) setDeliveries([]); return; }
    const controller = new AbortController();
    void api<{ items: Delivery[] }>(`/sessions/${sessionId}/deliveries`, { signal: controller.signal })
      .then(result => { if (!controller.signal.aborted) setDeliveries(result.items); })
      .catch(() => { if (!controller.signal.aborted) setDeliveries([]); });
    return () => controller.abort();
  }, [session?.session_id, busy, finalAnswers, artifacts.length]);
  async function onReview(deliveryId: string, manifestHash: string, decision: 'approve' | 'changes_requested') {
    const sessionId = session?.session_id;
    if (!sessionId) return;
    const note = decision === 'approve' ? '已核对事实、推断和预测依据' : '需要按意见修改后重新冻结';
    try {
      const updated = await api<Delivery>(`/sessions/${sessionId}/deliveries/${deliveryId}/review`, {
        method: 'POST',
        body: JSON.stringify({ decision, expected_manifest_hash: manifestHash, note, reviewed_claim_ids: [] }),
      });
      setDeliveries(current => current.map(item => item.delivery_id === deliveryId ? { ...item, ...updated } : item));
    } catch {
      return;
    }
  }
  useLayoutEffect(() => { previousScrollHeight.current = 0; }, [session?.session_id]);
  useLayoutEffect(() => {
    const element = scroll.current;
    if (!element) return;
    // Determine where the user was before this content grew. A scroll event
    // caused by that growth must not disable following the current stream.
    if (!previousScrollHeight.current || previousScrollHeight.current - element.scrollTop - element.clientHeight < 100) {
      element.scrollTop = element.scrollHeight;
    }
    previousScrollHeight.current = element.scrollHeight;
  }, [messages]);
  const renderedTurns = new Set<string>();

  return <div className="chat-page">
    <div className="chat-toolbar">
      <div className="model-select"><span className="status-dot" />
        <select aria-label="当前对话模型" value={selectedProfile} disabled={busy} onChange={e => onProfile(e.target.value)}>
          {models.filter(m => m.supported).map(m => <option value={m.id} key={m.id}>{m.label} · {m.model}{m.configured ? '' : '（未配置）'}</option>)}
        </select><ChevronDown size={15} />
      </div>
      <button className="skill-summary" onClick={onSkills}><Layers size={15} />{enabled.length ? `${enabled.length} 个技能已启用` : '选择研究技能'}</button>
    </div>
    <div className="conversation-scroll" ref={scroll}>
      {!messages.length ? <div className="chat-welcome">
        <div className="welcome-symbol"><MessageSquare size={28} strokeWidth={1.5} /></div>
        <span className="eyebrow">YOUR RESEARCH WORKSPACE</span>
        <h2>让研究，从一个好问题开始</h2>
        <p>整理资料，梳理观点，探索研究思路。<br />与你的投研助手一起，把问题逐步展开。</p>
        <div className="suggestion-grid">{suggestions.map(({ icon: Icon, title, text }) => <button className="suggestion" key={title} onClick={() => setDraft(text)}>
          <Icon size={21} strokeWidth={1.5} /><strong>{title}</strong><span>{text}</span><span className="suggestion-arrow">↗</span>
        </button>)}</div>
        {!model?.configured && <div className="setup-callout"><span>先连接一个模型，开启你的第一轮研究。</span><button className="text-button" onClick={onSettings}>配置模型 →</button></div>}
      </div> : <div className="message-list">{messages.map(message => {
        if (message.role === 'tool' || message.role === 'tool_result') return null;
        if (message.turn_id && (message.role === 'assistant' || message.role === 'activity')) {
          if (renderedTurns.has(message.turn_id)) return null;
          renderedTurns.add(message.turn_id);
          return <ConversationTurn key={`turn:${message.turn_id}`} rows={messages.filter(row => row.turn_id === message.turn_id && (row.role === 'assistant' || row.role === 'activity'))} />;
        }
        if (message.role === 'system') return <div className="system-row" key={message.id}>{message.text}</div>;
        return <article className={`message ${message.role}`} key={message.id}>
          <div className="message-avatar">{message.role === 'user' ? '你' : <BookOpen size={18} />}</div>
          <div className="message-body"><div className="message-label">{message.role === 'user' ? '你' : 'ResearchX'}{message.role === 'assistant' && <span>投研助手</span>}</div>
            <Markdown text={message.text} />
            {message.role === 'assistant' && <button className="copy-button" aria-label="复制回复" onClick={async () => {
              try { await navigator.clipboard.writeText(message.text); setCopied(message.id); }
              catch { setCopied(''); }
            }}>{copied === message.id ? <Check size={14} /> : <Copy size={14} />}{copied === message.id ? '已复制' : '复制'}</button>}
          </div>
        </article>;
      })}</div>}
    </div>
    <div className="composer-container">
      {session?.research_progress && <ResearchTasks progress={session.research_progress} sessionId={session.session_id} />}
      {deliveries.length > 0 && <div className="session-artifacts" aria-label="研究交付版本">{deliveries.map(delivery => <div key={delivery.delivery_id} title={delivery.coverage.gaps.join('；')}>
        {delivery.kind === 'answer' ? '点评草稿' : '报告草稿'} · {delivery.coverage.status === 'complete' ? '机器核验通过' : `资料${delivery.coverage.status === 'partial' ? '不完整' : '受阻'}`} · {({ awaiting_review: '待人工复核', blocked: '暂不可批准', approved: '人工审核通过', changes_requested: '需修改', invalidated: '批准已失效' } as Record<string, string>)[delivery.status]}
        {delivery.coverage.gaps.length > 0 && <small>{delivery.coverage.gaps.join('；')}</small>}
        {delivery.status === 'awaiting_review' && <button type="button" onClick={() => onReview(delivery.delivery_id, delivery.manifest_hash, 'approve')}>批准</button>}
        {delivery.status === 'awaiting_review' && <button type="button" onClick={() => onReview(delivery.delivery_id, delivery.manifest_hash, 'changes_requested')}>要求修改</button>}
      </div>)}</div>}
      {enabled.length > 0 && <div className="active-skills">{enabled.map(skill => <span key={skill.id}><Layers size={12} />{skill.label}</span>)}</div>}
      {artifacts.length > 0 && <div className="session-artifacts" aria-label="研究产物">{artifacts.map(file => <a key={file.id} href={`/api/sessions/${session?.session_id}/artifacts/${file.id}/download`} download>
        <Download size={14} />{file.name}<small>{file.status}{file.task_id ? ` · ${file.task_id}` : ''}</small></a>)}</div>}
      {attachments.length > 0 && <div className="session-attachments" aria-label="会话附件">{attachments.map(file => <div className="attachment" key={file.id} title={file.gaps?.join('；')}>
        <label><input type="checkbox" disabled={busy || uploading} checked={selectedFiles.includes(file.id)} onChange={() => onSelectFile(file.id)} />{file.name}<small>{file.status === 'ready' ? '可读取' : file.status === 'partial' ? '部分可读取' : file.status === 'failed' ? '解析失败' : '不支持'}{file.gaps?.length ? ` · ${file.gaps.join('；')}` : ''}</small></label>
        <button type="button" className="icon-button" aria-label={`删除附件 ${file.name}`} disabled={busy || uploading} onClick={() => onRemoveFile(file.id)}><Trash2 size={13} /></button></div>)}</div>}
      <form className="composer" onSubmit={e => { e.preventDefault(); if (busy) onSteer(); else onSubmit(); }}>
        <label>研究类型 <select aria-label="研究类型" value={researchType} disabled={busy} onChange={e => setResearchType(e.target.value)}>
          <option value="">自动识别</option>
          <option value="generic-research">普通问答</option>
          <option value="event_commentary">事件点评</option>
          <option value="financial_commentary">财报点评</option>
          <option value="financial-statement-analysis">财报解析</option>
          <option value="earnings-forecast">盈利预测</option>
          <option value="financial-commentary">业绩点评</option>
          <option value="company-event-monitor">事件监测</option>
          <option value="research-report-digest">研报精读</option>
          <option value="industry-commentary">行业点评</option>
          <option value="industry-deep-dive">行业深度</option>
          <option value="deep-investment-report">公司深度</option>
        </select></label>
        <textarea aria-label="对话输入" placeholder={busy ? '输入修改要求，可打断当前研究并重新规划…' : '输入研究问题，或粘贴需要分析的资料…'} rows={3} value={draft}
          onChange={e => setDraft(e.target.value)} onKeyDown={e => {
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); if (draft.trim() || selectedFiles.length) { if (busy) onSteer(); else onSubmit(); } }
          }} />
        <div className="composer-footer"><label className={`upload-button ${busy || uploading ? 'disabled' : ''}`}><Paperclip size={16} />{uploading ? '正在上传…' : '添加资料'}
          <input aria-label="上传研究资料" type="file" accept=".pdf,.txt,.md" multiple disabled={busy || uploading} onChange={e => { const files = Array.from(e.target.files || []); if (files.length) onUpload(files); e.target.value = ''; }} /></label><span>{status || 'Enter 发送 · Shift + Enter 换行'}</span>
          {busy ? <div className="composer-actions"><button type="button" className="button secondary steer-button" disabled={!draft.trim() || !connected} onClick={onSteer}>打断并修改</button>
            <button type="button" className="send-button stop" aria-label="停止生成" onClick={onCancel}><Square size={15} fill="currentColor" /></button></div> :
            <button className="send-button" aria-label="发送消息" disabled={uploading || (!draft.trim() && !selectedFiles.length) || !model?.configured || (hasSession && !connected)}><ArrowUp size={20} /></button>}
        </div>
      </form>
      {session && <UsageLine usage={session.usage} />}
      <p className="composer-note">研究结论请结合原始资料核验 · PDF / TXT / MD，每个文件最多30 MB，每次最多10个；扫描件不支持OCR</p>
    </div>
    {prompt && <Approval key={prompt.prompt_id} prompt={prompt} onRespond={onRespond} />}
  </div>;
}
