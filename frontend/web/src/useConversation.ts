import { useEffect, useRef, useState } from 'react';
import { errorText, type Message, type Prompt, type Session, type ResearchProgress } from './api';

export function mergeResearchProgress(current: ResearchProgress | null | undefined, next: ResearchProgress): ResearchProgress {
  if (!current) return next;
  if (next.revision < current.revision) return current;
  if (current.plan_id !== next.plan_id) return next.revision >= current.revision ? next : current;
  const batches = new Map((current.dispatches || []).map(batch => [batch.dispatch_id, batch]));
  for (const batch of next.dispatches || []) {
    const previous = batches.get(batch.dispatch_id);
    if (!previous || batch.version > previous.version || (batch.version === previous.version && next.revision >= current.revision)) {
      batches.set(batch.dispatch_id, batch);
    }
  }
  return { ...(next.revision >= current.revision ? next : current), dispatches: [...batches.values()] };
}

export function useConversation(sessionId: string | null, onDone: () => void, onStarted: () => void,
  onFailed: (text: string) => void, onDeleted: () => void) {
  const [session, setSession] = useState<Session | null>(null);
  const [busy, setBusy] = useState(false);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState('');
  const [status, setStatus] = useState('');
  const [prompts, setPrompts] = useState<Prompt[]>([]);
  const prompt = prompts[0] || null;
  const [epoch, setEpoch] = useState(0);
  const stream = useRef<EventSource | null>(null);
  const connectionId = useRef('');
  const readySessionId = useRef<string | null>(null);
  const activeId = useRef('');
  const draftId = useRef<string | null>(null);
  const pending = useRef<{ sessionId: string; text: string; profileId: string; attachmentIds: string[]; researchType: string } | null>(null);
  const lastText = useRef('');
  const steering = useRef<{ id: string; text: string } | null>(null);
  const callbacks = useRef({ onDone, onStarted, onFailed, onDeleted });
  callbacks.current = { onDone, onStarted, onFailed, onDeleted };

  function append(row: Message) {
    setSession(current => current ? { ...current, messages: [...current.messages, row] } : current);
  }

  async function sendCommand(source: EventSource, id: string, command: Record<string, unknown>): Promise<boolean> {
    try {
      const response = await fetch(`/api/sessions/${id}/commands`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-ResearchX-Connection': connectionId.current },
        body: JSON.stringify(command),
      });
      const ack = await response.json();
      if (!response.ok) throw new Error(typeof ack.detail === 'string' ? ack.detail : '命令提交失败');
      if (ack.accepted !== true || ack.request_id !== command.request_id) throw new Error('命令确认无效');
      return stream.current === source;
    } catch (error) {
      if (stream.current === source) setError(errorText(error));
      return false;
    }
  }

  function dispatch(source: EventSource, id: string, text: string, profileId: string, attachmentIds: string[] = [], researchType = '') {
    activeId.current = crypto.randomUUID();
    lastText.current = text;
    draftId.current = null;
    setBusy(true); setError(''); setStatus('正在准备…');
    const requestId = activeId.current;
    void sendCommand(source, id, { type: 'submit', request_id: requestId, text, profile_id: profileId, attachment_ids: attachmentIds, research_type: researchType || null }).then(ok => {
      if (!ok && stream.current === source && activeId.current === requestId) {
        setBusy(false); setStatus(''); callbacks.current.onFailed(text);
      }
    });
  }

  useEffect(() => {
    readySessionId.current = null;
    setSession(null); setConnected(false); setPrompts([]); setStatus(''); setError('');
    activeId.current = ''; draftId.current = null; steering.current = null; connectionId.current = '';
    if (!sessionId) { setBusy(false); return; }
    let disposed = false;
    const source = new EventSource(`/api/sessions/${sessionId}/events`);
    stream.current = source;
    source.onmessage = event => {
      if (disposed) return;
      const data = JSON.parse(event.data);
      if (data.session_id !== sessionId) return;
      if (data.type === 'ready') {
        connectionId.current = data.connection_id;
        readySessionId.current = sessionId;
        activeId.current = data.running ? data.request_id : '';
        setSession(data.session); setConnected(true); setBusy(Boolean(data.running));
        if (pending.current?.sessionId === sessionId) {
          const queued = pending.current; pending.current = null;
          dispatch(source, sessionId, queued.text, queued.profileId, queued.attachmentIds, queued.researchType);
        }
        return;
      }
      if (data.type === 'session_deleted') { disposed = true; source.close(); stream.current = null; readySessionId.current = null; setSession(null); setBusy(false); setConnected(false); setPrompts([]); callbacks.current.onDeleted(); return; }
      if (data.request_id && data.request_id !== activeId.current && data.request_id !== steering.current?.id) return;
      const pendingSteer = steering.current;
      if (data.type === 'steer_accepted' && pendingSteer && pendingSteer.id === data.next_request_id) {
        // Keep consuming the old run through its final snapshot.
        setPrompts([]); setStatus('正在停止当前执行并重新规划…');
        return;
      }
      switch (data.type) {
        case 'started':
          if (pendingSteer && pendingSteer.id === data.request_id) {
            activeId.current = pendingSteer.id; lastText.current = pendingSteer.text;
            steering.current = null; draftId.current = null;
          }
          setSession(current => current ? { ...current, profile_id: data.profile_id, model: data.model } : current);
          {
            const id = activeId.current, text = lastText.current;
            setSession(current => current && !current.messages.some(row => row.id === id)
              ? { ...current, messages: [...current.messages, { id, role: 'user', text, turn_id: id }] } : current);
          }
          callbacks.current.onStarted(); setStatus('正在思考…'); break;
        case 'delta':
          if (!draftId.current) {
            draftId.current = data.id;
            append({ id: data.id, role: 'assistant', text: data.text, turn_id: data.turn_id, turn_status: 'running', phase: 'pending' });
          } else {
            const id = draftId.current;
            setSession(current => current ? { ...current, messages: current.messages.map(row => row.id === id ? { ...row, text: row.text + data.text } : row) } : current);
          }
          setStatus('正在生成…'); break;
        case 'message': {
          const row = data.message as Message;
          if (row.role === 'assistant') draftId.current = null;
          setSession(current => current ? { ...current, messages: current.messages.some(item => item.id === row.id)
            ? current.messages.map(item => item.id === row.id ? row : item) : [...current.messages, row] } : current);
          break;
        }
        case 'usage':
          setSession(current => current ? { ...current, usage: data.usage } : current); break;
        case 'research_progress':
          setSession(current => current ? { ...current, research_progress: mergeResearchProgress(current.research_progress, data.progress) } : current); break;
        case 'system': append({ id: crypto.randomUUID(), role: 'system', text: data.text }); break;
        case 'status': setStatus(data.message); break;
        case 'prompt': setPrompts(current => current.some(item => item.prompt_id === data.prompt_id) ? current : [...current, data]); break;
        case 'error': setError(data.message); break;
        case 'rejected': steering.current = null; setError(data.message); break;
        case 'clear': setSession(current => current ? { ...current, messages: [] } : current); break;
        case 'done':
          if (pendingSteer && pendingSteer.id === data.request_id) {
            activeId.current = pendingSteer.id; lastText.current = pendingSteer.text;
            steering.current = null;
          }
          setSession(data.session); setBusy(!!steering.current); setPrompts([]);
          setStatus(data.cancelled ? '已停止生成' : '');
          draftId.current = null;
          if (data.failed) callbacks.current.onFailed(lastText.current);
          callbacks.current.onDone(); break;
      }
    };
    source.onerror = () => {
      source.close();
      if (!disposed) {
        readySessionId.current = null; connectionId.current = ''; stream.current = null;
        setConnected(false); setBusy(false); setPrompts([]); pending.current = null; steering.current = null;
        setError('事件连接断开');
      }
    };
    return () => { disposed = true; source.close(); if (stream.current === source) stream.current = null; readySessionId.current = null; connectionId.current = ''; };
  }, [sessionId, epoch]);

  return {
    session, setSession, busy, connected, error, setError, status: prompt ? (prompt.kind === 'question' ? '等待你的回复' : '等待操作确认') : status, prompt,
    reconnect() { setEpoch(value => value + 1); },
    send(id: string, text: string, profileId: string, attachmentIds: string[] = [], researchType = '') {
      setBusy(true);
      // ensureSession can await a catalog refresh while this stream already receives
      // ready. Its caller then resumes with an older render's sessionId/connected.
      if (readySessionId.current === id && stream.current?.readyState === EventSource.OPEN) dispatch(stream.current, id, text, profileId, attachmentIds, researchType);
      else pending.current = { sessionId: id, text, profileId, attachmentIds, researchType };
    },
    cancel() { if (stream.current && readySessionId.current) void sendCommand(stream.current, readySessionId.current, { type: 'cancel', request_id: activeId.current }); },
    steer(text: string, researchType = '') {
      if (!text.trim() || !busy || !connected || steering.current || stream.current?.readyState !== EventSource.OPEN || !readySessionId.current) return;
      const id = crypto.randomUUID();
      steering.current = { id, text: text.trim() };
      const source = stream.current;
      void sendCommand(source, readySessionId.current, { type: 'steer', request_id: id, target_request_id: activeId.current, text: text.trim(), research_type: researchType || null }).then(ok => {
        if (!ok && stream.current === source && steering.current?.id === id) { steering.current = null; setStatus(''); }
      });
      setStatus('正在提交修改要求…');
    },
    respond(answer: string) {
      if (!prompt || !stream.current || !readySessionId.current) return;
      const source = stream.current;
      void sendCommand(source, readySessionId.current, { type: 'response', request_id: activeId.current, prompt_id: prompt.prompt_id, answer }).then(ok => {
        if (ok && stream.current === source) { setPrompts(current => current.filter(item => item.prompt_id !== prompt.prompt_id)); setStatus('正在继续…'); }
      });
    },
  };
}
