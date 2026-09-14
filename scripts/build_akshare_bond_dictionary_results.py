"""Build normalized bond observations and explicit substitute availability."""
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
import csv
import inspect
import json
import math
from pathlib import Path

import akshare as ak
from query_akshare_bond_dictionary import ROOT, OUT, DOC, read, save

PREVIOUS = ROOT / 'research' / 'akshare-live-check-20260912-1239'
OLD = PREVIOUS / 'online'
AS_OF = date(2026, 9, 11)
LABELS = {'obtained': '已获取', 'derived': '已获取（计算）',
          'obtained_substitute': '已获取（替代）', 'derived_substitute': '已获取（计算替代）',
          'missing': '未获取', 'not_applicable': '不适用'}

# Field-level availability includes explicit substitutes as requested.
SPECS = [
    ('A', '证券代码', '证券代码及银行间市场映射', 'obtained', '代码', 'bond'),
    ('B', '发行地区', '从发行人名称提取的地区', 'derived', '地区', 'bond'),
    ('C', '证券简称', '证券简称', 'obtained', '文本', 'bond'),
    ('D', '剩余期限(年)', '剩余期限（Actual/365）', 'derived_substitute', '年', 'bond_day'),
    ('E', '余额(亿)', '发行规模（未偿余额的规模参考）', 'obtained_substitute', '亿元', 'bond_issue_event'),
    ('F', '地方债类型', '从债券全称识别的一般债/专项债', 'derived', '类别', 'bond'),
    ('G', '票面', '票面利率', 'obtained', '%', 'bond'),
    ('H', '久期', '地方政府债指数平均市值法久期（基准参考）', 'obtained_substitute', '年', 'index_day'),
    ('I', '地方债曲线', 'CFETS地方债AAA曲线期限插值收益率', 'derived_substitute', '%', 'curve_at_bond_tenor_day'),
    ('J', '中债估值', '同日成交收益率；缺失时地方债指数平均收益率参考', 'obtained_substitute', '%', 'bond_market_day_or_index_day'),
    ('K', '免税收益', '按原表公式计算的调整收益参考', 'derived_substitute', '%', 'proxy_at_bond_day'),
    ('L', '非免税曲线偏离', '替代收益率相对CFETS曲线的差值', 'derived_substitute', 'BP', 'proxy_at_bond_day'),
    ('M', '免税曲线偏离', '按原表调整公式计算的参考差值', 'derived_substitute', 'BP', 'proxy_at_bond_day'),
    ('N', '期限（四舍五入）', '基于Actual/365剩余期限的原表期限档位', 'derived_substitute', '年档', 'bond_day'),
    ('O', '债券是否提前偿还', '提前偿还/本金偿还安排', 'missing', '标志', 'bond'),
    ('P', '综合收益', '按原表公式计算的调整收益参考', 'derived_substitute', '%', 'proxy_at_bond_day'),
    ('Q', '久期*余额', '地方债指数久期×发行规模（研究参考）', 'derived_substitute', '年×亿元', 'bond_issue_event_x_index_day'),
    ('R', '债券是否提前偿还', '提前偿还/本金偿还安排', 'missing', '标志', 'bond'),
    ('S', '发行日期', '发行日期', 'obtained', '日期', 'bond'),
    ('T', '日期早于2025年8月8日，填1，否则为0', '发行日期早于2025-08-08的标记', 'derived', '0/1', 'bond'),
]

def detail(folder):
    result = read(folder / 'result.json')
    return {row['name']: row['value'] for row in read(folder / 'data.json')}, result

def dated_index(case):
    rows = read(OUT / 'queries' / case / 'data.json')
    selected = [row for row in rows if row['date'][:10] == AS_OF.isoformat()]
    assert len(selected) == 1, f'{case}: requested date unavailable or duplicated'
    return float(selected[0]['value'])

def numeric(value):
    if value in (None, '', '---'):
        return None
    result = float(value)
    return result if math.isfinite(result) else None

