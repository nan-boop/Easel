import { useEffect, useRef, useState } from 'react';
import {
  fetchHarnessModels, fetchHarnessSettings, saveHarnessSettings, testModelConnection,
} from '../lib/api';
import type { HarnessModel, HarnessSettingsResponse } from '../lib/api';
import SettingsSelect from './SettingsSelect';

export default function HarnessSettings({ onChange, onSelect }: {
  onChange: (runtime: string) => void;
  onSelect: (runtime: string) => void;
}) {
  const [settings, setSettings] = useState<HarnessSettingsResponse | null>(null);
  const [runtime, setRuntime] = useState('api');
  const [model, setModel] = useState('');
  const [sandbox, setSandbox] = useState('workspace-write');
  const [autoApprove, setAutoApprove] = useState(false);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState('');
  const [catalogs, setCatalogs] = useState<Record<string, HarnessModel[]>>({});
  const [modelLoading, setModelLoading] = useState(false);
  const [modelNote, setModelNote] = useState('');
  const [refresh, setRefresh] = useState(0);
  const [settingsLoading, setSettingsLoading] = useState(true);
  const [codexEnabled, setCodexEnabled] = useState(false);
  const [codexBase, setCodexBase] = useState('');
  const [codexKey, setCodexKey] = useState('');
  const [codexKeyMasked, setCodexKeyMasked] = useState('');
  const [codexTesting, setCodexTesting] = useState(false);
  const [codexTest, setCodexTest] = useState<{ ok: boolean; text: string; detail?: string } | null>(null);
  const modelRequest = useRef(0);

  useEffect(() => {
    let alive = true;
    setSettingsLoading(true);
    setNote('');
    fetchHarnessSettings().then((value) => {
      if (!alive) return;
      setSettings(value);
      setRuntime(value.runtime);
      setModel(value.models[value.runtime] || '');
      setSandbox(value.sandbox);
      setAutoApprove(value.codex_auto_approve ?? false);
      setCodexEnabled(Boolean(value.codex_api?.enabled));
      setCodexBase(value.codex_api?.baseUrl || '');
      setCodexKey('');
      setCodexKeyMasked(value.codex_api?.keyMasked || '');
      setCodexTest(null);
      onChange(value.runtime);
      onSelect(value.runtime);
    }).catch((error) => {
      if (alive) setNote(`读取失败：${error instanceof Error ? error.message : String(error)}`);
    }).finally(() => {
      if (alive) setSettingsLoading(false);
    });
    return () => { alive = false; modelRequest.current += 1; };
  }, [onChange, onSelect, refresh]);

  const selected = settings?.agents.find((agent) => agent.id === runtime);
  const fullAccess = sandbox === 'danger-full-access';
  const nativeModels = catalogs[runtime] || [];
  const customCodex = runtime === 'codex' && codexEnabled;
  const pullModels = async () => {
    const requested = runtime;
    const sequence = ++modelRequest.current;
    setModelLoading(true);
    setModelNote('');
    try {
      const result = await fetchHarnessModels(requested);
      if (modelRequest.current !== sequence) return;
      setCatalogs((previous) => ({ ...previous, [requested]: result.models }));
      if (!result.models.length) setModelNote('原生 CLI 没有返回模型，可使用默认模型或手动填写。');
    } catch (error) {
      if (modelRequest.current === sequence) {
        setModelNote(`读取失败：${error instanceof Error ? error.message : String(error)}`);
      }
    } finally {
      if (modelRequest.current === sequence) setModelLoading(false);
    }
  };
  const testCodexAPI = async () => {
    if (!model.trim()) {
      setCodexTest({ ok: false, text: '缺模型名', detail: '请先填写模型名称' });
      return;
    }
    if (!codexBase.trim()) {
      setCodexTest({ ok: false, text: '缺 Base URL', detail: '请先填写自定义 API 地址' });
      return;
    }
    setCodexTesting(true);
    setCodexTest(null);
    try {
      const result = await testModelConnection({
        baseUrl: codexBase,
        key: codexKey,
        model,
        protocol: 'openai-responses',
        slot: 'codex',
      });
      setCodexTest(result.ok
        ? { ok: true, text: `✓ ${result.ms}ms`, detail: `${result.protocol} · ${result.model} · ${result.ms}ms` }
        : { ok: false, text: `✗ ${(result.detail || '失败').slice(0, 32)}`, detail: result.detail });
    } catch (error) {
      const detail = error instanceof Error ? error.message : '测试请求失败';
      setCodexTest({ ok: false, text: `✗ ${detail.slice(0, 32)}`, detail });
    } finally {
      setCodexTesting(false);
    }
  };
  const save = async () => {
    setBusy(true);
    setNote('');
    try {
      const value = await saveHarnessSettings(
        runtime,
        model,
        sandbox,
        runtime === 'codex' ? autoApprove : undefined,
        runtime === 'codex'
          ? { enabled: codexEnabled, baseUrl: codexBase, key: codexKey, protocol: 'openai-responses' }
          : undefined,
      );
      setSettings(value);
      setCodexEnabled(Boolean(value.codex_api?.enabled));
      setCodexBase(value.codex_api?.baseUrl || '');
      setCodexKey('');
      setCodexKeyMasked(value.codex_api?.keyMasked || '');
      setCodexTest(null);
      onChange(value.runtime);
      setNote(runtime === 'codex' && codexEnabled
        ? '已保存 Codex 自定义 API；请新建对话验证。接口必须是 OpenAI Responses 协议。'
        : runtime === 'codex' && sandbox === 'danger-full-access'
          ? '已保存并立即生效。当前对话及后续对话的本机执行授权都会自动放行，不再逐次询问。'
          : '已保存，请新建对话；旧对话仍保留在原底座，不迁移历史。');
    } catch (error) {
      setNote(`保存失败：${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="st-panel active harness-settings" aria-label="Agent 底座设置">
      <div className="panel-top">
        <strong>Agent 底座</strong>
        <span className="desc">选择仅聊天，或使用 Agent 执行工具</span>
        <span className="spacer" />
        <button className="btn btn-sm" disabled={!settings || settingsLoading || busy || settings.locked || !selected?.installed}
          onClick={() => void save()}>{busy ? '保存中…' : '保存底座'}</button>
        <button className="btn btn-sm" disabled={settingsLoading || busy}
          onClick={() => setRefresh((previous) => previous + 1)}>{settingsLoading ? '读取中…' : '重新读取'}</button>
      </div>
      <div className="settings-field">
        <span className="settings-field-label">执行引擎</span>
        <div className="settings-field-body">
          <div className="settings-engine-control">
            <SettingsSelect label="执行引擎" value={runtime} disabled={!settings || settingsLoading || busy || settings.locked}
              options={(settings?.agents || [{ id: 'api', label: '仅聊天模式', installed: true }])
                .map((agent) => ({
                value: agent.id,
                label: `${agent.label}${agent.installed ? '' : '（未安装 / 未配置）'}`,
              }))}
              onChange={(value) => {
                setRuntime(value);
                setModel(settings?.models[value] || '');
                modelRequest.current += 1;
                setModelLoading(false);
                setModelNote('');
                setNote('');
                onSelect(value);
              }} />
            <span className="settings-help">
              <button type="button" className="settings-help-trigger" aria-label="仅聊天模式说明"
                aria-describedby="chat-only-help">ⓘ</button>
              <span id="chat-only-help" className="settings-help-tooltip" role="tooltip">
                仅聊天模式直接调用模型 API，适合问答、写作和内容分析，不启动 Agent，
                不能操作本机文件、运行命令、使用浏览器或执行技能。请在下方配置模型、API 地址和 Key；
                需要工具时可切换到 OpenClaw、OpenCode、Codex 或其它 Agent。切换后请新建对话，旧历史保留。
              </span>
            </span>
          </div>
          <p className="desc">{runtime === 'api' ? '模型 API 直连 · 无需安装 Agent，模型在下方配置'
            : runtime === 'openclaw' ? 'OpenClaw 的模型、API 地址和 Key 在下方配置，保存后同步到 gateway'
            : `${selected?.protocol || ''} · 连接由适配器管理，无需另选连接方式`}</p>
          <p className="desc">安装时可选择 OpenClaw，也可先跳过，进入这里选择已安装的其它引擎。选择引擎可查看对应配置，只有「保存底座」才切换；认证沿用各引擎自身配置。</p>
          {selected && !selected.installed && (
            <p className="desc" role="status">未检测到 {selected.label}。请确认 CLI 已安装在运行 Web 的服务器账户下，并检查启动 PATH；
              安装或调整路径后重启 Web。原有配置未删除，安装前不能保存为执行引擎。</p>
          )}
          {runtime !== settings?.runtime && <p className="desc">当前为配置预览；请先「保存底座」，再保存下方 API 配置。旧对话不迁移。</p>}
        </div>
      </div>
      {runtime !== 'openclaw' && runtime !== 'api' && (
        <>
          <div className="settings-field">
            <label className="settings-field-label" htmlFor="harness-model">模型覆盖</label>
            <div className="settings-field-body">
              {(runtime === 'opencode' || (runtime === 'codex' && !codexEnabled)) && (
                <div className="settings-native-models">
                  <SettingsSelect label="原生模型" value={model} disabled={busy || settings?.locked}
                    options={[
                      { value: '', label: '使用原生默认模型' },
                      ...nativeModels.map((item) => ({ value: item.id,
                        label: `${item.label}${item.default ? '（原生默认）' : ''}` })),
                      ...(model && !nativeModels.some((item) => item.id === model)
                        ? [{ value: model, label: `${model}（当前 / 自定义）` }] : []),
                    ]} onChange={setModel} />
                  <button className="btn btn-sm" disabled={modelLoading || busy || !selected?.installed}
                    onClick={() => void pullModels()}>{modelLoading ? '读取中…' : '拉取模型'}</button>
                </div>
              )}
              <input id="harness-model" className="mock" value={model} disabled={busy || settings?.locked} maxLength={240}
              onChange={(event) => setModel(event.target.value)}
              placeholder={customCodex
                ? '填写自定义 API 的模型名'
                : runtime === 'codex' ? '留空使用 Codex 原生配置' : 'provider/model；留空使用原生配置'} />
              <p className="desc">{customCodex
                ? '这里填写上游 Responses API 的真实模型名；Codex 会通过 Easel 专用 provider 使用它。'
                : '可从原生 CLI 列表选择，也可手动填写；留空沿用原生默认模型，不修改其它底座的供应商配置。' +
                  '模型是否可用仍取决于当前账号、供应商和原生 CLI 支持。'}</p>
              {modelNote && <p className="desc" role="status">{modelNote}</p>}
            </div>
          </div>
          {runtime === 'codex' && (
            <div className="settings-field">
              <span className="settings-field-label">自定义 API</span>
              <div className="settings-field-body">
                <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                  <button type="button" role="switch" aria-checked={codexEnabled}
                    aria-label="使用 Codex 自定义 API" className="settings-permission-switch"
                    disabled={busy || settingsLoading || !settings || settings.locked}
                    onClick={() => {
                      setCodexEnabled((previous) => !previous);
                      setCodexTest(null);
                    }}>
                    <span className="settings-permission-switch__thumb" />
                  </button>
                  <span className="desc" style={{ margin: 0 }}>
                    {codexEnabled ? '使用自定义 Base URL 与 API Key' : '使用 Codex 原生登录'}
                  </span>
                </div>
                {codexEnabled && (
                  <div style={{ display: 'grid', gap: 8, marginTop: 12 }}>
                    <div className="desc" style={{ margin: 0 }}>
                      协议：OpenAI Responses（请求到 <code>/responses</code>）
                    </div>
                    <input className="mock" value={codexBase} maxLength={300}
                      disabled={busy || settings?.locked}
                      placeholder="https://api.example.com/v1"
                      aria-label="Codex 自定义 API Base URL"
                      onChange={(event) => {
                        setCodexBase(event.target.value);
                        setCodexTest(null);
                      }} />
                    <input className="mock" type="password" value={codexKey} maxLength={400}
                      disabled={busy || settings?.locked}
                      placeholder={codexKeyMasked || '粘贴 API Key'}
                      aria-label="Codex 自定义 API Key"
                      onChange={(event) => {
                        setCodexKey(event.target.value);
                        setCodexTest(null);
                      }} />
                    <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                      <button type="button" className="btn btn-sm"
                        disabled={codexTesting || busy || settings?.locked}
                        onClick={() => void testCodexAPI()}>
                        {codexTesting ? <><span className="spin" />测试中…</> : '测试连通'}
                      </button>
                      {codexTest && (
                        <span className={`stt ${codexTest.ok ? 'good' : 'bad'}`} title={codexTest.detail}>
                          {codexTest.text}
                        </span>
                      )}
                    </div>
                    <p className="desc" style={{ margin: 0 }}>
                      Base URL 填到 <code>/v1</code> 层级。测试会向上游发送一次最小真实请求，
                      验证地址、Key、模型和 Responses 协议；Key 留空时使用已保存的 Key。
                    </p>
                  </div>
                )}
                <p className="desc">
                  当前 Codex CLI 只支持 OpenAI Responses 协议。需要 Chat Completions
                  （<code>/chat/completions</code>）时，请切换到“仅聊天模式”或 OpenClaw 配置，
                  两处都支持自定义 URL、Key 和模型连通测试。
                </p>
              </div>
            </div>
          )}
          {runtime === 'codex' && (
            <>
            <div className="settings-field">
              <span className="settings-field-label">Codex 权限模式</span>
              <div className="settings-field-body">
                <SettingsSelect label="Codex 权限模式" value={sandbox} disabled={busy || settings?.locked}
                  options={[
                    { value: 'workspace-write', label: '项目工作区写入' },
                    { value: 'danger-full-access', label: '完全访问（自动授权）' },
                    { value: 'read-only', label: '只读' },
                  ]}
                  onChange={setSandbox} />
                <p className="desc">{fullAccess
                  ? '可读写本机任意路径并运行命令，同时跳过 Codex 的逐次执行授权。仅在信任当前任务时使用。'
                  : '保留所选沙箱边界；完全访问模式才会获得系统级读写能力。'}</p>
                {fullAccess && <div className="notice-error" role="alert">
                  完全访问风险最高：Agent 可修改工作区外文件、运行系统命令或访问网络。
                  如只做内容创作和项目文件整理，优先使用「项目工作区写入」。
                </div>}
              </div>
            </div>
            <div className="settings-field">
              <label className="settings-field-label" htmlFor="codex-auto-approve">普通操作自动授权</label>
              <div className="settings-field-body">
                <button id="codex-auto-approve" type="button" role="switch"
                  aria-checked={autoApprove} aria-label="普通操作自动授权"
                  aria-describedby="codex-auto-approve-help" className="settings-permission-switch"
                  disabled={busy || settingsLoading || !settings || settings.locked || fullAccess}
                  onClick={() => setAutoApprove((previous) => !previous)}>
                  <span className="settings-permission-switch__thumb" />
                </button>
                <p id="codex-auto-approve-help" className="desc">{fullAccess
                  ? '完全访问模式已包含自动授权，无需再开启此项。'
                  : <>保存后持续生效，仅影响 Easel 的 Codex：
                    自动批准普通文件读取、搜索、Git 查看/拉取，以及项目工作区内的文件修改授权。
                    删除、系统修改、执行脚本及无法判断安全性的操作仍需确认；不是无限权限，可随时关闭。</>}</p>
              </div>
            </div>
            </>
          )}
          <p className="desc">{runtime === 'codex' && codexEnabled
            ? '此模式由 Easel 使用隔离的 Codex provider 启动，不改写全局 ~/.codex/config.toml；Key 持久化在本机 .env。'
            : '认证由原生 CLI 管理，Easel 不迁移密钥。'}
            {fullAccess ? '完全访问模式下，Codex 不会再弹出技术执行授权；'
              : '未自动批准的额外权限需在对话中确认；'}
            OpenCode / 通用 ACP 使用其原生权限策略，不是 Codex 权限模式。</p>
          <p className="desc"><code>{selected?.install_hint}</code></p>
          <p className="desc">“已安装”仅表示找到 CLI，不代表模型已登录；保存后请发送消息验证。</p>
        </>
      )}
      {settings?.locked && <p className="desc">启动环境 EASEL_AGENT_RUNTIME 已固定底座，请修改启动环境后重启。</p>}
      {note && <div className={`save-note${note.includes('失败') ? ' err' : ''}`} role="status">{note}</div>}
    </section>
  );
}
