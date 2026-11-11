import { Component, useState, useEffect, useCallback, useRef } from 'react';
import type { ReactNode } from 'react';
import { fetchTrendSources, fetchTrends, createIdea } from '../lib/api';
import type { TrendGroup, TrendSource } from '../lib/api';
import { IconFire, IconRefresh, IconBookmark, IconCheck, IconChat } from './icons';
import TrendSourcesDialog from './TrendSourcesDialog';
import ContentPanel from './ContentPanel';

const SCOPE_PRESETS = ['3C数码', '加湿器', '手持风扇'];
const MAX_SCOPES = 8;

function splitScopes(value: string): string[] {
  const result: string[] = [];
  const seen = new Set<string>();
  for (const raw of value.split(/[,，;；|\n]+/)) {
    const scope = raw.trim().replace(/^#+/, '').trim();
    const key = scope.toLocaleLowerCase();
    if (!scope || scope.length > 40 || seen.has(key)) continue;
    seen.add(key);
    result.push(scope);
    if (result.length >= MAX_SCOPES) break;
  }
  return result;
}

interface TrendsPageProps {
  onUseTopic: (title: string) => void;   // 一键做成内容 → 跳 chat
}

class TrendBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    return this.state.failed ? <div className="page-scroll trends-page"><div className="notice-error" role="alert">热点页面暂时无法显示，其他工作台功能仍可使用。<button className="btn btn-sm" onClick={() => this.setState({ failed: false })}>重新加载热点</button></div></div> : this.props.children;
  }
}

export default function TrendsPage(props: TrendsPageProps) {
  return <TrendBoundary><TrendsPanel {...props} /></TrendBoundary>;
}

