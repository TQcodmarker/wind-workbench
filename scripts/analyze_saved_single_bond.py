"""Calculate the existing one-bond response locally, without MCP calls or publication."""
import json
import sys
from pathlib import Path

from backend.domain import REGIONS, RULES, calculate, classify
from backend.wind_mapping import Merge, VERSION

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / 'runtime/single-bond-ytm-809336-20260911.json'
OUTPUT = ROOT / 'outputs/26河北23-已有数据计算-20260911.json'


def main():
    saved = json.loads(SOURCE.read_text(encoding='utf-8'))
    code, target = saved['code'], saved['targetDate']
    merged = Merge(target)
    for item in saved['tests']:
        if item.get('result'):
            merged.add(item['result'], item['requestId'], expected=[code])
    record, sources = merged.normalize(code)
    decision = classify(record, target)
    cells, counts = calculate([record], target)
    groups = [cell for cell in cells if cell['sampleCount']]
    result = dict(source='wind_mcp_saved_response', sourcePath=str(SOURCE),
                  sourceSessionId=saved['sessionId'], evaluationDate=target,
                  scope='single_bond_sample', newWindDataCalls=0, publishedSnapshot=False,
                  rules=RULES, fieldMappingVersion=VERSION, record=record,
                  decision=decision, counts=counts, groups=groups, fieldSources=sources)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    descriptive = record['_descriptive']
    values = [
        ('债券代码', code), ('债券简称', descriptive['name']),
        ('发行主体', descriptive['issuer']), ('地区', next((r['name'] for r in REGIONS if r['id']==record['regionId']),record['regionId'])),
        ('债券分类', {'general':'一般债','special':'专项债'}.get(record['bondType'],record['bondType'])), ('发行日期（发行起始日期）', record['issueDate']),
        ('到期日', descriptive['maturityDate']), ('收益率（%）', record['yieldPct']),
        ('剩余期限（年）', record['remainingYears']), ('发行规模（亿元）', record['issueAmountYi']),
        ('收盘价修正久期', descriptive['duration']), ('票面利率（%）', descriptive['couponPct']),
        ('债券余额（亿元）', descriptive['outstandingBalanceYi']), ('收盘净价（元）', descriptive['closeNetPrice']),
    ]
    lines = ['# 26河北23：使用已有 MCP 数据计算', '',
             f'查询日期：{target}。复用原会话 `{saved["sessionId"]}` 的返回，没有新增 Wind 查询。', '',
             '当前规则暂不采集、记录或筛选提前偿还及发行人赎回信息；非空 MCP 值按查询日期归档。原始响应仍作为历史证据保留。', '',
             '| 已有数据 | 采用值 |', '|---|---|']
    lines += [f'| {label} | {value if value is not None else "—"} |' for label, value in values]
    decision_label = '可纳入当前计算' if decision['disposition']=='included' else '未纳入当前计算'
    lines += ['', f'计算判定：**{decision_label}**（{decision["reason"]}）。', '',
              '| 发行日期组 | 地区 | 期限 | 口径 | 样本数 | 发行规模合计（亿元） | 加权收益率（%） |',
              '|---|---|---|---|---:|---:|---:|']
    for group in groups:
        cohort = '2025-08-08 当日及以后' if group['cohort']=='on_or_after_20250808' else '2025-08-08 之前'
        scope = {'all':'整体','general':'一般债','special':'专项债'}[group['bondScope']]
        lines.append(f'| {cohort} | 河北省 | {group["termYears"]} 年 | {scope} | {group["sampleCount"]} | {group["issueAmountSumYi"]} | {group["yieldPct"]} |')
    lines += ['', '以上仅为这只债券的计算结果，未作为全国完整行情快照发布。', '',
              f'规则：`{RULES["version"]}`；字段映射：`{VERSION}`。', '',
              f'[查看计算明细及字段来源]({OUTPUT.as_posix()})', '']
    OUTPUT.with_suffix('.md').write_text('\n'.join(lines), encoding='utf-8')
    print(json.dumps({'decision':decision,'counts':counts,'groups':groups,'output':str(OUTPUT)}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
