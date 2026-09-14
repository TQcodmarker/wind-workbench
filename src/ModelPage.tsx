import {useEffect, useState} from 'react';
import {Check, ChevronDown, Eye, EyeOff, LoaderCircle, Plug, RotateCcw} from 'lucide-react';
import {api, post, time} from './data';

type Settings = {baseUrl:string; model:string; requireKey:boolean};
type Config = Settings & {
  hasKey:boolean; configured:boolean; readyToTest:boolean;
  testResult:null|{status:string; message:string; testedAt:string; latencyMs:number};
};
const empty:Settings = {baseUrl:'', model:'', requireKey:true};

export default function ModelPage() {
  const [saved, setSaved] = useState<Config|null>(null);
  const [draft, setDraft] = useState<Settings>(empty);
  const [key, setKey] = useState('');
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState('loading');
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const dirty = !!key || (!!saved && Object.keys(empty).some(k => draft[k as keyof Settings] !== saved[k as keyof Settings]));
  const needsKey = draft.requireKey && (!saved?.hasKey || draft.baseUrl.replace(/\/$/, '') !== saved.baseUrl);

  function accept(config:Config) {
    setSaved(config);
    setDraft({baseUrl:config.baseUrl, model:config.model, requireKey:config.requireKey});
    setKey(''); setShow(false);
  }
  useEffect(() => {
    const controller = new AbortController();
    api<Config>('/model', {signal:controller.signal}).then(accept)
      .catch(e => {if(e.name !== 'AbortError') setError(e.message)})
      .finally(() => {if(!controller.signal.aborted) setBusy('')});
    return () => controller.abort();
  }, []);
  function change<K extends keyof Settings>(field:K, value:Settings[K]) {
    setDraft(d => ({...d, [field]:value})); setError(''); setMessage('');
    if(field === 'baseUrl') {setKey(''); setShow(false)}
  }
  async function action(kind:'save'|'test'|'clear') {
    setBusy(kind); setError(''); setMessage('');
    try {
      const config = await post<Config>(kind==='save'?'/model':`/model/${kind}`, kind==='save'?{...draft, apiKey:key}:{});
      accept(config);
      if(kind === 'test') {
        if(config.testResult?.status === 'passed') setMessage(config.testResult.message);
        else setError(config.testResult?.message || '测试未通过');
      } else setMessage(kind==='save'?'配置已保存，可以测试模型连接。':'已清除大模型配置。');
    } catch(e) {setError((e as Error).message)} finally {setBusy('')}
  }
  const status = dirty ? '有未保存修改' : saved?.testResult?.status==='passed' ? '连接成功' : saved?.testResult ? '测试未通过' : '未验证';
  return <section className="page source-page model-page">
    <div className="page-heading"><h1>大模型配置</h1><span className="source-protocol">OpenAI 兼容协议</span></div>
    <div className="source-layout"><div>
      <section className="source-card">
        <div className="source-section-title"><h2>连接配置</h2><span>{dirty?'未保存修改':saved?.configured?'已保存':'尚未配置'}</span></div>
        <form onSubmit={e=>{e.preventDefault();action('save')}} autoComplete="off">
          <fieldset disabled={!!busy || !saved} className="model-fieldset">
            <label className="source-field" htmlFor="model-url">服务地址（Base URL）<span className="required-mark"> *</span></label>
            <input className="model-input" id="model-url" type="url" required value={draft.baseUrl} maxLength={2048} placeholder="https://your-provider.example/v1" onChange={e=>change('baseUrl', e.target.value)}/>
            <label className="source-field" htmlFor="model-name">模型名称（Model ID）<span className="required-mark"> *</span></label>
            <input className="model-input" id="model-name" required maxLength={200} placeholder="填写服务商提供的准确模型 ID" value={draft.model} onChange={e=>change('model', e.target.value)}/>
            <label className="model-auth-toggle"><input type="checkbox" checked={draft.requireKey} onChange={e=>change('requireKey', e.target.checked)}/>服务需要 API Key 鉴权</label>
            {draft.requireKey?<><label className="source-field" htmlFor="model-key">API Key{needsKey?<span className="required-mark"> *</span>:<small>已保存，留空保留</small>}</label><div className="source-secret model-secret"><input id="model-key" type={show?'text':'password'} required={needsKey} value={key} maxLength={4096} autoComplete="new-password" placeholder={needsKey?'输入 API Key，不含 Bearer 前缀':'已保存，输入新 Key 可替换'} onChange={e=>{setKey(e.target.value);setMessage('');setError('')}}/><button className="icon-button" type="button" aria-label={show?'隐藏 API Key':'显示 API Key'} aria-pressed={show} onClick={()=>setShow(!show)}>{show?<EyeOff/>:<Eye/>}</button></div></>:null}
            {draft.requireKey?<p className="source-help">API Key 仅保存在本次服务内存中，服务重启后需重新填写。</p>:null}
            <div className="source-form-actions"><button className="button primary" type="submit" disabled={!dirty}>{busy==='save'?<LoaderCircle className="spin"/>:<Check/>}保存配置</button><button className="button secondary" type="button" disabled={!dirty} onClick={()=>{if(saved)accept(saved);setMessage('已撤销未保存修改');setError('')}}><RotateCcw/>撤销修改</button><button className="text-button" type="button" disabled={!saved?.configured} onClick={()=>action('clear')}>清除配置</button></div>
          </fieldset>
        </form>
        {error?<div className="inline-error" role="alert">{error}</div>:null}{message?<div className="inline-success" role="status">{message}</div>:null}
        {!saved&&!busy?<button className="button secondary" onClick={()=>location.reload()}>重新加载配置</button>:null}
        <details className="source-details"><summary>服务填写说明<ChevronDown/></summary><p>使用支持 Chat Completions 的服务，填写服务商提供的基础地址与模型 ID。地址需保留 /v1 等版本路径，系统自动追加 /chat/completions；本机服务可填写 http://localhost:端口/v1。</p><p>更换服务地址后需重新填写 API Key。服务地址与模型名称保存在本机，Key 不写入浏览器存储或配置文件。</p></details>
      </section>
      <p className="source-usage-note">AI 助手尚未接入此模型，目前使用规则解析与模板摘要。</p>
    </div><aside>
      <section className="source-card connection-card"><div className="source-section-title"><h2>连接状态</h2><span className={`badge ${status==='连接成功'?'success':'neutral'}`}>{status}</span></div>
        <dl className="source-status-list"><div><dt>模型</dt><dd>{saved?.model || '未配置'}</dd></div><div><dt>访问凭据</dt><dd>{!saved?'读取中':!saved.requireKey?'无需 API Key':saved.hasKey?'已填写':'待填写 API Key'}</dd></div></dl>
        {dirty?<p className="source-help">请先保存当前修改，再验证连接。</p>:saved?.testResult?<p className="source-help">{saved.testResult.message}</p>:null}
        <button className="button primary full-width" disabled={!!busy||dirty||!saved?.readyToTest} onClick={()=>action('test')}>{busy==='test'?<LoaderCircle className="spin"/>:<Plug/>}{busy==='test'?'正在测试连接…':'测试连接'}</button>
        <p className="source-help">测试会发送一句消息，可能产生少量费用；不发送行情或工作台数据。</p>
        {saved?.testResult&&!dirty?<div className="model-test-meta">测试于 {time(saved.testResult.testedAt)}<br/>耗时 {saved.testResult.latencyMs} ms</div>:null}
      </section>
    </aside></div>
  </section>;
}