index_duration = dated_index('index_duration')
index_yield = dated_index('index_yield')
external_results = {
    'index_duration': read(OUT / 'queries/index_duration/result.json'),
    'index_yield': read(OUT / 'queries/index_yield/result.json'),
    'curve': read(OLD / 'local_curve_aaa/result.json'),
    'trade': read(OLD / 'spot_deals/result.json'),
}
curve_rows = read(OLD / 'local_curve_aaa' / 'data.json')
assert {row['日期'][:10] for row in curve_rows} == {AS_OF.isoformat()}
curve_points = sorted((float(row['期限']), float(row['到期收益率'])) for row in curve_rows)

def interpolate(term):
    # No silent extrapolation or stale-date fallback.
    terms = [point[0] for point in curve_points]
    i = bisect_left(terms, term)
    if i < len(terms) and terms[i] == term:
        return curve_points[i][1]
    if i == 0 or i == len(terms):
        return None
    x0, y0 = curve_points[i-1]
    x1, y1 = curve_points[i]
    return y0 + (y1-y0) * (term-x0)/(x1-x0)

raw_deals = read(OLD / 'spot_deals' / 'response-01.json')
trade_groups = defaultdict(list)
for row in raw_deals['records']:
    if str(row.get('showDate', ''))[:10] == AS_OF.isoformat():
        trade_groups[str(row['bondcode']) + '.IB'].append(row)
trades = {}
for code, group in trade_groups.items():
    values = {numeric(row.get('dmiLatestContraRate')) for row in group}
    assert len(values) == 1, f'{code}: conflicting observations'
    trades[code] = group[0]

reference = read(PREVIOUS / 'workbook-reference.json')
original_codes = {row['values'][0] for row in reference['rows']}
matched = sorted(original_codes & set(trades))
save(OUT / 'benchmark-indicators.json', {
    'as_of_date': AS_OF.isoformat(),
    'index': {'entity_id': '地方政府债指数', 'entity_grain': 'index_day',
              'average_duration': index_duration, 'average_yield_pct': index_yield,
              'source_functions': ['bond_index_general_cbond'],
              'evidence': ['queries/index_duration/data.json', 'queries/index_yield/data.json']},
    'curve': {'entity_id': '地方政府债(AAA)', 'entity_grain': 'curve_tenor_day',
              'source_function': 'bond_china_close_return', 'rows': curve_rows,
              'evidence': str(OLD / 'local_curve_aaa' / 'data.json')},
})

