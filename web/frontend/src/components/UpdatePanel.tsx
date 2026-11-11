import { useCallback, useEffect, useRef, useState } from 'react';
import {
  applyUpdate, checkUpdate, fetchUpdateInfo, fetchUpdateStatus,
} from '../lib/api';
import type { UpdateInfo, UpdateJob, UpdateStepState } from '../lib/api';

const ACTIVE_STATES = new Set(['running', 'restarting']);

function shortCommit(value: string): string {
  return value ? value.slice(0, 8) : '—';
}

function fmtTime(value: number | null): string {
  if (!value) return '—';
  const date = new Date(value * 1000);
  return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}:${String(date.getSeconds()).padStart(2, '0')}`;
}

function stepMark(state: UpdateStepState): string {
  if (state === 'done') return '✓';
  if (state === 'fail') return '!';
  if (state === 'skipped') return '–';
  if (state === 'active') return '•';
  return '';
}

function jobPill(job: UpdateJob | undefined): { text: string; cls: string } {
  if (!job || job.state === 'idle') return { text: '尚未更新', cls: 'off' };
  if (job.state === 'running') return { text: '更新中', cls: 'warn' };
  if (job.state === 'restarting') return { text: '重启中', cls: 'warn' };
  if (job.state === 'ok') return { text: '最近一次成功', cls: 'ok' };
  return { text: '最近一次失败', cls: 'fail' };
}

export default function UpdatePanel() {
  const [info, setInfo] = useState<UpdateInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [checking, setChecking] = useState(false);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState('');
  const [pollMisses, setPollMisses] = useState(0);
  const aliveRef = useRef(true);
  const previousJobState = useRef<string>('idle');

  const loadLocal = useCallback(async () => {
    try {
      const data = await fetchUpdateInfo();
      if (aliveRef.current) {
        setInfo(data);
        setError('');
      }
    } catch (e) {
      if (aliveRef.current) setError(e instanceof Error ? e.message : '读取更新状态失败');
    } finally {
      if (aliveRef.current) setLoading(false);
    }
  }, []);

  const checkRemote = useCallback(async () => {
    setChecking(true);
    setError('');
    try {
      const data = await checkUpdate();
      if (aliveRef.current) setInfo(data);
    } catch (e) {
      if (aliveRef.current) setError(e instanceof Error ? e.message : '检查更新失败');
    } finally {
      if (aliveRef.current) setChecking(false);
    }
  }, []);

  useEffect(() => {
    aliveRef.current = true;
    void loadLocal().then(() => {
      if (aliveRef.current) void checkRemote();
    });
    return () => { aliveRef.current = false; };
  }, [loadLocal, checkRemote]);

  const active = !!info && ACTIVE_STATES.has(info.job.state);

  useEffect(() => {
    const state = info?.job.state || 'idle';
    if (ACTIVE_STATES.has(previousJobState.current) && !ACTIVE_STATES.has(state) && state !== 'idle') {
      void loadLocal().then(() => {
        if (aliveRef.current) void checkRemote();
      });
    }
    previousJobState.current = state;
  }, [info?.job.state, loadLocal, checkRemote]);

  useEffect(() => {
    if (!active) return undefined;
    let stopped = false;
    let busy = false;
    let misses = 0;
    const timer = window.setInterval(async () => {
      if (busy || stopped) return;
      busy = true;
      try {
        const job = await fetchUpdateStatus();
        if (stopped) return;
        misses = 0;
        setPollMisses(0);
        setInfo((current) => current ? { ...current, job } : current);
        if (!ACTIVE_STATES.has(job.state)) window.clearInterval(timer);
      } catch {
        if (stopped) return;
        misses += 1;
        setPollMisses(misses);
        if (misses >= 4) {
          setInfo((current) => current && ACTIVE_STATES.has(current.job.state)
            ? {
                ...current,
                job: {
                  ...current.job,
                  state: 'restarting',
                  detail: '服务正在重启，连接暂时中断；恢复后会自动继续显示结果。',
                },
              }
            : current);
        }
      } finally {
        busy = false;
      }
    }, 1500);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [active]);

  const beginUpdate = useCallback(async () => {
    setStarting(true);
    setError('');
    try {
      const job = await applyUpdate(true);
      setInfo((current) => current ? { ...current, canApply: false, job } : current);
    } catch (e) {
      setError(e instanceof Error ? e.message : '启动更新失败');
    } finally {
      setStarting(false);
    }
  }, []);

  if (loading && !info) {
    return <section className="st-sec active"><div className="board"><div className="empty"><span className="spin" /> 正在读取版本信息…</div></div></section>;
  }

  const job = info?.job;
  const pill = jobPill(job);
  const blocked = info?.blockedReason || '';
  const updateText = info?.updateAvailable
    ? info.fastForward
      ? `发现新版本 ${shortCommit(info.remoteCommit)}`
      : '远程版本与本地提交不一致'
    : info?.remoteCommit
      ? '已是最新版本'
      : '尚未检查远程版本';
  const restarting = job?.state === 'restarting' || pollMisses >= 4;

  return (
    <section className="st-sec active update-panel">
      <div className="panel-top">
        <span className={`pill ${pill.cls}`}><span className="dot" />{pill.text}</span>
        <span className="desc">固定从 nan-boop/Easel 的 main 分支更新，自动安装依赖并构建前端</span>
        <span className="spacer" />
        <button className="btn btn-sm" disabled={checking || starting || active} onClick={() => void checkRemote()}>
          {checking ? '检查中…' : '检查更新'}
        </button>
      </div>

      <div className="update-hero">
        <div className="update-version">
          <span className="update-kicker">Easel {info?.version ? `v${info.version}` : ''}</span>
          <div className="update-title">{updateText}</div>
          <div className="update-source">{info?.source || 'https://github.com/nan-boop/Easel.git'} · {info?.branch || 'main'}</div>
        </div>
        <div className="update-commits">
          <div><span>当前版本</span><code>{info?.currentShort || '—'}</code></div>
          <div><span>远程版本</span><code>{info?.remoteShort || '—'}</code></div>
        </div>
        <button
          className="btn btn-primary update-apply"
          disabled={!info?.canApply || starting || active || checking}
          onClick={() => void beginUpdate()}
        >
          {starting ? '正在启动…' : active ? '更新进行中' : '更新并重启'}
        </button>
      </div>

      {blocked && <div className="update-blocked">{blocked}</div>}
      {error && <div className="env-error">{error}</div>}

      {!!info?.dirtyFiles.length && (
        <div className="update-dirty">
          <strong>未提交文件（{info.dirtyFiles.length}）</strong>
          <span>{info.dirtyFiles.slice(0, 8).join('、')}{info.dirtyFiles.length > 8 ? '…' : ''}</span>
        </div>
      )}

      {job && job.state !== 'idle' && (
        <div className="update-job">
          <div className="update-job-head">
            <div>
              <strong>{job.state === 'ok' ? '更新完成' : job.state === 'fail' ? '更新失败' : restarting ? '正在重启服务' : '正在更新'}</strong>
              <span>{job.detail || (job.state === 'fail' ? job.error : '')}</span>
            </div>
            <div className="update-times">
              {job.startedAt ? `开始 ${fmtTime(job.startedAt)}` : ''}
              {job.endedAt ? ` · 结束 ${fmtTime(job.endedAt)}` : ''}
            </div>
          </div>

          <div className="update-steps">
            {job.steps.map((step) => (
              <div className={`update-step ${step.state}`} key={step.id}>
                <span className="update-step-mark">{stepMark(step.state)}</span>
                <span className="update-step-name">{step.label}</span>
                <span className="update-step-detail">{step.detail}</span>
              </div>
            ))}
          </div>

          {restarting && !job.restart.ok && (
            <div className="update-restarting">
              <span className="spin" /> Web 服务重启期间页面会短暂断开，恢复后任务结果会自动回来。
            </div>
          )}

          {job.state === 'ok' && job.restart.ok && (
            <div className="update-done">
              <span>新版本已生效。刷新页面以使用最新前端资源。</span>
              <button className="btn btn-sm" onClick={() => window.location.reload()}>刷新页面</button>
            </div>
          )}

          {job.state === 'fail' && (
            <div className="update-fail-note">
              更新失败。未进入重启阶段时会尽量回滚源码；详细日志在 <code>outputs/_update/update.log</code>，
              重启异常另见 <code>outputs/_update/web-restart.log</code>。
            </div>
          )}

          {!!job.logs.length && (
            <details className="update-log">
              <summary>查看执行日志（最近 {job.logs.length} 行）</summary>
              <pre>{job.logs.join('\n')}</pre>
            </details>
          )}
        </div>
      )}

      <div className="update-safety">
        <strong>更新范围</strong>
        <span>只快进 Fork 的代码提交；不会覆盖 <code>.env</code>、<code>outputs/</code>、<code>profiles/</code>、登录态和个人画像。工作区有未提交修改时会拒绝更新。</span>
      </div>
    </section>
  );
}
