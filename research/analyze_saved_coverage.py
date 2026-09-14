"""Read-only, consistent snapshot of saved AKShare bond aggregation coverage."""
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
import csv
import json
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
TARGET = '2026-09-11'
OUT = ROOT / 'research' / 'coverage-analysis-20260912'
OUT.mkdir(exist_ok=True)
db = sqlite3.connect((ROOT / 'runtime' / 'wind-wind.sqlite3').as_uri() + '?mode=ro', uri=True)
db.row_factory = sqlite3.Row
db.execute('BEGIN')
captured = datetime.now().astimezone().isoformat(timespec='seconds')
row = db.execute('SELECT collected_at,payload FROM akshare_datasets WHERE target_date=?', (TARGET,)).fetchone()
dataset = json.loads(row['payload'])
jobrow = db.execute('SELECT payload FROM akshare_sync_jobs WHERE target_date=? ORDER BY rowid DESC LIMIT 1', (TARGET,)).fetchone()
job = json.loads(jobrow['payload']) if jobrow else {}
item_counts = {r['status']: r['n'] for r in db.execute('SELECT status,COUNT(*) n FROM akshare_sync_items WHERE job_id=? GROUP BY status', (job.get('jobId'),))}
partitions = [dict(r) for r in db.execute('SELECT year,status,total,attempts,error,updated_at FROM akshare_sync_partitions WHERE job_id=? ORDER BY year DESC', (job.get('jobId'),))]
details = {r['code']: json.loads(r['payload']) for r in db.execute('SELECT code,payload FROM akshare_details')}
failures = [dict(r) for r in db.execute("SELECT code,status,error,attempts,updated_at FROM akshare_sync_items WHERE job_id=? AND status IN ('failed','unmapped') LIMIT 20", (job.get('jobId'),))]
db.close()
bonds = dataset['bonds']
# Use the implementation's versioned term set, never infer it from populated cells.
import sys
sys.path.insert(0, str(ROOT))
from backend.domain import TERMS
terms = set(TERMS)
required = ['regionId','bondType','issueDate','remainingYears','issueAmountYi','yieldPct']
static = required[:-1]
counter = lambda key: dict(Counter(str(b.get(key)) for b in bonds))
missing = {key: sum(b.get(key) is None for b in bonds) for key in required + ['maturityDate','couponPct','duration','outstandingBalanceYi']}
reason = Counter(b.get('reason') for b in bonds)
combos = Counter(' + '.join(k for k in required if b.get(k) is None) or 'none' for b in bonds)
def deceased(b):
    return bool(b.get('maturityDate') and b['maturityDate'] <= TARGET)
def eligible_except_yield(b):
    return all(b.get(k) is not None for k in static) and not deceased(b) and b['issueDate'] <= TARGET and Decimal(b['issueAmountYi']) > 0 and b.get('termYears') in terms
def example(b):
    return {key:b.get(key) for key in ['code','name','disposition','reason','staticSyncStatus',*required,'maturityDate','termYears','yieldDate','cohort']}
waterfall = Counter()
groups = defaultdict(Counter)
for b in bonds:
    if deceased(b): bucket = 'matured'
    elif b.get('issueDate') and b['issueDate'] > TARGET: bucket = 'future_issue'
    elif any(b.get(k) is None for k in static): bucket = 'missing_required_static'
    elif Decimal(b['issueAmountYi']) <= 0: bucket = 'nonpositive_amount'
    elif b.get('termYears') not in terms: bucket = 'outside_term_grid'
    elif b.get('yieldPct') is None: bucket = 'only_missing_target_day_yield'
    else: bucket = 'eligible'
    b['_diagnosticBucket'] = bucket
    waterfall[bucket] += 1
    groups[str(b.get('cohort'))][b['disposition']] += 1
yield_bonds = [b for b in bonds if b.get('yieldPct') is not None]
eligible = [b for b in bonds if b['disposition']=='eligible']
summary = dict(capturedAt=captured,targetDate=TARGET,datasetCollectedAt=row['collected_at'],
               counts=dataset['counts'],job=job,liveItemCounts=item_counts,partitions=partitions,
               savedStaticStatus=counter('staticSyncStatus'),missingFields=missing,
               missingCombinations=dict(combos),reasons=dict(reason),exclusiveWaterfall=dict(waterfall),
               terms=sorted(terms),cohorts={k:dict(v) for k,v in groups.items()},
               cachedDetailCount=len(details),failureExamples=failures,
               datasetProvenance=dataset.get('provenance'),datasetFullSync=dataset.get('fullSync'),
               bondCount=len(bonds),distinctCodes=len({b['code'] for b in bonds}),
               yieldCoverageCount=len(yield_bonds),yieldDates=dict(Counter(b.get('yieldDate') for b in yield_bonds)),
               yieldsByDisposition=dict(Counter(b['disposition'] for b in yield_bonds)),
               staticReadyForAggregation=sum(eligible_except_yield(b) for b in bonds),
               coreStaticComplete=sum(all(b.get(k) is not None for k in static) for b in bonds),
               indexReferenceCount=sum(b.get('referenceFields',{}).get('indexYieldPct') is not None for b in bonds),
               eligibleRegions=dict(Counter(b.get('regionId') for b in eligible)),
               eligibleTerms=dict(Counter(b.get('termYears') for b in eligible)),
               eligibleTypes=dict(Counter(b.get('bondType') for b in eligible)),
               examples={key:[example(b) for b in bonds if b['_diagnosticBucket']==key][:3] for key in waterfall})
(OUT / 'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
columns = ['code','name','cohort','disposition','_diagnosticBucket','staticSyncStatus',*required,'maturityDate','termYears','yieldDate','reason']
with (OUT / 'bond-diagnostics.csv').open('w',encoding='utf-8-sig',newline='') as f:
    writer = csv.DictWriter(f,fieldnames=columns,extrasaction='ignore')
    writer.writeheader()
    writer.writerows(bonds)
print(json.dumps({k:v for k,v in summary.items() if k not in {'examples','datasetFullSync','datasetProvenance','partitions','job'}},ensure_ascii=False,indent=2))
print(json.dumps({'job':{k:job.get(k) for k in ['jobId','status','phase','updatedAt','catalogComplete','currentYear']},'partitions':dict(Counter(p['status'] for p in partitions))},ensure_ascii=False,indent=2))
