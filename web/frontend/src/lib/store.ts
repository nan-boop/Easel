import type { UploadedFile, ChatQuestion } from './api';
import {
  fetchDraftState,
  fetchSessionState,
  saveActiveSession,
  saveDraftState,
  syncSessionState,
} from './api';

export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
  agentContent?: string; // 仅发给 Agent 的增强消息（如附件路径），不在对话页面展示
  attachments?: UploadedFile[]; // 结构化附件引用；仅用于请求/重试，不在消息气泡展示
  thinking?: string;   // 模型思考过程（隐思考），流式结束后持久保留
  activity?: string;   // 工具/执行活动步骤（换行分隔），持久保留
  questions?: ChatQuestion[];
}

export interface ChatSession {
  id: string;
  title: string;
  messages: ChatMessage[];
  persona?: string;
  created: number;
  sessionKey?: string;  // OpenClaw 的 session key，用于后端删除
  pendingTurnId?: string; // 进行中的可重连 job；浏览器重开后继续按 eventId 续流
  archived?: boolean;   // 归档：从 History 主列表移到「已归档」区
  updated?: number;     // 本地/服务端最后修改时间，用于跨浏览器合并
}

/** 进行中的流式状态（存于 App，不随页面切换/ChatPage 卸载而丢失）。 */
export interface StreamState {
  content: string;
  thinking: string;
  activity: string;
  stillWorking?: string;   // 防呆心跳提示（未卡住）；独立于 activity，不覆盖真实状态
  questions: ChatQuestion[];   // ask_user 问答题卡片（进行中）
}

/** 发布中心草稿：持久化到 localStorage，切页/刷新都不丢。 */
export interface PublishDraft {
  title: string;
  body: string;
  platforms: string[];
  overrides: Record<string, string>;
  tags: string;   // 话题标签，逗号分隔（小红书绑话题；其它平台按需写入）
  updatedAt?: number;
}
const PUBLISH_KEY = 'easel_publish_draft';
const PREVIOUS_BRAND = ['post', 'craft'].join('');

let localClock = 0;
function nextTimestamp(): number {
  localClock = Math.max(Date.now(), localClock + 1);
  return localClock;
}

function notifySyncError(): void {
  if (typeof window !== 'undefined') {
    queueMicrotask(() => window.dispatchEvent(new Event('easel-session-sync-error')));
  }
}

function notifySyncOk(): void {
  if (typeof window !== 'undefined') {
    queueMicrotask(() => window.dispatchEvent(new Event('easel-session-sync-ok')));
  }
}

let draftSyncReady = false;
let draftSyncTimer: ReturnType<typeof setTimeout> | null = null;
let pendingDraft: PublishDraft | null = null;
let draftSyncChain: Promise<void> = Promise.resolve();

function readMigratedLocalValue(key: string, suffix: string): string | null {
  const current = localStorage.getItem(key);
  if (current !== null) return current;
  const previousKey = `${PREVIOUS_BRAND}_${suffix}`;
  const previous = localStorage.getItem(previousKey);
  if (previous !== null) {
    localStorage.setItem(key, previous);
    localStorage.removeItem(previousKey);
  }
  return previous;
}

const PUBLISH_DEFAULT: PublishDraft = {
  title: '', body: '', platforms: ['xiaohongshu', 'douyin'], overrides: {}, tags: '',
};
export function loadPublishDraft(): PublishDraft {
  try {
    const raw = readMigratedLocalValue(PUBLISH_KEY, 'publish_draft');
    if (!raw) return { ...PUBLISH_DEFAULT };
    return { ...PUBLISH_DEFAULT, ...(JSON.parse(raw) as PublishDraft) };
  } catch {
    return { ...PUBLISH_DEFAULT };
  }
}
export function savePublishDraft(d: PublishDraft): void {
  const next = { ...d, updatedAt: nextTimestamp() };
  try { localStorage.setItem(PUBLISH_KEY, JSON.stringify(next)); } catch { /* quota */ }
  if (!draftSyncReady) return;
  pendingDraft = next;
  scheduleDraftFlush();
}