function TrendsPanel({ onUseTopic }: TrendsPageProps) {
  const [sources, setSources] = useState<TrendSource[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [ready, setReady] = useState(false);
  const [groups, setGroups] = useState<TrendGroup[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [updated, setUpdated] = useState(0);
  const [saved, setSaved] = useState<Set<string>>(new Set());
  const [managerOpen, setManagerOpen] = useState(false);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [saveError, setSaveError] = useState('');
  const [scopes, setScopes] = useState<string[]>(() => {
    try { return splitScopes(window.localStorage.getItem('easel:trend-scopes') || ''); }
    catch { return []; }
  });
  const [scopeDraft, setScopeDraft] = useState('');
  const [scopeError, setScopeError] = useState('');
  const requestId = useRef(0);
  const discoveryId = useRef(0);
  const initialized = useRef(false);
  const scopeKey = scopes.join(',');

  const save = async (key: string, title: string, source: string) => {
    if (saved.has(key)) return;
    try {
      await createIdea({ title, source, status: 'pending' });
      setSaved((prev) => new Set(prev).add(key));
    } catch { setSaveError('收藏失败，请稍后重试。'); }
  };

  const load = useCallback((pfs: string[], activeScopes: string[]) => {
    const id = ++requestId.current;
    if (pfs.length === 0) { setGroups([]); setLoading(false); setError(''); return; }
    setGroups((prev) => prev.filter((g) => pfs.includes(g.platform)));
    setLoading(true);
    setError('');
    fetchTrends(pfs.join(','), 15, activeScopes.join(','))
      .then((d) => { if (requestId.current === id) { setGroups(d.trends); setUpdated(d.updated); setWarnings(d.warnings); } })
      .catch((e: unknown) => {
        if (requestId.current === id) setError(e instanceof Error ? e.message : '热点拉取失败，请稍后重试。');
      })
      .finally(() => { if (requestId.current === id) setLoading(false); });
  }, []);

  const discover = useCallback(() => {
    const id = ++discoveryId.current;
    requestId.current++;
    setReady(false);
    setLoading(false);
    setError('');
    return fetchTrendSources().then((d) => {
      if (discoveryId.current !== id) return;
      setSources(d.sources);
      setWarnings(d.warnings);
      const first = !initialized.current;
      initialized.current = true;
      setSelected((prev) => first ? d.sources.filter((s) => s.default).map((s) => s.id)
        : d.sources.filter((s) => prev.includes(s.id)).map((s) => s.id));
      setGroups((prev) => prev.filter((g) => d.sources.some((s) => s.id === g.platform)));
      setReady(true);
    }).catch((e: unknown) => {
      if (discoveryId.current === id) {
        setGroups([]);
        setSources([]);
        setError(e instanceof Error ? e.message : '无法加载热点数据源。');
      }
    });
  }, []);

  const invalidate = useCallback(() => { requestId.current++; discoveryId.current++; }, []);
  useEffect(() => { void discover(); return invalidate; }, [discover, invalidate]);
  useEffect(() => {
    if (!ready) return;
    const timer = window.setTimeout(() => load(selected, scopes), 350);
    return () => window.clearTimeout(timer);
  }, [load, selected, ready, scopes]);
  useEffect(() => {
    try { window.localStorage.setItem('easel:trend-scopes', scopeKey); }
    catch { /* 本地存储不可用时仍可正常筛选。 */ }
  }, [scopeKey]);
  useEffect(() => {
    const onRefresh = () => load(selected, scopes);
    window.addEventListener('easel:trends-refresh', onRefresh);
    return () => window.removeEventListener('easel:trends-refresh', onRefresh);
  }, [load, selected, scopes]);
  useEffect(() => {
    if (!groups.some(group => group.error?.code === 'busy')) return;
    const timer = window.setTimeout(() => load(selected, scopes), 1500);
    return () => window.clearTimeout(timer);
  }, [groups, selected, load, scopes]);

  const toggle = (k: string) =>
    setSelected((prev) => prev.includes(k) ? prev.filter((x) => x !== k) : [...prev, k]);
  const hasScope = (scope: string) =>
    scopes.some((item) => item.toLocaleLowerCase() === scope.toLocaleLowerCase());
  const toggleScope = (scope: string) => {
    setScopeError('');
    if (!hasScope(scope) && scopes.length >= MAX_SCOPES) {
      setScopeError(`最多选择 ${MAX_SCOPES} 个范围话题，请先取消一个。`);
      return;
    }
    setScopes((prev) => {
      const key = scope.toLocaleLowerCase();
      return prev.some((item) => item.toLocaleLowerCase() === key)
        ? prev.filter((item) => item.toLocaleLowerCase() !== key)
        : [...prev, scope];
    });
  };
  const addScopes = (event: React.FormEvent) => {
    event.preventDefault();
    const additions = splitScopes(scopeDraft);
    if (additions.length === 0) {
      setScopeError('请输入 1–40 字的范围话题，多个话题用逗号分隔。');
      return;
    }
    if (scopes.length >= MAX_SCOPES
      && additions.some((item) => !scopes.some((scope) => scope.toLocaleLowerCase() === item.toLocaleLowerCase()))) {
      setScopeError(`最多选择 ${MAX_SCOPES} 个范围话题，请先取消一个。`);
      return;
    }
    setScopes((prev) => splitScopes([...prev, ...additions].join(',')));
    setScopeDraft('');
    setScopeError('');
  };
  const scopeOptions = [
    ...SCOPE_PRESETS,
    ...scopes.filter((scope) => !SCOPE_PRESETS.some(
      (preset) => preset.toLocaleLowerCase() === scope.toLocaleLowerCase())),
  ];

  return (
    <div className="page-scroll trends-page" aria-busy={loading}>
      <div className="page-head">
        <div>
          <h1 className="page-title"><IconFire size={22} /> 热点雷达</h1>
          <p className="page-subtitle">
            汇集平台热搜与小红书真实笔记，可按产品方向筛选，挑选题后一键交给 AI 做成内容。
            {updated > 0 && <span style={{ color: 'var(--text-tertiary)' }}> · {new Date(updated * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })} 查询</span>}
          </p>
        </div>
        <div className="trend-actions"><button className="btn btn-sm" onClick={() => setManagerOpen(true)}>数据源</button>
        <button className="btn btn-sm" onClick={() => { void discover(); }} disabled={loading}>
          <IconRefresh size={14} /> {loading ? '刷新中…' : '刷新'}
        </button></div>
      </div>

      <div className="trend-platforms trend-source-controls" role="group" aria-label="选择热点数据源">
        {sources.map((p) => (
          <button key={p.id} className={`chip ${selected.includes(p.id) ? 'active' : ''}`}
            aria-pressed={selected.includes(p.id)} onClick={() => toggle(p.id)}>{p.label}</button>
        ))}
      </div>

      <section className="trend-scope-panel" aria-label="范围话题筛选">
        <div className="trend-scope-head">
          <strong>范围话题</strong>
          <span>可多选 · 最多 {MAX_SCOPES} 个</span>
        </div>
        <div className="trend-scope-chips" role="group" aria-label="选择范围话题">
          <button className={`chip ${scopes.length === 0 ? 'active' : ''}`}
            aria-pressed={scopes.length === 0} onClick={() => { setScopes([]); setScopeError(''); }}>全部</button>
          {scopeOptions.map((scope) => (
            <button key={scope} className={`chip ${hasScope(scope) ? 'active' : ''}`}
              aria-pressed={hasScope(scope)} onClick={() => toggleScope(scope)}>{scope}</button>
          ))}
        </div>
        <form className="trend-scope-form" onSubmit={addScopes}>
          <input className="field trend-scope-input" value={scopeDraft} maxLength={200}
            onChange={(event) => setScopeDraft(event.target.value)}
            placeholder="自定义范围，如：桌面好物、露营风扇（多个用逗号分隔）"
            aria-label="自定义范围话题" />
          <button className="btn btn-sm" type="submit" disabled={!scopeDraft.trim()}>添加并筛选</button>
        </form>
        {scopeError && <div className="trend-scope-error" role="alert">{scopeError}</div>}
        <div className="trend-scope-note">
          {scopes.length > 0
            ? <>当前范围：{scopes.join(' / ')}。小红书按这些词搜索热门笔记，其他来源按标题做同义词匹配。</>
            : '不选范围时展示各来源的综合热点；选择后只保留相关选题。'}
          <br />小红书同一范围的新搜索至少间隔 5 分钟；不同范围按 15 秒间隔排队，已缓存范围直接复用。
        </div>
      </section>

      {error && <div className="notice-error" role="alert">{error}</div>}
      {saveError && <div className="notice-error" role="alert">{saveError}</div>}
      {warnings.map((warning, i) => <div className="notice-error" role="status" key={i}>{warning}</div>)}
      {!ready && !error && <div className="trend-platforms" role="status">正在加载数据源…</div>}
      {ready && sources.length === 0 && <div className="trend-platforms">尚未启用数据源，请在本地热点数据源配置中添加或启用。</div>}
      {ready && sources.length > 0 && selected.length === 0 && <div className="trend-platforms">请选择上方的数据源查看热点。</div>}
      {loading && <div className="trend-platforms" role="status">正在获取热点…</div>}

      <div className="trend-grid">
        {groups.map((g) => (
          <ContentPanel key={g.platform} className="trend-col" label={`${g.label}热点列表`} header={
            <div className="trend-col-head"><span className="trend-source-label" title={g.label}>{g.label}</span><span className="trend-count">{g.items.length}</span></div>}>
            <div className="trend-source-status">
              {g.fetched_at && <span>采集于 {new Date(g.fetched_at).toLocaleString('zh-CN')}</span>}
              {g.stale && <span> · 缓存已过期</span>}
              {g.error && <div role="status">{g.error.message}{g.stale ? '，暂用上次结果' : ''}
                {['verification_required', 'ip_blocked', 'login_required'].includes(g.error.code) && (
                  <button style={{ marginLeft: 8, border: 0, background: 'none', color: 'var(--accent-end)', cursor: 'pointer' }}
                    onClick={() => window.dispatchEvent(new CustomEvent('easel:xhs-verify', { detail: g.error }))}>
                    处理验证
                  </button>
                )}
              </div>}
            </div>
              {g.items.length === 0 && !loading && (
                <div className="trend-empty">
                  {!g.ok ? '该数据源暂时不可用' : '采集成功，暂无热点'}
                </div>
              )}
              {g.items.map((it, i) => (
                <div key={it.id} className="trend-item">
                  <span className={`trend-rank ${i < 3 ? 'top' : ''}`}>{i + 1}</span>
                  <div className="trend-main">
                    <a className="trend-title" href={it.url || undefined} target="_blank" rel="noreferrer"
                      title={it.title}>{it.title}</a>
                    {it.hot && <span className="trend-hot">{it.hot}</span>}
                  </div>
                  <div className="trend-item-actions">
                    <button className="trend-save" title={saved.has(`${g.platform}:${it.id}`) ? '已收藏到选题库' : '收藏到选题库'}
                      aria-label={`${saved.has(`${g.platform}:${it.id}`) ? '已收藏' : '收藏选题'}：${it.title}`}
                      onClick={() => save(`${g.platform}:${it.id}`, it.title, g.label)}>
                      {saved.has(`${g.platform}:${it.id}`) ? <IconCheck size={14} /> : <IconBookmark size={14} />}
                    </button>
                    <button className="trend-use" title="做成内容" aria-label={`做成内容：${it.title}`}
                      onClick={() => onUseTopic(it.title)}><IconChat size={14} /></button>
                  </div>
                </div>
              ))}
          </ContentPanel>
        ))}
      </div>
      {managerOpen && <TrendSourcesDialog onClose={() => setManagerOpen(false)} onChanged={addedId => {
        void discover().then(() => { if (addedId) setSelected(prev => [...new Set([...prev, addedId])]); });
      }} />}
    </div>
  );
}
