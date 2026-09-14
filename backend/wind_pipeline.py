"""Multi-tool Wind acquisition. Failed or partial runs retain every source reply."""
from datetime import date,timedelta
from pathlib import Path
from . import storage as store
from .domain import REGIONS,RULES,YIELD_DEFINITION,calculate,classify
from .credentials import read_wind_key
from .lineage import Recorder,save_observation,update_decision
from .wind_mcp import WindMCP,WindError
from .wind_mapping import Merge,VERSION,rows_from

BATCH_SIZE=10
FIELD_GROUPS=[
 ('发行档案','get_bond_basicinfo','证券全称、发行起始日期、主证券代码与跨市场代码'),
 ('发行规模','get_bond_basicinfo','发行总额（亿元）与币种'),
 ('债券分类','get_bond_basicinfo','所属概念板块'),
 ('发行主体','get_bond_issuer_info','发行主体名称'),
 ('收盘价到期收益率','get_bond_market_data','收盘价到期收益率（%），保留原始指标名称和单位'),
 ('剩余期限','get_bond_market_data','剩余期限（年），保留原始指标名称和单位'),
 ('久期','get_bond_market_data','收盘价修正久期，保留原始指标名称和单位'),
]


class Pipeline:
    def __init__(self,run_id,client_factory=WindMCP,regions=None,batch_size=BATCH_SIZE):
        self.run_id=run_id; self.target=store.get_run(run_id)['targetDate']
        self.regions=regions if regions is not None else REGIONS
        self.factory=client_factory;self.batch_size=batch_size
        self.recorder=Recorder('全量多工具取数',self.target,run_id,
            {'rules':RULES,'yieldDefinition':YIELD_DEFINITION,'fieldMappingVersion':VERSION,'fieldGroups':FIELD_GROUPS,'regions':self.regions,'batchSize':batch_size})
        # Keep the exact implementation with future runs, so later mapping changes
        # do not erase the rules that produced an earlier result.
        self.recorder.artifact('implementation',{'files':{
            name:(Path(__file__).parent/name).read_text(encoding='utf-8')
            for name in ('domain.py','wind_mapping.py','wind_pipeline.py')}})
        self.merge=Merge(self.target);self.records=[];self.observation_ids={};self.coverage=[];self.discovered={}
        self.tool_failures=[]

    def progress(self,stage,**changes):
        self.recorder.stage=stage
        store.update_run(self.run_id,phase=stage,traceSessionId=self.recorder.id,**changes)

    async def ask(self,mcp,stage,tool,question,expected=None,primary=False):
        self.progress(stage)
        result=await mcp.call(tool,{'question':question})
        codes,total=self.merge.add(result,mcp.last_request_id,expected,primary)
        return result,codes,total

    def issuer(self,region):
        return region['name'] if region['id']=='xpcc' else region['name']+'人民政府'

    async def universe(self,mcp,region,lower=None,upper=None,depth=0):
        issuer=self.issuer(region)
        interval=(f'发行起始日期不早于{lower}，' if lower else '')+(f'发行起始日期不晚于{upper}，' if upper else '')
        criteria=f'截至{self.target}，由{issuer}发行且未到期的地方政府债券，{interval}按主证券代码去重。'
        _,_,total=await self.ask(mcp,f'核对{region["name"]}名单总数','get_bond_basicinfo',criteria+'只统计去重后的主证券代码总数，返回一个整数。',primary=True)
        if total is None:raise WindError('Wind 未返回去重后主证券代码总数，完整性无法确认')
        if total==0:
            self.recorder.artifact('universe-partition',{'regionId':region['id'],'lower':lower,'upper':upper,'reportedTotal':0,'requestId':mcp.last_request_id})
            return set(),0
        q=criteria+'仅列出全部主证券代码、发行起始日期、债务主体名称，不计算总数。'
        result,codes,_=await self.ask(mcp,f'获取{region["name"]}名单','get_bond_basicinfo',q,primary=True)
        self.discovered.setdefault(region['id'],set()).update(codes)
        evidence={'regionId':region['id'],'lower':lower,'upper':upper,'received':len(codes),'reportedTotal':total,'requestId':mcp.last_request_id}
        self.recorder.artifact('universe-partition',evidence)
        rows,_=rows_from(result,mcp.last_request_id,primary=True)
        issue_dates=[]
        for code,fields in rows:
            attrs={c['name']:c['value'] for c in fields}
            issued=attrs.get('发行起始日期')
            try:date.fromisoformat(issued)
            except (ValueError,TypeError):raise WindError('名单缺少可验证的发行日期，无法确认分批范围')
            if issued>self.target or (lower and issued<lower) or (upper and issued>upper):
                raise WindError('Wind 未遵守发行日期分批条件；已保存范围外记录，停止发布')
            if attrs.get('债务主体名称')!=issuer:
                raise WindError('名单发行主体未明确匹配当前地区，无法确认范围')
            issue_dates.append(issued)
        if total is None or len(codes)>total:raise WindError('Wind 未返回一致的名单总数，完整性无法确认')
        if len(codes)==total:return codes,total
        if depth>=16 or not issue_dates:raise WindError('名单仍被截断，分批获取未能完成；已保存全部返回')
        # Split by observed dates; the two closed intervals cover the parent exactly.
        dates=sorted(set(issue_dates)); pivot=dates[(len(dates)-1)//2]
        if upper and pivot>=upper:
            pivot=(date.fromisoformat(upper)-timedelta(days=1)).isoformat()
        if lower and pivot<lower:raise WindError('单个发行日的名单仍被截断，需来源支持更细分页')
        left,left_total=await self.universe(mcp,region,lower,pivot,depth+1)
        right,right_total=await self.universe(mcp,region,(date.fromisoformat(pivot)+timedelta(days=1)).isoformat(),upper,depth+1)
        if left&right or len(left|right)!=total or left_total+right_total!=total:
            raise WindError('分批结果与总数不一致，未发布不完整名单')
        return left|right,total

    async def fields(self,mcp,codes,region):
        """Fetch separate field groups and fall back once to individual missing codes."""
        for offset in range(0,len(codes),self.batch_size):
            batch=codes[offset:offset+self.batch_size]
            for stage,tool,fields in FIELD_GROUPS:
                q=f'查询以下债券在{self.target}的{fields}，逐券返回Wind代码和原始字段，缺失保留空值：'+ '、'.join(batch)
                try:
                    _,received,_=await self.ask(mcp,f'{region["name"]} · {stage} · {offset+1}/{len(codes)}',tool,q,batch)
                    if len(batch)>1:
                        for code in sorted(set(batch)-received):
                            await self.ask(mcp,f'{region["name"]} · {stage} · 单券补取',tool,
                                f'查询{code}在{self.target}的{fields}，返回Wind代码和原始字段，缺失保留空值。',[code])
                except WindError as exc:
                    # Preserve other independent field groups, but any failed call blocks publication.
                    self.tool_failures.append(str(exc))
                    if any(x in str(exc) for x in ['HTTP 401','HTTP 403','HTTP 429','网络','超时']):raise
            normalized=[]
            for code in batch:
                row,sources=self.merge.normalize(code)
                if row.get('regionId')!=region['id']:row['_validationErrors'].append('发行主体与名单地区不一致')
                decision=classify(row,self.target)
                self.observation_ids[code]=save_observation(self.run_id,row,sources,VERSION,decision)
                self.records.append(row);normalized.append(row)
            self.recorder.artifact('normalized-batch',{'codes':batch,'mappingVersion':VERSION,'rows':normalized})
            self.progress(f'{region["name"]} · 已保存 {len(self.records)} 条个券记录',acquiredRecords=len(self.records))
            # A field-wide outage is not evidence that every bond should be excluded.
            essentials=['bondId','regionId','bondType','issueDate','issueAmountYi','yieldPct','yieldMetric','yieldPriceBasis','yieldDate','remainingYears']
            absent=[field for field in essentials if all(row.get(field) is None for row in normalized)]
            if absent:
                raise WindError('分工具取数后仍整批缺失必需字段：'+ '、'.join(absent)+'；原始响应和个券合并结果已保存')
            if all(row['valueStatus']!='valid' for row in normalized):
                raise WindError('整批未取得有效收盘价到期收益率；不能据此发布空矩阵')
            if all(row['_validationErrors'] for row in normalized):
                raise WindError('整批字段口径未通过校验：'+'、'.join(normalized[0]['_validationErrors']))

    async def fetch(self):
        self.progress('连接 Wind 与发现工具')
        async with self.factory(read_wind_key(),recorder=self.recorder) as mcp:
            tools=await mcp.list_tools()
            self.recorder.artifact('tool-catalog',{'protocolVersion':mcp.version,'tools':tools})
            for region in self.regions:
                try:
                    codes,total=await self.universe(mcp,region)
                except WindError as exc:
                    self.coverage.append({'regionId':region['id'],'complete':False,'error':str(exc)})
                    self.recorder.artifact('coverage',self.coverage)
                    # Still collect independent field groups for a small known subset, to expose
                    # field availability without pretending that the universe is complete.
                    sample=sorted(self.discovered.get(region['id'],set()))[:self.batch_size]
                    if sample:
                        try:await self.fields(mcp,sample,region)
                        except WindError as field_error:self.tool_failures.append(str(field_error))
                    raise WindError(str(exc)+('；'+self.tool_failures[-1] if self.tool_failures else '')) from exc
                self.coverage.append({'regionId':region['id'],'complete':True,'count':total})
                self.recorder.artifact('coverage',self.coverage)
                await self.fields(mcp,sorted(codes),region)
        if self.tool_failures:raise WindError('部分字段请求失败，未发布行情：'+self.tool_failures[0])
        if {r['id'] for r in self.regions}!={r['id'] for r in REGIONS}:
            raise WindError('此次只完成部分地区取数，不能发布全国完整快照')
        if not self.records:raise WindError('未取得任何逐券数据，不能确认全市场当日无到期收益率')
        return self.records,{'source':'wind','complete':True,'evaluationDate':self.target,
            'traceSessionId':self.recorder.id,'mappingVersion':VERSION,'yieldDefinition':YIELD_DEFINITION,'coverage':self.coverage}

    async def run(self):
        try:
            raw,provenance=await self.fetch()
            self.progress('计算与保存逐券筛选依据')
            cells,counts=calculate(raw,self.target,observer=lambda row,d:update_decision(self.observation_ids[row['code']],d))
            self.recorder.artifact('calculation',{'rules':RULES,'counts':counts,'cells':cells})
            self.progress('原子发布完整到期收益率快照')
            store.publish(self.run_id,cells,counts,provenance)
            return cells,counts
        except Exception as exc:
            self.recorder.artifact('pipeline-failure',{'error':str(exc) if isinstance(exc,(WindError,ValueError)) else type(exc).__name__,
                'coverage':self.coverage,'recordsSaved':len(self.records),'toolFailures':self.tool_failures})
            raise
