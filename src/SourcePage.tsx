import {useEffect, useState} from 'react';
import FullSync from './FullSync';
import {Check, ChevronDown, ExternalLink, Eye, EyeOff, Link, LoaderCircle, Plug, RefreshCw} from 'lucide-react';
import {api, post, YieldDefinition, closeYieldDefinition, legacyYieldDefinition, Provider, providerName, time, RuleData} from './data';

type Verification = {yieldDefinition?:YieldDefinition; rulesVersion?:string; targetDate:string; checkedAt:string; message:string; checks:{name:string; status:string; detail:string}[]; preview:Record<string,unknown>[]};
type Source = {activeProvider?:Provider;providers?:{id:string;label:string}[];akshare?:{available:boolean;version:string|null;latestDate:string|null;dates:string[];message:string;collectedAt?:string};yieldDefinition?:YieldDefinition; verificationIsCurrent?:boolean; pipelineIsCurrent?:boolean; pipeline:{runId:string; targetDate:string; status:string; phase:string; message:string}|null; verification:Verification|null; mode:Provider; toolCount:number; endpoint:string; configured:boolean; connectionStatus:string; dataStatus:string; storage:string};
const official = 'https://market.windalice.com/#/market?tab=mcps&detailType=mcp&detailId=wind_bond_data-1';

