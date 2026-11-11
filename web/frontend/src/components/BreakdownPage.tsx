import { useEffect, useState, useSyncExternalStore } from 'react';
import { ApiError, createIdea, readBreakdownLink, runAgent } from '../lib/api';
import type { BreakdownNote } from '../lib/api';
import { renderMarkdown } from '../lib/sanitize';
import { IconFire, IconIdea, IconSkills } from './icons';

interface BreakdownPageProps {
  persona: string;
}

interface BreakdownState {
  input: string;
  result: string;
  error: string;
  warning: string;
  loading: boolean;
  stage: string;
  notes: BreakdownNote[];
  steps: BreakdownStep[];
  startedAt: number;
  linkTotal: number;
  updatedAt: number;
}

type BreakdownStepStatus = 'pending' | 'active' | 'done' | 'skipped' | 'error';

interface BreakdownStep {
  id: string;
  label: string;
  detail: string;
  status: BreakdownStepStatus;
}

const STORAGE_KEY = 'easel:breakdown-state-v1';
const MAX_LINKS = 3;
const STEP_LABELS = [
  ['input', '解析输入与链接'],
  ['fetch', '读取小红书笔记'],
  ['prepare', '整理可验证素材'],
  ['analyze', 'AI 拆解结构与钩子'],
  ['finish', '生成拆解结果'],
] as const;
const EMPTY_STATE: BreakdownState = {
  input: '', result: '', error: '', warning: '', loading: false, stage: '',
  notes: [], steps: [], startedAt: 0, linkTotal: 0, updatedAt: 0,
};

function newBreakdownSteps(): BreakdownStep[] {
  return STEP_LABELS.map(([id, label], index) => ({
    id, label, detail: '',
    status: index === 0 ? 'active' : 'pending',
  }));
}

function isBreakdownNote(value: unknown): value is BreakdownNote {
  if (!value || typeof value !== 'object') return false;
  const note = value as Record<string, unknown>;
  return typeof note.title === 'string'
    && typeof note.author === 'string'
    && Array.isArray(note.tags)
    && typeof note.image_count === 'number';
}

function isBreakdownStep(value: unknown): value is BreakdownStep {
  if (!value || typeof value !== 'object') return false;
  const step = value as Record<string, unknown>;
  return typeof step.id === 'string'
    && typeof step.label === 'string'
    && typeof step.detail === 'string'
    && ['pending', 'active', 'done', 'skipped', 'error'].includes(String(step.status));
}

function loadBreakdownState(): BreakdownState {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return { ...EMPTY_STATE };
    const value = JSON.parse(raw) as Partial<BreakdownState>;
    const interrupted = value.loading === true;
    return {
      input: typeof value.input === 'string' ? value.input.slice(0, 60000) : '',
      result: typeof value.result === 'string' ? value.result : '',
      error: interrupted
        ? '上次拆解在页面刷新时中断，请重新开始。'
        : typeof value.error === 'string' ? value.error : '',
      warning: typeof value.warning === 'string' ? value.warning : '',
      loading: false,
      stage: '',
      notes: Array.isArray(value.notes)
        ? value.notes.filter(isBreakdownNote).slice(0, MAX_LINKS)
        : [],
      steps: Array.isArray(value.steps)
        ? value.steps.filter(isBreakdownStep).map((step) => (
          interrupted && step.status === 'active' ? { ...step, status: 'error' } : step))
        : [],
      startedAt: typeof value.startedAt === 'number' && value.startedAt > 0 ? value.startedAt : 0,
      linkTotal: typeof value.linkTotal === 'number' && value.linkTotal >= 0
        ? Math.min(value.linkTotal, MAX_LINKS) : 0,
      updatedAt: typeof value.updatedAt === 'number' ? value.updatedAt : 0,
    };
  } catch {
    return { ...EMPTY_STATE };
  }
}

let breakdownState = loadBreakdownState();
let activeRun = 0;
let persistTimer = 0;
const listeners = new Set<() => void>();

function persistBreakdownState() {
  window.clearTimeout(persistTimer);
  persistTimer = window.setTimeout(() => {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(breakdownState));
    } catch {
      // 存储空间不足时，页面内和切页状态仍然保留。
    }
  }, 120);
}