folders = [OLD / ('detail_typed_' + name) for name in ['general','special','amortizing','matured']]
folders.append(OUT / 'queries' / 'traded_detail')
observations = []
row_summaries = []
for folder in folders:
    d, result = detail(folder)
    code = str(d['bondCode']) + '.IB'
    name = d['bondName']
    coupon = numeric(d.get('parCouponRate'))
    issue_size = numeric(d.get('issueAmnt'))
    issue_date = date.fromisoformat(d['issueDate'])
    maturity = date.fromisoformat(d['mrtyDate'])
    term = max(0, (maturity - AS_OF).days / 365)
    matured = maturity <= AS_OF
    issuer = str(d.get('entyFullName', ''))
    region = issuer.removesuffix('人民政府').removesuffix('政府')
    full_name = str(d.get('bondFullName', ''))
    bond_type = '专项债' if '专项' in full_name else '一般债' if '一般' in full_name else None
    observed = numeric(trades.get(code, {}).get('dmiLatestContraRate'))
    proxy_yield = None if matured else observed if observed is not None else index_yield
    y_method = 'individual_trade' if observed is not None else 'index_reference'
    baseline = None if matured else interpolate(term)
    adjusted = proxy_yield + coupon / 4 if proxy_yield is not None and coupon is not None else None
    spread = (proxy_yield - baseline)*100 if proxy_yield is not None and baseline is not None else None
    adjusted_spread = (adjusted - 1.25*baseline)*100 if adjusted is not None and baseline is not None else None
    values = {'A': code, 'B': region or None, 'C': name, 'D': term, 'E': issue_size, 'F': bond_type,
              'G': coupon, 'H': None if matured else index_duration, 'I': baseline, 'J': proxy_yield,
              'K': adjusted, 'L': spread, 'M': adjusted_spread,
              'N': 30 if term >= 25 else math.floor(term + 0.5), 'O': None, 'P': adjusted,
              'Q': None if matured or issue_size is None else index_duration*issue_size,
              'R': None, 'S': issue_date.isoformat(), 'T': int(issue_date < date(2025,8,8))}
    fields_in_row = {}
    for col, original, actual, status, unit, grain in SPECS:
        value = values[col]
        is_market = col in {'H','I','J','K','L','M','P','Q'}
        note = ''
        method = None
        deps = []
        src_entity = code
        src_date = issue_date.isoformat() if col == 'E' else AS_OF.isoformat() if col in {'D','N'} or is_market else None
        function = 'bond_info_detail_cm'
        primary_result = result
        source_field = {'A':'bondCode','B':'entyFullName','C':'bondName','D':'mrtyDate','E':'issueAmnt',
                        'F':'bondFullName','G':'parCouponRate','S':'issueDate','T':'issueDate'}.get(col)
        evidence = [str(folder / 'data.json')]
        if col == 'D':
            method = 'max(0,(maturity-as_of).days/365)'
            note = '按实际天数/365计算，与Wind计年规则存在细小差异。'
        if col == 'B':
            method = 'remove_government_name_suffix'
        if col == 'F':
            method = 'classify_general_or_special_from_full_name'
        if col == 'E':
            note = '仅作为规模替代指标，不是未偿本金；到期后发行规模仍可为正。'
            method = 'issuance_size_reference'
        if col == 'H':
            primary_result = external_results['index_duration']
            function, source_field, src_entity = 'bond_index_general_cbond', 'value', '地方政府债指数'
            method = 'index_duration_reference'
            evidence = [str(OUT / 'queries/index_duration/data.json')]
            note = '指数级基准，不是这只债券的修正久期。'
        if col == 'I':
            primary_result = external_results['curve']
            function, source_field, src_entity = 'bond_china_close_return', '到期收益率', '地方政府债(AAA)'
            method, deps = 'linear_interpolation', ['D']
            evidence = [str(OLD / 'local_curve_aaa/data.json')]
            note = '以AAA曲线作统一比较基准，不代表对该券评级的确认；不外推。'
        if col == 'J':
            primary_result = external_results['trade' if y_method == 'individual_trade' else 'index_yield']
            function = 'bond_spot_deal' if y_method == 'individual_trade' else 'bond_index_general_cbond'
            source_field = '最新收益率' if y_method == 'individual_trade' else 'value'
            actual = '个券最新成交收益率（估值收益率替代）' if y_method == 'individual_trade' else '地方债指数平均市值法到期收益率（参考）'
            grain = 'bond_market_day' if y_method == 'individual_trade' else 'index_day'
            src_entity = code if y_method == 'individual_trade' else '地方政府债指数'
            evidence = [str(OLD / 'spot_deals/response-01.json')] if y_method == 'individual_trade' else [str(OUT / 'queries/index_yield/data.json')]
            method = y_method
            note = '保留真实成交日期；不是中债估值。' if y_method == 'individual_trade' else '缺少同日个券成交，提供指数均值参考；未做期限匹配，不表示个券定价。'
        if col in {'K','P','L','M','Q','N','T'}:
            function, source_field = 'local_calculation', None
            deps = {'K':['J','G'], 'P':['J','G'], 'L':['J','I'], 'M':['J','G','I'],
                    'Q':['H','E'], 'N':['D'], 'T':['S']}[col]
            method = {'K':'J+0.25*G','P':'J+0.25*G','L':'(J-I)*100',
                      'M':'(J+0.25*G-1.25*I)*100','Q':'H*E',
                      'N':'30 if D>=25 else floor(D+0.5)','T':'int(S<2025-08-08)'}[col]
            if col in {'K','P','L','M'}:
                actual = ('成交收益率' if y_method == 'individual_trade' else '指数收益率参考') + '：' + actual
                note = '采用用户允许的替代输入并沿用原表公式；' + (
                    '按成交收益率与票息计算的调整收益参考。' if y_method == 'individual_trade' and col in {'K','P'} else
                    '属于成交收益率相对曲线的参考差值。' if y_method == 'individual_trade' else
                    '是指数基准参考计算，不是个券实测收益或估值利差。')
                evidence += [str(OUT/'queries/index_yield/data.json') if y_method == 'index_reference' else str(OLD/'spot_deals/response-01.json')]
                if col in {'L','M'}:
                    evidence += [str(OLD/'local_curve_aaa/data.json')]
            if col == 'N':
                note = '分组规则沿用原表，剩余期限继承D列Actual/365替代计年口径。'
            if col == 'Q':
                note = '仅研究参考；输入为发行规模和指数久期，禁止解释为真实逐券风险敞口或据此计算真实组合久期。'
                evidence += [str(OUT/'queries/index_duration/data.json')]
        if col in {'O','R'}:
            function, source_field = 'bond_info_detail_cm', 'exerciseInfoFlag'
            note = '原始行权标志不涵盖完整本金提前偿还安排；存在与原表不一致样本，不能用否或占位值填补。'
        if matured and is_market:
            status, note = 'not_applicable', '按已披露合同到期日，该券在评估日已到期；不生成当前收益、久期或风险参考。'
        elif value is None:
            status = 'missing'
        own_source = {'function':primary_result['function'], 'arguments':primary_result['arguments'],
                      'retrieved_at':primary_result['checked_at_utc'], 'source_field':source_field,
                      'execution_mode':'compatibility_adapter' if primary_result['case'].startswith('detail_typed_') or primary_result['case']=='traded_detail' else 'documented_sdk'}
        sources = [] if function == 'local_calculation' else [own_source]
        for dependency in deps:
            sources += fields_in_row[dependency]['input_sources']
        sources = list({json.dumps(source,sort_keys=True):source for source in sources}.values())
        entry = {'target_column': col, 'original_target_name': original, 'actual_metric_name': actual,
            'value': value, 'unit': unit, 'status': status, 'status_label': LABELS[status],
            'is_acquired': status in {'obtained','derived','obtained_substitute','derived_substitute'},
            'is_proxy': 'substitute' in status, 'entity_grain': grain,
            'source_entity_id': src_entity, 'target_entity_id': code, 'target_entity_name': name,
            'source_date': src_date, 'requested_as_of_date': AS_OF.isoformat(),
            'retrieved_at': max(source['retrieved_at'] for source in sources),
            'normalized_at':datetime.now(timezone.utc).isoformat(),
            'source_function': function, 'source_field': source_field,
            'source_arguments':primary_result['arguments'] if function!='local_calculation' else {},
            'input_sources':sources,
            'proxy_or_calculation_method': method, 'dependencies': deps, 'evidence': evidence,
            'coverage_scope': 'five_sample_bonds', 'allow_actual_portfolio_risk': False if col in {'E','H','Q'} else None,
            'note': note, 'raw_exercise_flag': d.get('exerciseInfoFlag') if col in {'O','R'} else None}
        observations.append(entry)
        fields_in_row[col] = entry
    row_summaries.append({'code':code, 'name':name, 'matured':matured, 'yield_method':y_method,
                         'values':values, 'maturity_date':maturity.isoformat()})

