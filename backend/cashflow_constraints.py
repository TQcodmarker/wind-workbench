"""Positive cash-flow clause hints from the user's original workbook.

The bundled extract contains only positive flags and their cell provenance.
Blank/zero workbook cells never certify a bullet bond. Applying a hint creates
a copy and leaves the collected provider detail untouched. A dated workbook
duration is deliberately not imported as a current individual-bond duration.
"""
from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path
import re


_DATA = Path(__file__).resolve().parent / 'data' / 'cashflow_constraints.json'
_CODE = re.compile(r'^\d{6,9}(?:\.IB)?$')


@lru_cache(maxsize=1)
def _constraints():
    data = json.loads(_DATA.read_text(encoding='utf-8'))
    return data, {entry['code']: entry for entry in data['constraints']}


def apply_cashflow_constraints(code, detail):
    """Return a detail copy with known positive clause flags and source evidence.

    ``earlyRepayment=True`` may be resolved by a verified full repayment
    schedule in ``calculate_duration``. ``redemption=True`` remains unsupported
    by the conventional fixed-cash-flow model. No negative flag is synthesized.
    """
    copied = deepcopy(detail)
    normalized = str(code or '').strip().upper()
    if not _CODE.fullmatch(normalized):
        return copied
    if not normalized.endswith('.IB'):
        normalized += '.IB'
    metadata, by_code = _constraints()
    entry = by_code.get(normalized)
    if entry is None:
        return copied
    existing = copied.get('cashflowConstraintEvidence') or []
    evidence = list(existing) if isinstance(existing, list) else [str(existing)]
    for marker in entry['markers']:
        if marker['kind'] == 'early_repayment':
            copied['earlyRepayment'] = True
        elif marker['kind'] == 'redemption':
            copied['redemption'] = True
        else:
            # An unrecognized future schema must not fabricate a constraint.
            continue
        source_text = (f"用户原表 {metadata['sourceFile']} / {metadata['sheet']} / "
                       f"{marker['cell']}（原表评估日 {metadata['evaluationDate']}）："
                       f"{marker['rawValue']}。结构性条款提示，不含完整还本或行权计划。")
        if source_text not in evidence:
            evidence.append(source_text)
    if evidence:
        copied['cashflowConstraintEvidence'] = evidence
    return copied