function updateBreakdown(patch: Partial<BreakdownState>) {
  breakdownState = { ...breakdownState, ...patch, updatedAt: Date.now() };
  listeners.forEach((listener) => listener());
  persistBreakdownState();
}

function updateBreakdownStep(id: string, status: BreakdownStepStatus, detail?: string) {
  const current = getBreakdownSnapshot();
  updateBreakdown({
    steps: current.steps.map((step) => step.id === id
      ? { ...step, status, detail: detail === undefined ? step.detail : detail }
      : step),
  });
}

function clearBreakdown() {
  activeRun += 1;
  updateBreakdown({ ...EMPTY_STATE });
}

function subscribeBreakdown(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function getBreakdownSnapshot() {
  return breakdownState;
}

function extractUrls(value: string): string[] {
  const found = value.match(/https?:\/\/[^\s<>"'`]+/gi) || [];
  return [...new Set(found.map((url) => url.replace(/[)\]}>，。；、,.!?！？]+$/g, '')))];
}

function isXiaohongshuUrl(value: string): boolean {
  try {
    const host = new URL(value).hostname.toLocaleLowerCase();
    return host === 'xiaohongshu.com' || host.endsWith('.xiaohongshu.com')
      || host === 'xhslink.com' || host.endsWith('.xhslink.com');
  } catch {
    return false;
  }
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : '拆解失败，请重试';
}

function formatElapsed(seconds: number): string {
  const value = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(value / 60);
  return `${String(minutes).padStart(2, '0')}:${String(value % 60).padStart(2, '0')}`;
}