export async function hydratePublishDraft(local: PublishDraft): Promise<PublishDraft> {
  try {
    const remote = await fetchDraftState();
    draftSyncReady = true;
    const remoteUpdated = remote.updatedAt || 0;
    const localUpdated = local.updatedAt || 0;
    if (remote.draft && remoteUpdated >= localUpdated) {
      const next = { ...PUBLISH_DEFAULT, ...remote.draft, updatedAt: remoteUpdated };
      try { localStorage.setItem(PUBLISH_KEY, JSON.stringify(next)); } catch { /* quota */ }
      return next;
    }
    if (!remote.draft && (local.title || local.body || local.tags)) {
      pendingDraft = { ...local, updatedAt: nextTimestamp() };
      scheduleDraftFlush();
    }
    return local;
  } catch {
    draftSyncReady = true;
    notifySyncError();
    return local;
  }
}

function scheduleDraftFlush(): void {
  if (draftSyncTimer) return;
  draftSyncTimer = setTimeout(() => {
    draftSyncTimer = null;
    flushDraftSync();
  }, 350);
}

function flushDraftSync(keepalive = false): void {
  if (draftSyncTimer) {
    clearTimeout(draftSyncTimer);
    draftSyncTimer = null;
  }
  const draft = pendingDraft;
  pendingDraft = null;
  if (!draft) return;
  draftSyncChain = draftSyncChain
    .then(async () => {
      await saveDraftState(draft, keepalive);
      notifySyncOk();
    })
    .catch(() => {
      if (!pendingDraft) pendingDraft = draft;
      notifySyncError();
    });
}

const STORAGE_KEY = 'easel_sessions';
const ACTIVE_KEY = 'easel_active_session';
const MAX_SESSIONS = 100;
const TITLE_MAX_CHARS = 24;
const SESSION_SYNC_DELAY = 120;
const ACTIVE_SYNC_DELAY = 100;

interface SessionObservation {
  signature: string;
  updated: number;
}

let observedSessions = new Map<string, SessionObservation>();
let observedInitialized = false;
let sessionSyncReady = false;
let sessionSyncTimer: ReturnType<typeof setTimeout> | null = null;
let sessionSyncChain: Promise<void> = Promise.resolve();
const pendingSessionUpserts = new Map<string, ChatSession>();
const pendingSessionDeletes = new Set<string>();
let activeSyncTimer: ReturnType<typeof setTimeout> | null = null;
let activeSyncValue: string | null | undefined;
let activeSyncChain: Promise<void> = Promise.resolve();

function sessionSignature(session: ChatSession): string {
  return JSON.stringify({
    title: session.title || '',
    messages: session.messages || [],
    persona: session.persona || '',
    created: session.created || 0,
    sessionKey: session.sessionKey || '',
    pendingTurnId: session.pendingTurnId || '',
    archived: Boolean(session.archived),
  });
}

function setObservedSessions(sessions: ChatSession[]): void {
  observedSessions = new Map(sessions.map((session) => [
    session.id,
    {
      signature: sessionSignature(session),
      updated: Number(session.updated) || Number(session.created) || nextTimestamp(),
    },
  ]));
  observedInitialized = true;
}

/** 上次活跃会话 id：重开网页时据此续接上次对话（而不是丢进新空会话）。 */
export function loadActiveId(): string | null {
  try { return readMigratedLocalValue(ACTIVE_KEY, 'active_session'); } catch { return null; }
}
export function saveActiveId(id: string | null): void {
  try {
    if (id) localStorage.setItem(ACTIVE_KEY, id);
    else localStorage.removeItem(ACTIVE_KEY);
  } catch { /* quota */ }
  activeSyncValue = id;
  scheduleActiveSync();
}

function scheduleActiveSync(): void {
  if (!sessionSyncReady || activeSyncTimer) return;
  activeSyncTimer = setTimeout(() => {
    activeSyncTimer = null;
    flushActiveSync();
  }, ACTIVE_SYNC_DELAY);
}