# Verify internal relationships and keep unknowns distinct from genuine zero.
for row in row_summaries:
    v = row['values']
    assert v['O'] is None and v['R'] is None
    if row['matured']:
        assert all(v[col] is None for col in ['H','I','J','K','L','M','P','Q'])
    else:
        assert math.isclose(v['K'], v['P'])
        assert math.isclose(v['M'], (v['K']-1.25*v['I'])*100)
        assert math.isclose(v['Q'], v['H']*v['E'])
        assert math.isclose(v['L'], (v['J']-v['I'])*100)

field_map = []
for col, original, actual, status, unit, grain in SPECS:
    cells = [item for item in observations if item['target_column'] == col]
    acquired = [item for item in cells if item['is_acquired']]
    field_map.append({'column':col,'original_target_name':original,'actual_metric_name':actual,
        'status':status if acquired else 'missing','status_label':LABELS[status] if acquired else LABELS['missing'],
        'is_acquired':bool(acquired),'unit':unit,'entity_grain':grain,
        'sample_acquired_count':len(acquired),'sample_total':len(cells),
        'scope_note':'字段能力与5只样本的取得情况，不代表14,023只全量覆盖。',
        'example_value':acquired[0]['value'] if acquired else None,
        'example_actual_metric_name':acquired[0]['actual_metric_name'] if acquired else actual})