export default function BreakdownPage({ persona }: BreakdownPageProps) {
  const state = useSyncExternalStore(
    subscribeBreakdown, getBreakdownSnapshot, getBreakdownSnapshot);
  const [toast, setToast] = useState('');
  const [clock, setClock] = useState(Date.now());

  useEffect(() => {
    if (!state.loading) return;
    setClock(Date.now());
    const timer = window.setInterval(() => setClock(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [state.loading, state.startedAt]);

  const elapsed = state.loading && state.startedAt > 0
    ? Math.max(0, Math.floor((clock - state.startedAt) / 1000))
    : 0;
  const completedSteps = state.steps.filter(
    (step) => step.status === 'done' || step.status === 'skipped').length;
  const progressUnits = state.steps.reduce((total, step) => total + (
    step.status === 'done' || step.status === 'skipped' || step.status === 'error' ? 1
      : step.status === 'active' ? 0.45 : 0), 0);
  const progressPercent = state.steps.length
    ? Math.min(100, Math.round((progressUnits / state.steps.length) * 100))
    : 0;
  const eta = state.linkTotal > 1 ? '通常 20–90 秒'
    : state.linkTotal === 1 ? '通常 15–60 秒' : '通常 10–45 秒';

  const run = async () => {
    const current = getBreakdownSnapshot();
    const raw = current.input.trim();
    if (!raw || current.loading) return;
    const token = ++activeRun;
    updateBreakdown({
      result: '', error: '', warning: '', loading: true,
      stage: '正在解析输入与链接…', notes: [],
      steps: newBreakdownSteps(), startedAt: Date.now(), linkTotal: 0,
    });

    try {
      const urls = extractUrls(raw);
      const noteUrls = urls.filter(isXiaohongshuUrl);
      const textWithoutUrls = urls.reduce((text, url) => text.split(url).join(''), raw).trim();
      if (urls.length > 0 && noteUrls.length === 0 && !textWithoutUrls) {
        throw new Error('当前只自动读取小红书链接；其他平台的链接请先粘贴正文。');
      }

      const links = noteUrls.slice(0, MAX_LINKS);
      updateBreakdown({ linkTotal: links.length });
      updateBreakdownStep('input', 'done',
        links.length > 0 ? `识别到 ${links.length} 条小红书链接` : '纯文本输入');
      if (links.length > 0) {
        updateBreakdownStep('fetch', 'active', `准备读取 0/${links.length}`);
      } else {
        updateBreakdownStep('fetch', 'skipped', '纯文本输入，无需读取链接');
      }
      const failures: string[] = [];
      const fetched: Awaited<ReturnType<typeof readBreakdownLink>>[] = [];
      for (let index = 0; index < links.length; index += 1) {
        if (token !== activeRun) return;
        updateBreakdown({ stage: `正在读取小红书笔记 ${index + 1}/${links.length}…` });
        updateBreakdownStep('fetch', 'active', `正在读取 ${index + 1}/${links.length}`);
        try {
          fetched.push(await readBreakdownLink(links[index]));
        } catch (error) {
          if (error instanceof ApiError && error.status === 409) throw error;
          failures.push(`第 ${index + 1} 条：${errorMessage(error)}`);
        }
        updateBreakdownStep('fetch', 'active', `已读取 ${fetched.length}/${links.length}`);
      }
      if (token !== activeRun) return;
      if (links.length > 0 && fetched.length === 0) {
        throw new Error(failures.join('；') || '没有从链接中读取到笔记内容。');
      }
      updateBreakdownStep('fetch', links.length > 0 ? 'done' : 'skipped',
        links.length > 0 ? `成功读取 ${fetched.length}/${links.length} 条` : '纯文本输入');

      const warnings: string[] = [];
      if (noteUrls.length > MAX_LINKS) {
        warnings.push(`一次最多读取 ${MAX_LINKS} 条链接，已忽略其余 ${noteUrls.length - MAX_LINKS} 条。`);
      }
      if (failures.length > 0) warnings.push(`部分链接未读取：${failures.join('；')}`);
      updateBreakdownStep('prepare', 'done',
        fetched.length > 0 ? `已整理 ${fetched.length} 篇笔记与互动数据` : '已整理粘贴正文');
      updateBreakdownStep('analyze', 'active', '正在分析，进度会持续计时');
      updateBreakdown({
        stage: 'AI 正在拆解…',
        notes: fetched.map((item) => item.note),
        warning: warnings.join(' '),
      });

      const sourceText = fetched.length > 0
        ? fetched.map((item, index) => `【小红书笔记 ${index + 1}】\n${item.text}`).join('\n\n')
        : '本次没有使用链接自动读取，只依据用户粘贴的正文拆解。';
      const prompt =
        `你是小红书爆款内容拆解专家。请只依据下方可验证材料做拆解，不要脑补未提供的图片文字、数据或用户反馈。\n` +
        `如果有多篇笔记，先提炼共性，再指出差异。互动数据只用于判断选题和传播表现，不能当成爆款因果。\n\n` +
        `请用中文分点输出：\n` +
        `1. **一句话概括**：这条内容到底在卖什么感觉/解决什么问题\n` +
        `2. **素材边界**：哪些信息可验证，哪些信息缺失（尤其图片内文字）\n` +
        `3. **钩子拆解**：标题、开头、首图承诺分别为什么抓人\n` +
        `4. **结构套路**：正文分段、节奏、信息密度与互动引导\n` +
        `5. **为什么会火**：情绪、共鸣、实用价值、争议点或平台语境\n` +
        `6. **可复制模板**：抽象成不抄原句、可直接套用的标题+正文结构\n` +
        `7. **结合我的画像做 3 个选题**：${persona ? `画像「${persona}」` : '通用 3C 小家电内容账号'}\n\n` +
        `【用户输入与补充说明】\n${raw}\n\n` +
        `【已读取的笔记数据】\n${sourceText}`;
      const response = await runAgent(prompt, persona);
      if (token !== activeRun) return;
      updateBreakdown({
        result: response.response, loading: false, stage: '',
        steps: getBreakdownSnapshot().steps.map((step) => (
          step.status === 'skipped' ? step : { ...step, status: 'done' })),
      });
    } catch (error) {
      if (token !== activeRun) return;
      updateBreakdown({
        error: errorMessage(error), loading: false, stage: '',
        steps: getBreakdownSnapshot().steps.map((step) => (
          step.status === 'active' ? { ...step, status: 'error' } : step)),
      });
    }
  };

  const saveToIdeas = async () => {
    if (!state.result) return;
    const sourceTitle = state.notes[0]?.title
      || state.input.trim().split('\n')[0].slice(0, 24)
      || '未命名素材';
    try {
      await createIdea({
        title: `拆解模板：${sourceTitle}`,
        note: state.result,
        source: '爆款拆解',
        status: 'pending',
      });
      setToast('已存入选题库');
      window.setTimeout(() => setToast(''), 2200);
    } catch (error) {
      setToast(errorMessage(error));
      window.setTimeout(() => setToast(''), 2600);
    }
  };

  return (
    <div className="page-scroll breakdown-page">
      <div className="page-head">
        <div>
          <h1 className="page-title"><IconFire size={21} /> 爆款拆解</h1>
          <p className="page-subtitle">
            粘贴小红书链接或正文。链接会先读取标题、正文、话题和互动数据，再拆钩子、结构、火的原因与可复制模板。
          </p>
        </div>
      </div>

      <div className="breakdown-body">
        <textarea className="field" style={{ minHeight: 160 }} value={state.input} maxLength={60000}
          placeholder="粘贴小红书笔记链接，或直接粘贴正文；最多自动读取 3 条链接…"
          onChange={(event) => updateBreakdown({ input: event.target.value })} />
        <div style={{ marginTop: 12, display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          <button className="btn btn-primary" disabled={state.loading || !state.input.trim()} onClick={run}>
            <IconSkills size={15} /> {state.loading ? '处理中…' : '读取并拆解'}
          </button>
          {state.result && (
            <button className="btn" onClick={saveToIdeas}>
              <IconIdea size={14} /> 存入选题库
            </button>
          )}
          {(state.input || state.result || state.loading) && (
            <button className="btn btn-ghost" onClick={() => { clearBreakdown(); setToast(''); }}>
              清空
            </button>
          )}
        </div>

        {state.notes.length > 0 && !state.loading && (
          <div className="panel" style={{ marginTop: 16 }}>
            <div className="panel-title"><IconFire size={14} /> 已读取素材</div>
            <div style={{ display: 'grid', gap: 8 }}>
              {state.notes.map((note, index) => (
                <div key={`${note.url}:${index}`} style={{ fontSize: 13, lineHeight: 1.65 }}>
                  <strong>{note.title || '无标题笔记'}</strong>
                  <span style={{ color: 'var(--text-secondary)' }}>
                    {note.author ? ` · @${note.author}` : ''}
                    {` · 图片 ${note.image_count} 张`}
                    {` · 赞 ${note.likes} / 藏 ${note.collects} / 评 ${note.comments}`}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}

        {state.warning && <div className="notice-error" role="status" style={{ marginTop: 14 }}>{state.warning}</div>}
        {state.error && <div className="notice-error" role="alert" style={{ marginTop: 14 }}>{state.error}</div>}
        {state.loading && (
          <section className="breakdown-progress" role="status" aria-live="polite">
            <div className="breakdown-progress__head">
              <div>
                <div className="breakdown-progress__title">{state.stage || '正在拆解…'}</div>
                <div className="breakdown-progress__meta">
                  已用 {formatElapsed(elapsed)} · 预计 {eta} · 可以切到别的页面，回来会继续显示进度
                </div>
              </div>
              <div className="breakdown-progress__count">
                {completedSteps}/{state.steps.length || STEP_LABELS.length} 步
              </div>
            </div>
            <div className="breakdown-progress__bar" aria-hidden="true">
              <span style={{ width: `${progressPercent}%` }} />
            </div>
            <ol className="breakdown-steps">
              {state.steps.map((step) => (
                <li key={step.id} className={`breakdown-step breakdown-step--${step.status}`}>
                  <span className="breakdown-step__mark" aria-hidden="true">
                    {step.status === 'active' ? <span className="breakdown-step__spinner" />
                      : step.status === 'done' ? '✓'
                      : step.status === 'skipped' ? '—'
                      : step.status === 'error' ? '!' : '·'}
                  </span>
                  <span className="breakdown-step__body">
                    <strong>{step.label}</strong>
                    {step.detail && <small>{step.detail}</small>}
                  </span>
                </li>
              ))}
            </ol>
          </section>
        )}
        {state.result && !state.loading && (
          <div className="panel" style={{ marginTop: 18 }}>
            <div className="panel-title"><IconFire size={14} /> 拆解结果</div>
            <div className="skill-body-md" dangerouslySetInnerHTML={{ __html: renderMarkdown(state.result) }} />
          </div>
        )}
      </div>

      {toast && <div className="toast ok"><span className="toast-icon">✓</span>{toast}</div>}
    </div>
  );
}