export default function SourcePage({date,rules}:{date:string;rules?:RuleData|null}) {
  const [source, setSource] = useState<Source|null>(null);
  const [key, setKey] = useState('');
  const [show, setShow] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const verificationDefinition = source?.verification ? (source.verification.yieldDefinition || legacyYieldDefinition) : null;
  const currentDefinition = source?.activeProvider==='akshare'?closeYieldDefinition:source?.yieldDefinition || closeYieldDefinition;
  const activeProvider=source?.activeProvider||source?.mode;
  const historicalVerification = Boolean(source?.verification && !source.verificationIsCurrent);
  useEffect(() => {
    const controller = new AbortController();
    api<Source>('/source', {signal:controller.signal}).then(setSource).catch(e => {if(e.name !== 'AbortError') setError(e.message)});
    return () => controller.abort();
  }, []);
  function validate() {
    if(!key.trim() || /^bearer\s/i.test(key) || /\s/.test(key.trim())) {
      setError('请输入不含空格及 Bearer 前缀的 Wind Key');
      return false;
    }
    setError('');
    return true;
  }
  async function save() {
    if(!validate()) return;
    setBusy(true);
    try {
      setSource(await post<Source>('/source', {key}));
      setKey(''); setShow(false);
      setMessage('配置已加密保存在本机。连接与数据覆盖仍待验证。');
    } catch(e) {setError((e as Error).message)} finally {setBusy(false)}
  }
  async function test() {
    setBusy(true); setError(''); setMessage('');
    try {
      const result = await post<Source>('/source/test');
      setSource(result);
      if(result.connectionStatus === '握手成功') setMessage('握手成功；工作台必需字段与历史覆盖仍未验证。');
      else setError(result.connectionStatus);
    } catch(e) {setError((e as Error).message)} finally {setBusy(false)}
  }
  async function discover() {
    setBusy(true); setError(''); setMessage('');
    try {
      const result = await post<{tools:unknown[]}>('/source/discover');
      setSource(await api<Source>('/source'));
      setMessage(`已发现 ${result.tools.length} 个 Wind 工具；字段与数据完整性仍需核验。`);
    } catch(e) {setError((e as Error).message)} finally {setBusy(false)}
  }
  async function verify() {
    setBusy(true); setError(''); setMessage('');
    try {
      const result = await post<Source>('/source/verify', {targetDate:date});
      setSource(result);
      setError(result.verification?.message || '尚未完成数据核验');
    } catch(e) {setError((e as Error).message)} finally {setBusy(false)}
  }
  async function selectProvider(provider:'akshare'|'wind') {
    setBusy(true); setError(''); setMessage('');
    try {await post('/source/provider',{provider});location.reload()}
    catch(e) {setError((e as Error).message);setBusy(false)}
  }
  async function updateAkshare() {
    setBusy(true);setError('');setMessage('');
    try {await post('/runs',{targetDate:date,provider:'akshare'});setMessage(`已提交 ${date} 的 AKShare 更新，可在更新记录查看进度。`);setSource(await api<Source>('/source'))}
    catch(e) {setError((e as Error).message)}finally{setBusy(false)}
  }
  return <section className="page source-page">
    <div className="page-heading"><h1>数据源</h1><span className="source-protocol">当前：{source?providerName(activeProvider):'读取中'}</span></div>
    {error ? <div className="inline-error" role="alert">{error}</div> : null}
    {message ? <div className="inline-success" role="status">{message}</div> : null}
    <section className="source-card provider-selection" aria-label="选择工作台数据源">
      <div className="source-section-title"><h2>工作台数据源</h2><span>分别保存数据与计算口径</span></div>
      <div className="source-form-actions"><button className={`button ${activeProvider==='akshare'?'primary':'secondary'}`} aria-pressed={activeProvider==='akshare'} disabled={busy||!source||activeProvider==='akshare'} onClick={()=>selectProvider('akshare')}>{activeProvider==='akshare'?<Check/>:null}{activeProvider==='akshare'?'当前使用 AKShare':'切换到 AKShare'}</button><button className={`button ${activeProvider==='wind'?'primary':'secondary'}`} aria-pressed={activeProvider==='wind'} disabled={busy||!source||activeProvider==='wind'} onClick={()=>selectProvider('wind')}>{activeProvider==='wind'?<Check/>:null}{activeProvider==='wind'?'当前使用 Wind':'切换到 Wind'}</button></div>
      <p className="source-help">AKShare 无需 Key。切换后重新读取所选来源的日期、样本与规则；数据获取失败时保留当前来源。</p>
    </section>
    <section className="source-card" aria-label="AKShare 数据状态">
      <div className="source-section-title"><h2>AKShare</h2><span className={`badge ${source?.akshare?.available?'success':'neutral'}`}>{!source?'读取中':source.akshare?.available?'依赖可用':'依赖未就绪'}</span></div>
      <dl className="source-status-list"><div><dt>依赖版本</dt><dd>{source?.akshare?.version||'未检测到'}</dd></div><div><dt>最新缓存日期</dt><dd>{source?.akshare?.latestDate||'暂无缓存'}</dd></div><div><dt>已有日期</dt><dd>{source?.akshare?.dates?.length?source.akshare.dates.join('、'):'暂无'}</dd></div>{source?.akshare?.collectedAt?<div><dt>最近采集</dt><dd>{time(source.akshare.collectedAt)}</dd></div>:null}</dl>
      <p>{source?.akshare?.message||'正在读取 AKShare 的本机依赖与缓存状态。'}</p><p>个券实际成交数据与指数、曲线参考分别展示。替代指标标注“已获取（替代）”，保留实际指标名称、来源和日期。</p>
      <div className="source-form-actions"><button className="button primary" disabled={busy||!date||activeProvider!=='akshare'||!source?.akshare?.available} onClick={updateAkshare}>{busy?<LoaderCircle className="spin"/>:<RefreshCw/>}更新 AKShare 数据</button><a className="button secondary" href="https://akshare.akfamily.xyz/data/bond/bond.html" target="_blank" rel="noreferrer">AKShare 债券接口<ExternalLink/></a></div>
      <p className="source-help">“更新 AKShare 数据”按目标日补充成交样本。全市场目录与静态档案使用下方全量同步。</p>
      <FullSync rules={rules} targetDate={date}/>
    </section>
    <div className="source-section-title"><h2>Wind MCP 配置与核验</h2><a className="button secondary" href={official} target="_blank" rel="noreferrer">查看 Wind 官方服务<ExternalLink/></a></div>
    <div className="source-layout"><div>
      <section className="source-card">
        <div className="source-section-title"><h2>Wind 连接配置</h2><span>{key ? '未保存修改' : source?.configured ? '已配置' : '尚未配置'}</span></div>
        <form onSubmit={e => {e.preventDefault(); save()}} autoComplete="off">
          <label className="source-field" htmlFor="wind-endpoint">MCP 服务地址 <small>官方地址</small></label>
          <div className="source-readonly"><Link/><input id="wind-endpoint" readOnly value={source?.endpoint || 'https://mcp.wind.com.cn/vserver_bond_data/mcp/'}/></div>
          <label className="source-field" htmlFor="wind-key">Wind Key <span className="required-mark">*</span></label>
          <div className="source-secret"><input id="wind-key" aria-label="Wind Key" type={show ? 'text' : 'password'} autoComplete="new-password" placeholder={source?.configured ? '已保存，输入可替换' : '输入具备债券服务权限的 Key'} value={key} onChange={e => {setKey(e.target.value); setMessage(''); setError('')}}/><button className="icon-button" type="button" aria-label={show ? '隐藏 Wind Key' : '显示 Wind Key'} aria-pressed={show} onClick={() => setShow(!show)}>{show ? <EyeOff/> : <Eye/>}</button></div>
          <p className="source-help">仅填写 Key，不含 Bearer 前缀。Key 加密保存在本机。</p>
          <div className="source-form-actions"><button className="button primary" disabled={busy || !key}><Check/>保存配置</button><button className="button secondary" type="button" disabled={busy} onClick={() => {if(validate()) setMessage('配置格式检查通过；尚未验证鉴权与连接。')}}>检查配置</button><button className="text-button" type="button" disabled={!key} onClick={() => {setKey(''); setShow(false); setError(''); setMessage('已撤销未保存修改')}}>撤销修改</button></div>
        </form>
        <details className="source-details"><summary>连接与存储说明<ChevronDown/></summary><p>使用 Bearer Token 鉴权。Wind Key 由 Windows 用户凭据加密，服务重启后仍可使用。原始响应与逐券依据长期保存在本机。</p><p>连接成功仅表示鉴权通过，数据是否可用于工作台以核验结果为准。</p></details>
      </section>
      <section className="source-card coverage-card">
        <div className="source-section-title"><h2>{historicalVerification ? 'Wind 历史数据核验' : 'Wind 工作台所需数据'}</h2><span>{date}</span></div>
        {historicalVerification ? <p className="source-help">{verificationDefinition?.metric === 'chinabond_valuation' ? '历史中债口径核验：以下为已保存的中债估值核验结果，不能作为新到期收益率口径的可用性结论。' : '历史规则核验：以下结果采用当时的字段验收与条款筛选规则，不代表当前数据可用性。'}</p> : null}
        <table><thead><tr><th>数据要求</th><th>状态</th></tr></thead><tbody>{(source?.verification?.checks || ['债券类型与发行地区', '发行日期与发行规模', '收盘价到期收益率（按查询日期归档）', '剩余期限', '历史地方债全集与批量获取'].map(name => ({name, status:'未验证', detail:''}))).map(check => <tr key={check.name}><td>{check.name}{check.detail ? <p className="source-help">{check.detail}</p> : null}</td><td>{check.status}</td></tr>)}</tbody></table>
        {source?.verification ? <p className="source-help">最近核验：{source.verification.targetDate} · {source.verification.checkedAt}</p> : null}
        <details className="source-details"><summary>数据口径<ChevronDown/></summary><p>新取数使用{currentDefinition.label}，按查询日期归档，采用 Wind MCP 返回的非空数值。</p><p>提前偿还及发行人赎回暂不采集、记录或用于筛选。</p></details>
      </section>
      {source?.verification?.preview.length ? <section className="source-card coverage-card"><h2>Wind 实际返回的档案样本</h2><p className="source-help">前 5 条返回样本，仅用于核验，未进入估值矩阵。</p><table><thead><tr><th>代码 / 简称</th><th>发行总额（亿元）</th></tr></thead><tbody>{source.verification.preview.map((row, i) => <tr key={i}><td>{String(row['Wind代码'] || '—')}<p className="source-help">{String(row['证券简称'] || '—')}</p></td><td>{String(row['发行总额'] ?? '—')}</td></tr>)}</tbody></table></section> : null}
    </div><aside>
      <section className="source-card connection-card">
        <div className="source-section-title"><h2>连接状态</h2><span className={`badge ${source?.connectionStatus === '握手成功' ? 'success' : 'neutral'}`}>{!source ? '读取中' : source.connectionStatus === '握手成功' ? '连接成功' : source.connectionStatus === '未验证' ? '未验证' : '未通过'}</span></div>
        <dl className="source-status-list"><div><dt>Wind Key</dt><dd>{source?.configured ? '已配置' : '未配置'}</dd></div><div><dt>鉴权与连接</dt><dd>{source?.connectionStatus || '未验证'}</dd></div><div><dt>数据可用性</dt><dd>{source?.dataStatus || '未验证'}</dd></div><div><dt>可用工具</dt><dd>{source?.toolCount || 0} 个</dd></div><div><dt>运行模式</dt><dd>{source ? providerName(activeProvider) : '读取中'}</dd></div></dl>
        <div className="source-connection-actions"><button className="button primary full-width" disabled={!source?.configured || busy} onClick={test}>{busy ? <LoaderCircle className="spin"/> : <Plug/>}验证真实连接</button><button className="button secondary full-width" disabled={!source?.configured || busy} onClick={discover}>发现 Wind 工具</button><button className="button secondary full-width" disabled={!source?.configured || busy || !date} onClick={verify}>{busy ? '正在请求 Wind…' : '核验工作台数据'}</button></div>
        <p className="source-help">以上操作会请求 Wind 官方服务。</p>
      </section>
      {source?.pipeline ? <section className="source-card"><h2>{!source.pipelineIsCurrent ? 'Wind 历史多工具取数' : 'Wind 最近多工具取数'}</h2>{!source.pipelineIsCurrent ? <p className="source-help">历史规则下的记录，供追溯使用。</p> : null}<p>{source.pipeline.targetDate} · {source.pipeline.phase}</p><p className="source-help">{source.pipeline.message}</p><a className="button secondary full-width" href={`/trace?run=${source.pipeline.runId}&date=${source.pipeline.targetDate}`}>查看原始数据与逐券依据</a></section> : null}
    </aside></div>
  </section>;
}