assert len(field_map) == 20 and sum(item['is_acquired'] for item in field_map) == 18
save(OUT / 'normalized-observations.json', observations)
save(OUT / 'field-availability.json', field_map)
save(OUT / 'sample-values.json', row_summaries)

def csv_write(name, columns, rows):
    with (OUT/name).open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=columns,extrasaction='ignore')
        writer.writeheader(); writer.writerows(rows)

csv_write('字段获取状态.csv', ['column','original_target_name','actual_metric_name','status_label','unit',
    'entity_grain','sample_acquired_count','sample_total','example_value','scope_note'], field_map)
csv_write('规范化样本数据.csv', ['target_entity_id','target_entity_name','target_column','original_target_name',
    'actual_metric_name','value','unit','status_label','entity_grain','source_entity_id','source_date',
    'requested_as_of_date','source_function','proxy_or_calculation_method','note'], observations)

contracts = read(OUT/'query-contracts.json')
reused = [
    ('lookup_general',['债券简称','债券代码','发行日期']),
    ('local_issues_april',['债券代码','债券简称','实际发行总量','发行起始日','交易市场']),
    ('spot_deals',['债券简称','成交净价','最新收益率','加权收益率','交易量']),
    ('spot_quotes',['报价机构','债券简称','买入净价','卖出净价','买入收益率','卖出收益率']),
    ('curve_catalog',['value','cnLabel','enLabel']),
    ('local_curve_aaa',['日期','期限','到期收益率','即期收益率','远期收益率']),
    ('local_curve_aaa_minus',['日期','期限','到期收益率','即期收益率','远期收益率']),
]
curve_names={row['cnLabel'] for row in read(OLD/'curve_catalog/data.json')}
for case, columns in reused:
    r=read(OLD/case/'result.json')
    signature=inspect.signature(getattr(ak,r['function']))
    signature.bind(**r['arguments'])
    assert set(columns).issubset(r['columns'])
    if r['function']=='bond_china_close_return':
        assert r['arguments']['symbol'] in curve_names
    for key in ['start_date','end_date']:
        if key in r['arguments']:
            assert len(r['arguments'][key])==8
            datetime.strptime(r['arguments'][key],'%Y%m%d')
    contracts.append({'case':case,'function':r['function'],'arguments':r['arguments'],
        'dictionary_url':DOC,'installed_signature':str(signature),'expected_columns':columns,
        'schema_valid':True,'execution_mode':'reused_verified_sdk_response',
        'checked_at_utc':r['checked_at_utc'],'result':str(OLD/case/'result.json')})
save(OUT/'normalized-query-registry.json',contracts)
summary={'as_of_date':AS_OF.isoformat(),'akshare_version':ak.__version__,
    'field_count':20,'acquired_including_substitutes':18,'missing_columns':['O','R'],
    'status_counts':dict(Counter(item['status'] for item in field_map)),
    'new_validated_query_cases':6,'reused_query_contracts':len(reused),
    'sample_bonds':len(row_summaries),'market_reference_applicable_bonds':sum(not r['matured'] for r in row_summaries),
    'matched_trade_bonds':len(matched),'original_workbook_bonds':len(original_codes),
    'index_duration_reference':index_duration,'index_yield_reference_pct':index_yield,
    'scope':'Field-level capability, including user-authorized substitutes. Not full-universe completion.'}
save(OUT/'summary.json',summary)