function flushActiveSync(keepalive = false): void {
  if (activeSyncTimer) {
    clearTimeout(activeSyncTimer);
    activeSyncTimer = null;
  }
  if (!sessionSyncReady || activeSyncValue === undefined) return;
  const value = activeSyncValue;
  activeSyncValue = undefined;
  activeSyncChain = activeSyncChain
    .then(async () => {
      await saveActiveSession(value, keepalive);
      notifySyncOk();
    })
    .catch(() => {
      if (activeSyncValue === undefined) activeSyncValue = value;
      notifySyncError();
    });
}

function generateId(): string {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
}

export function loadSessions(): ChatSession[] {
  try {
    const raw = readMigratedLocalValue(STORAGE_KEY, 'sessions');
    if (!raw) return [];
    const sessions = (JSON.parse(raw) as ChatSession[]).filter((s) => s && typeof s === 'object' && s.id);
    // 兼容旧版本：附件内部指令曾被直接存进用户消息，加载时拆出并隐藏。
    // 防御：损坏/缺字段的会话（旧版写入或写中断）恢复成可用形态，绝不让渲染期崩。
    return sessions.map((session) => ({
      ...session,
      messages: Array.isArray(session.messages)
        ? session.messages.map((message) => {
            if (message?.role !== 'user' || message.agentContent || !message.content?.includes('【附件素材】')) {
              return message;
            }
            const [visible = ''] = message.content.split('【附件素材】', 1);
            return {
              ...message,
              content: visible.trim(),
              agentContent: message.content,
            };
          })
        : [],
    }));
  } catch {
    return [];
  }
}

/** 保存前清理：只保留最新 1 个空会话（避免空会话无限堆积），并封顶总数。 */
function prune(sessions: ChatSession[]): ChatSession[] {
  let keptEmpty = false;
  const pruned = sessions.filter((s) => {
    if (Array.isArray(s.messages) && s.messages.length > 0) return true;
    if (keptEmpty) return false;
    keptEmpty = true;
    return true;
  });
  return pruned.slice(0, MAX_SESSIONS);
}

function writeSessionCache(sessions: ChatSession[]): boolean {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(prune(sessions)));
    return true;
  } catch {
    if (typeof window !== 'undefined') {
      queueMicrotask(() => window.dispatchEvent(new Event('easel-session-save-error')));
    }
    return false;
  }
}

function queueSessionUpsert(session: ChatSession): void {
  pendingSessionDeletes.delete(session.id);
  pendingSessionUpserts.set(session.id, session);
  scheduleSessionSync();
}

function queueSessionDelete(sessionId: string): void {
  pendingSessionUpserts.delete(sessionId);
  pendingSessionDeletes.add(sessionId);
  scheduleSessionSync();
}

let sessionRetryDelay = 1000;
function scheduleSessionSync(delay = SESSION_SYNC_DELAY): void {
  if (!sessionSyncReady || sessionSyncTimer) return;
  sessionSyncTimer = setTimeout(() => {
    sessionSyncTimer = null;
    flushSessionSync();
  }, delay);
}

function flushSessionSync(keepalive = false): void {
  if (sessionSyncTimer) {
    clearTimeout(sessionSyncTimer);
    sessionSyncTimer = null;
  }
  if (!sessionSyncReady || (!pendingSessionUpserts.size && !pendingSessionDeletes.size)) return;
  const sessions = [...pendingSessionUpserts.values()];
  const deleted = [...pendingSessionDeletes];
  pendingSessionUpserts.clear();
  pendingSessionDeletes.clear();
  sessionSyncChain = sessionSyncChain
    .then(async () => {
      await syncSessionState(sessions, deleted, undefined, false, keepalive);
      sessionRetryDelay = 1000;
      notifySyncOk();
    })
    .catch(() => {
      for (const session of sessions) {
        if (!pendingSessionUpserts.has(session.id) && !pendingSessionDeletes.has(session.id)) {
          pendingSessionUpserts.set(session.id, session);
        }
      }
      for (const sessionId of deleted) {
        if (!pendingSessionUpserts.has(sessionId)) pendingSessionDeletes.add(sessionId);
      }
      notifySyncError();
      const retry = sessionRetryDelay;
      sessionRetryDelay = Math.min(sessionRetryDelay * 2, 30_000);
      scheduleSessionSync(retry);
    });
}

