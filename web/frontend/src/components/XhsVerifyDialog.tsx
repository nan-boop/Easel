import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchXhsVerifyStatus, retryXhsTrends, startXhsVerify } from '../lib/api';
import type { XhsVerifyStatus } from '../lib/api';
import type { Page } from './Sidebar';

interface Props {
  onNavigate: (page: Page) => void;
}

const TERMINAL = new Set(['success', 'closed', 'timeout', 'failed', 'busy']);

export default function XhsVerifyDialog({ onNavigate }: Props) {
  const [open, setOpen] = useState(false);
  const [status, setStatus] = useState<XhsVerifyStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const retried = useRef(false);

  const load = useCallback(async () => {
    try {
      const next = await fetchXhsVerifyStatus();
      setStatus(next);
      setError('');
      return next;
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : '无法读取验证状态');
      return null;
    }
  }, []);

  useEffect(() => {
    let timer = 0;
    const onRequired = () => {
      window.clearTimeout(timer);
      const reveal = () => {
        if (document.querySelector('.overlay')) {
          timer = window.setTimeout(reveal, 800);
          return;
        }
        setOpen(true);
        setError('');
        retried.current = false;
        void load();
      };
      timer = window.setTimeout(reveal, 120);
    };
    window.addEventListener('easel:xhs-verify', onRequired);
    return () => {
      window.removeEventListener('easel:xhs-verify', onRequired);
      window.clearTimeout(timer);
    };
  }, [load]);

  useEffect(() => {
    if (!open) return;
    void load();
    const timer = window.setInterval(() => { void load(); }, 2000);
    return () => window.clearInterval(timer);
  }, [open, load]);

  const retry = useCallback(async () => {
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      await retryXhsTrends();
      window.dispatchEvent(new Event('easel:trends-refresh'));
      setOpen(false);
      setStatus(null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : '重新获取失败，请稍后重试');
    } finally {
      setBusy(false);
    }
  }, [busy]);

  useEffect(() => {
    if (!open || status?.state !== 'success' || retried.current) return;
    retried.current = true;
    const timer = window.setTimeout(() => { void retry(); }, 900);
    return () => window.clearTimeout(timer);
  }, [open, status?.state, retry]);

  if (!open) return null;

  const cause = status?.cause || 'required';
  const state = status?.state || 'starting';
  const running = status?.running === true;
  const waiting = state === 'starting' || state === 'waiting' || (state === 'required' && running);
  const success = state === 'success';
  const copy = success
    ? '验证完成，正在重新获取小红书热点…'
    : cause === 'blocked'
      ? '小红书限制了当前网络。已打开浏览器窗口，请按页面提示处理，必要时切换网络后重试。'
      : cause === 'login_required'
        ? '小红书登录态已失效。已打开浏览器窗口，请重新登录；也可以去账号页重新扫码。'
        : waiting
          ? '小红书触发了安全验证。已打开浏览器窗口，请完成滑块或验证码，然后回到这里点击「我已验证，重新获取」。'
          : state === 'required'
            ? '小红书的验证窗口还未打开。点击「重新打开验证窗口」继续完成验证。'
          : '验证窗口已关闭或启动失败。可以重新打开窗口继续验证。';

  return (
    <div className="overlay" role="presentation">
      <div className="modal xhs-verify-modal" role="dialog" aria-modal="true" aria-labelledby="xhs-verify-title">
        <div className="xhs-verify-head">
          <span className={`xhs-verify-dot ${success ? 'ok' : TERMINAL.has(state) ? 'warn' : 'live'}`} />
          <h2 id="xhs-verify-title">小红书安全验证</h2>
        </div>
        <p className="xhs-verify-copy">{copy}</p>
        {error && <div className="xhs-verify-error" role="alert">{error}</div>}
        <div className="xhs-verify-status">
          <span>浏览器窗口</span>
          <strong>{success ? '验证成功' : status?.running ? '等待你完成验证' : '未在运行'}</strong>
        </div>
        <div className="xhs-verify-actions">
          {cause === 'login_required' && (
            <button className="btn btn-sm" onClick={() => { setOpen(false); onNavigate('accounts'); }}>
              去账号页登录
            </button>
          )}
          <button className="btn btn-sm" disabled={busy} onClick={() => {
            setBusy(true);
            setError('');
            startXhsVerify().then(setStatus).catch((e: unknown) => {
              setError(e instanceof Error ? e.message : '无法打开验证窗口');
            }).finally(() => setBusy(false));
          }}>
            重新打开验证窗口
          </button>
          <button className="btn btn-sm btn-primary" disabled={busy} onClick={() => { void retry(); }}>
            {busy ? '处理中…' : success ? '正在重新获取…' : '我已验证，重新获取'}
          </button>
          <button className="btn btn-sm xhs-verify-later" onClick={() => setOpen(false)}>稍后处理</button>
        </div>
      </div>
    </div>
  );
}