lines=['# 按AKShare数据字典规范化查询与替代字段结果','',
    '**按允许替代的字段能力口径，20列中18列已有实值或计算结果；O、R两列提前偿还安排仍未取得可靠替代。** 该统计包含计算和替代，基于5只样本，不表示14,023只债券每列均已取得。','',
    '## 查询规范','',
    f'依据：[AKShare官方债券数据字典]({DOC})；安装并实测版本 {ak.__version__}。',
    '本次新增6组查询全部返回符合约定结构的数据，并复用前轮7组已验证查询。接口参数先按实际函数签名绑定，债券类型和指数名称使用官方枚举结果验证；日期使用YYYYMMDD，输出检查字典要求的列名。','',
    '- 债券类型：bond_info_cm_query(symbol="债券类型")，返回30种。',
    '- 可选指数：bond_available_index_cbond()，返回313项。',
    '- 指数久期：bond_index_general_cbond(index_category="地方政府债指数", indicator="平均市值法久期", period="总值")。',
    '- 指数收益率：同接口，indicator="平均市值法到期收益率"。',
    '- 个券查询：bond_info_cm(bond_code="101948", bond_type="地方政府债")；按返回简称查询23四川18详情。',
    '- 字典的指数查询参数表写成periods，但官方示例和实际签名均为period，本次采用period并记录差异。',
    '- 详情函数只接受symbol；内部补传bond_type的修复标为compatibility_adapter，不伪称原版接口直接成功。','',
    '最新成交没有历史日期参数。本轮市场计算统一采用2026-09-11；原表2026-06-17的历史估值不被当前行情冒充。指数接口返回历史序列，按指定日精确筛选，不偷偷用最新值填历史值。','',
    '## 字段获取状态','',
    '| 列 | 原字段 | 新状态 | 实际取得指标 | 样本取得数 |','|---|---|---|---|---:|']
for item in field_map:
    lines.append(f"| {item['column']} | {item['original_target_name']} | {item['status_label']} | {item['actual_metric_name']} | {item['sample_acquired_count']}/{item['sample_total']} |")
lines += ['', '取得数小于5的市场指标，是因为24湖北债16已经到期；其当前收益、久期及相应派生指标记为不适用。','',
    '## 替代值如何解释','',
    f'- 久期基准已取得：地方政府债指数2026-09-11平均市值法久期为 **{index_duration}年**，是指数参考，不是各只债券的中债修正久期。',
    f'- 收益率优先采用同日个券成交；缺少成交时明确展示地方债指数平均市值法到期收益率 **{index_yield}%** 作为参考。指数均值未匹配个券期限，不能据此断言个券贵便宜。',
    '- 余额缺失时，把发行规模标为“已获取（替代）”，实际字段保留“发行规模”名称。已到期券发行量仍可为正。',
    '- Q使用“指数久期×发行规模”作为单独的研究参考值，禁止将其视为逐券真实风险敞口，或用它生成真实余额加权组合久期。',
    '- K/P、L/M按原表公式使用替代输入计算，结果继承替代状态；J若为指数均值，差值也明确标为指数参考差值，而不是个券估值利差。',
    '- exerciseInfoFlag=否不能覆盖本金分期还本条款。25江西债68已有反例，O/R保留未知；空值和“---”不填成0或否。','',
    '## 新增有成交样本：23四川18','']
s=next(row for row in row_summaries if row['code']=='101948.IB'); v=s['values']
lines += [f"2026-09-11：成交收益率 **{v['J']:.4f}%**，票息 **{v['G']:.4f}%**，同期限CFETS AAA曲线插值 **{v['I']:.6f}%**。",
    f"按替代口径，K/P={v['K']:.6f}%，L={v['L']:.4f} BP，M={v['M']:.4f} BP。这些是已生成的计算结果，完整来源见规范化样本数据。",'',
    '## 文件','',
    '- [字段获取状态](字段获取状态.csv)：20列能力清单与替代标签。',
    '- [规范化样本数据](规范化样本数据.csv)：5只债券、100个字段记录；每条保留原字段、实际指标、值、状态、粒度、来源日和说明。',
    '- [完整机器可读结果](normalized-observations.json)、[查询注册表](normalized-query-registry.json)、[基准指标](benchmark-indicators.json)。',
    '- [规范查询脚本](../../scripts/query_akshare_bond_dictionary.py)、[结果整理脚本](../../scripts/build_akshare_bond_dictionary_results.py)。',
    '- queries目录保留本次原始响应、SDK结果和请求记录；前轮证据通过注册表路径引用。','',
    '现券成交对原表仍为358只覆盖，报价接口仍有首页分页限制。允许替代改变的是字段可用性判定，不会扩大已经实际验证的个券覆盖。']
(OUT/'规范化查询结果.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
print(json.dumps(summary,ensure_ascii=False))