export function saveSessions(sessions: ChatSession[]): boolean {
  if (!observedInitialized) setObservedSessions(loadSessions());
  const seen = new Set<string>();
  const stamped = sessions.map((session) => {
    seen.add(session.id);
    const previous = observedSessions.get(session.id);
    const signature = sessionSignature(session);
    const updated = !previous || previous.signature !== signature
      ? nextTimestamp()
      : previous.updated;
    return { ...session, updated };
  });
  const ok = writeSessionCache(stamped);

  if (sessionSyncReady) {
    for (const sessionId of observedSessions.keys()) {
      if (!seen.has(sessionId)) queueSessionDelete(sessionId);
    }
    for (const session of stamped) {
      const previous = observedSessions.get(session.id);
      if (!previous || previous.signature !== sessionSignature(session)) {
        queueSessionUpsert(session);
      }
    }
  }
  setObservedSessions(stamped);
  return ok;
}

export interface HydratedSessions {
  sessions: ChatSession[];
  activeSessionId: string | null;
  remote: boolean;
}

/** 合并本地缓存与服务端唯一真源；服务端为空时自动迁移旧 localStorage 数据。 */
export async function hydrateSessions(localInput?: ChatSession[]): Promise<HydratedSessions> {
  const local = localInput ?? loadSessions();
  let remote;
  try {
    remote = await fetchSessionState();
  } catch {
    setObservedSessions(local);
    sessionSyncReady = true;
    writeSessionCache(local);
    notifySyncError();
    return { sessions: local, activeSessionId: loadActiveId(), remote: false };
  }

  const tombstoned = new Set(remote.deletedSessionIds || []);
  const remoteById = new Map(remote.sessions.map((session) => [session.id, session]));
  const merged = remote.sessions.filter((session) => !tombstoned.has(session.id));
  const mergedIndex = new Map(merged.map((session, index) => [session.id, index]));
  const uploads: ChatSession[] = [];
  const localSeen = new Set<string>();

  for (const session of local) {
    if (!session?.id || tombstoned.has(session.id) || localSeen.has(session.id)) continue;
    localSeen.add(session.id);
    const remoteSession = remoteById.get(session.id);
    if (remoteSession) {
      const localUpdated = Number(session.updated) || Number(session.created) || 0;
      const remoteUpdated = Number(remoteSession.updated) || Number(remoteSession.created) || 0;
      if (localUpdated > remoteUpdated) {
        const index = mergedIndex.get(session.id);
        if (index !== undefined) merged[index] = session;
        uploads.push(session);
      }
      continue;
    }
    merged.push(session);
    mergedIndex.set(session.id, merged.length - 1);
    uploads.push(session);
  }

  merged.sort((a, b) => (Number(b.updated) || Number(b.created) || 0)
    - (Number(a.updated) || Number(a.created) || 0));
  const validIds = new Set(merged.map((session) => session.id));
  const localActive = loadActiveId();
  const activeSessionId = remote.activeSessionId && validIds.has(remote.activeSessionId)
    ? remote.activeSessionId
    : localActive && validIds.has(localActive) ? localActive : null;

  pendingSessionUpserts.clear();
  pendingSessionDeletes.clear();
  setObservedSessions(merged);
  sessionSyncReady = true;
  writeSessionCache(merged);
  for (const session of uploads) queueSessionUpsert(session);
  scheduleActiveSync();
  return { sessions: merged, activeSessionId, remote: true };
}

if (typeof window !== 'undefined') {
  window.addEventListener('pagehide', () => {
    flushSessionSync(true);
    flushActiveSync(true);
    flushDraftSync(true);
  });
}

export function createSession(persona?: string): ChatSession {
  return {
    id: generateId(),
    title: 'New Chat',
    messages: [],
    persona,
    created: Date.now(),
  };
}

type TitleIntent = 'issue' | 'create' | 'optimize' | 'publish' | 'inspect' | 'general';

function titleIntent(text: string): TitleIntent {
  // 明确请求比“有点问题”更能表达用户意图，所以 issue 放在最后。
  if (/(?:优化|改进|调整|完善|增强|多样|随机)/.test(text)) return 'optimize';
  if (/(?:发布|投稿|分发)|(?:上传.*(?:平台|账号|小红书|抖音|B站|bilibili))/i.test(text)) return 'publish';
  if (/(?:制作|生成|创建|设计|写一|做一|剪辑|合成)/.test(text)) return 'create';
  if (/(?:检查|排查|分析|看看|查看|确认|什么逻辑)/.test(text)) return 'inspect';
  if (/(?:bug|修复|解决|问题|异常|报错|失败|卡住|断开|不对|不生效|混乱|拉伸)/i.test(text)) return 'issue';
  return 'general';
}

function semanticTopic(text: string, fallback: string): string {
  if (/(?:历史)?会话.*(?:命名|标题|主题|名字)|(?:命名|标题|主题|名字).*(?:历史)?会话/.test(text)) return '历史会话标题';
  if (/(?:模型)?回答(?:完成|完).*?(?:运行|断开)|(?:运行|断开).*?(?:模型)?回答(?:完成|完)/.test(text)) return '对话完成状态';
  if (/(?:paper-explainer|paper explainer).*(?:skill|逻辑|流程)|(?:skill|逻辑|流程).*(?:paper-explainer|paper explainer)/i.test(text)) return 'paper-explainer 逻辑';
  if (/(?:论文|paper).*(?:ppt|slide).*(?:拉伸|移动|缩放|画面)|(?:拉伸|移动|缩放).*(?:ppt|slide)/i.test(text)) return '论文 PPT 画面';
  if (/(?:论文|paper).*(?:ppt|slide|幻灯|讲解)|(?:ppt|slide|幻灯).*(?:论文|paper)/i.test(text)) return '论文讲解 PPT';
  if (/(?:附件|素材).*(?:上传|重名|多个|显示|隐藏)|(?:上传|重名).*(?:附件|素材)/.test(text)) return '附件上传';
  if (/(?:小红书|xhs).*种草.*文案/i.test(text)) return '小红书种草文案';
  if (/(?:小红书|xhs).*(?:文案|笔记|卡片)/i.test(text)) return '小红书内容';
  if (/交接文档.*项目|项目.*交接文档/.test(text)) return '项目交接';
  if (/视频.*(?:横版|竖版|画幅)|(?:横版|竖版|画幅).*视频/.test(text)) return '视频画幅';
  if (/配置.*模型|模型.*配置/.test(text)) return '模型配置';

  const quoted = text.match(/[「『“"]([^」』”"]{2,24})[」』”"]/u)?.[1];
  if (quoted && /(?:围绕|关于|选题|主题|热点)/.test(text)) return quoted.trim();

  const publishTarget = text.match(/^(?:把|将)?(.{2,18}?)(?:发布|投稿|分发)(?:到|至|去|给)/)?.[1];
  if (publishTarget) return publishTarget.trim();

  return fallback
    .replace(/^(?:做|制作|生成|创建|设计|写|检查|排查|分析|优化|改进|调整|修复)(?:一个|一条|一份|个|条|份)?/, '')
    .replace(/(?:有个|出现了?|遇到)?\s*(?:bug|问题|异常|报错|有点乱).*$/i, '')
    .replace(/[，,、：:\s]+$/g, '')
    .trim() || fallback;
}

function cleanTitleClause(value: string): string {
  return value
    .replace(/^(?:(?:然后|还有|另外|对了|那个|嗯|就是|首先|先说一下|麻烦你?|请问|请你?|能不能|能否|是否可以|可不可以|你能否|你可以|我想(?:要|让你)?|想让你|帮忙|帮我|给我|先|看看|看一下|看下|查一下|查下|确认一下|确认下|我发现|我觉得)[，,、：:\s]*)+/g, '')
    .replace(/^(?:现在|目前|当前)的?/g, '')
    .replace(/[吗么呢吧啊呀哦]+[？?！!。.]?$/g, '')
    .trim();
}

function chooseFocusClause(text: string): string {
  const clauses = text.split(/[。！？?!；;\n，,]/).map(cleanTitleClause).filter(Boolean);
  if (!clauses.length) return text;
  const domain = /(?:skill|paper-explainer|会话|历史|标题|命名|附件|素材|论文|ppt|slide|视频|字幕|配音|发布|小红书|抖音|图片|模型|配置|项目|页面|前端)/i;
  const action = /(?:优化|修复|改进|调整|制作|生成|创建|发布|分析|排查|检查)/;
  return clauses.reduce((best, clause) => {
    const score = (domain.test(clause) ? 4 : 0) + (action.test(clause) ? 2 : 0)
      - (/(?:有可能|是不是|为什么|怎么回事|有点)/.test(clause) ? 1 : 0)
      - Math.max(0, Array.from(clause).length - 28) / 20;
    const bestScore = (domain.test(best) ? 4 : 0) + (action.test(best) ? 2 : 0)
      - (/(?:有可能|是不是|为什么|怎么回事|有点)/.test(best) ? 1 : 0)
      - Math.max(0, Array.from(best).length - 28) / 20;
    return score > bestScore ? clause : best;
  }, clauses[0]);
}

/** 从首条提问提取“主题对象 + 动作”；确定性生成，不请求模型或后端。 */
export function generateSessionTitle(message: string, _seed = message): string {
  const attachmentMarker = '【附件素材】';
  const [question = '', attachmentBlock = ''] = message.split(attachmentMarker, 2);
  let text = question
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/https?:\/\/\S+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();

  if (!text && attachmentBlock) {
    const path = attachmentBlock.match(/outputs\/([^\s]+)/)?.[1] || '';
    const filename = path.split('/').pop() || '上传素材';
    text = `处理附件 ${filename}`;
  }

  text = cleanTitleClause(text);
  const focusClause = chooseFocusClause(text);
  const baseTitle = focusClause
    .replace(/^(.+?)配置的?是什么模型$/i, '$1模型配置')
    .replace(/^对(.+?)进行/g, '$1')
    .replace(/然后(?:再)?(?:对)?/g, '')
    .replace(/进行|这个|一下/g, '')
    .replace(/(?:怎么|如何|怎样)(?:去)?/g, '')
    .replace(/(?:是不是|是否|是什么|有哪些|有啥)$/g, '')
    .replace(/[吗么呢吧啊呀]+$/g, '')
    .replace(/[，,、：:\s]+$/g, '')
    .trim();

  const core = Array.from(semanticTopic(text, baseTitle || '新对话'))
    .slice(0, 18).join('')
    .replace(/(?:问题排查|故障分析|异常|问题|优化|改进|分析|检查|制作|生成|发布)$/g, '')
    .trim() || '新对话';
  const intent = titleIntent(text);
  const titles: Record<TitleIntent, string> = {
    issue: `${core}问题排查`,
    create: /(?:文案|脚本|方案)$/.test(core) ? core : `${core}制作`,
    optimize: `${core}优化`,
    publish: `${core}发布`,
    inspect: `${core}分析`,
    general: core,
  };
  const title = titles[intent] || core;
  const chars = Array.from(title);
  return chars.length > TITLE_MAX_CHARS
    ? `${chars.slice(0, TITLE_MAX_CHARS).join('')}…`
    : title;
}

export function updateSessionTitle(session: ChatSession): void {
  const first = Array.isArray(session.messages) ? session.messages[0] : undefined;
  if (first && session.title === 'New Chat') {
    session.title = generateSessionTitle(first.content || '', session.id);
  }
}
