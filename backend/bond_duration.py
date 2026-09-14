"""Auditable, self-calculated duration for conventional fixed-rate bonds.

Yield is in percent. Cash flows are discounted at the nominal annual yield,
compounded at the coupon frequency. Fractional time is measured against the
actual surrounding coupon period, rather than dividing calendar days by 365.
This is a model calculation, never an official provider duration.

Optional verified amortization input (amounts per original face value of 100)::

    repaymentSchedule=[{'date': '2030-01-01', 'principalPct': '50'}, ...],
    repaymentScheduleVerified=True,
    repaymentScheduleSource='Prospectus URL/page or local evidence reference'

The complete principal schedule must sum to 100, end at maturity, and use the
regular coupon dates. Coupons accrue on the principal outstanding before each
payment. A provider exerciseInfoFlag of "否" is not repayment evidence.
"""
from calendar import monthrange
from datetime import date, datetime
from decimal import Decimal, DecimalException, localcontext
from collections.abc import Mapping


METHOD_VERSION = 'fixed-cashflow-modified-duration-v1'
DURATION_BUCKETS = (3, 5, 7, 10, 15, 20, 30)
_EMPTY = {'', '-', '--', '---', 'N/A', 'NA', 'NULL', 'NONE'}
_FALSE = {'否', '无', '不适用', 'false', 'no', '0'}
_FIXED = {'附息式固定利率', '固定利率', 'fixed', 'fixed_rate'}
_FREQUENCIES = {'年': 1, '每年': 1, 'annual': 1, '半年': 2, '每半年': 2,
                'semiannual': 2, 'semi-annual': 2}
_ACTUAL_COUPON = {'ACT/ACT', 'ACTUAL/ACTUAL', 'ACT/ACT(ICMA)',
                  'ACTUAL/ACTUAL(ICMA)', 'ACT/ACTISMA', '实际/实际'}


class _Unavailable(ValueError):
    def __init__(self, reason, fields=()):
        super().__init__(reason)
        self.fields = list(fields)


def _empty(value):
    return value is None or (isinstance(value, str) and value.strip().upper() in _EMPTY)


def _decimal(value, field):
    if _empty(value):
        raise _Unavailable(f'缺少 {field}', [field])
    if isinstance(value, bool):
        raise _Unavailable(f'{field} 必须是有限数值', [field])
    try:
        result = Decimal(str(value).strip())
    except (DecimalException, ValueError):
        raise _Unavailable(f'{field} 必须是有限数值', [field]) from None
    if not result.is_finite():
        raise _Unavailable(f'{field} 必须是有限数值', [field])
    return result


def _date(value, field):
    if _empty(value):
        raise _Unavailable(f'缺少 {field}', [field])
    # Timestamps are intentionally not silently truncated to a different date.
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        if not isinstance(value, str) or len(value) != 10:
            raise ValueError
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except ValueError:
        raise _Unavailable(f'{field} 日期无效，要求 YYYY-MM-DD', [field]) from None


def duration_bucket(value):
    """Nearest configured duration bucket; midpoint ties go to the higher one."""
    duration = _decimal(value, 'duration')
    if duration <= 0:
        raise ValueError('久期必须大于 0')
    return min(DURATION_BUCKETS, key=lambda item: (abs(duration - item), -item))


def _month_date(anchor, offset, month_end):
    month_index = anchor.year * 12 + anchor.month - 1 + offset
    year, month_zero = divmod(month_index, 12)
    last_day = monthrange(year, month_zero + 1)[1]
    return date(year, month_zero + 1, last_day if month_end else min(anchor.day, last_day))


def _coupon_schedule(start, first, maturity, frequency):
    if not start < first <= maturity:
        raise _Unavailable('起息日、首次付息日和到期日顺序无效',
                           ['frstValueDate', 'frstCpnDt', 'mrtyDate'])
    months = 12 // frequency
    span = (maturity.year - start.year) * 12 + maturity.month - start.month
    if span <= 0 or span > 2400:
        raise _Unavailable('付息日期跨度无法按常规债券处理', ['mrtyDate'])
    # Test the contractual fixed calendar day first, then month-end rolling.
    # EOM avoids losing October 31 after an April 30 semiannual payment.
    for month_end in (False, True):
        if month_end and maturity.day != monthrange(maturity.year, maturity.month)[1]:
            continue
        backward = [_month_date(maturity, -offset, month_end)
                    for offset in range(0, span + months + 1, months)]
        if start not in backward:
            continue
        dates = list(reversed(backward[:backward.index(start) + 1]))
        if len(dates) >= 2 and dates[1] == first:
            return dates
    raise _Unavailable('首次付息或到期安排不是可验证的规则付息周期；需完整现金流规则',
                       ['couponSchedule'])


def _positive_flag(value):
    if _empty(value) or value is False or value == 0:
        return False
    return str(value).strip().lower() not in _FALSE


def _check_clauses(detail, has_verified_schedule):
    for field in ('redemption', 'redemptionFlag', 'callable', 'puttable',
                  'putOption', 'complexTerms', 'exerciseInfoFlag'):
        if _positive_flag(detail.get(field)):
            raise _Unavailable(f'{field} 显示未建模的选择权或复杂条款', [field])
    for entry in detail.get('exerciseInfoList') or []:
        if not isinstance(entry, Mapping):
            raise _Unavailable('行权信息格式无效', ['exerciseInfoList'])
        if any(not _empty(entry.get(key)) for key in ('exerciseType', 'exerciseDate')):
            raise _Unavailable('存在行权安排，需核验完整条款及现金流', ['exerciseInfoList'])
    for field in ('earlyRepayment', 'earlyRepaymentFlag', 'amortizing', 'amortizationFlag'):
        if _positive_flag(detail.get(field)) and not has_verified_schedule:
            raise _Unavailable('已知存在提前或分期还本，但缺少可靠还本计划', ['repaymentSchedule'])
    for field in ('repaymentType', 'repaymentMethod', 'principalRepayment', 'note'):
        value = str(detail.get(field) or '')
        if any(marker in value for marker in ('分期还本', '分期偿还', '提前偿还', '提前还本', '摊还')):
            if not has_verified_schedule:
                raise _Unavailable('存在提前或分期还本描述，但缺少可靠还本计划', ['repaymentSchedule'])
        if any(marker in value for marker in ('赎回权', '回售权', '可赎回', '可回售', '利率调整')):
            raise _Unavailable('存在未建模的选择权或利率调整条款', [field])


def _principal_schedule(detail, schedule, assumptions, missing):
    raw = detail.get('repaymentSchedule')
    if raw is None:
        assumptions.append('本金偿还安排未核验，暂按到期一次偿还本金估算；exerciseInfoFlag=否不能证明无分期还本')
        missing.append('repaymentSchedule')
        return {schedule[-1]: Decimal(100)}, False
    if (detail.get('repaymentScheduleVerified') is not True
            or _empty(detail.get('repaymentScheduleSource'))):
        raise _Unavailable('还本计划缺少核验标识或来源证据',
                           ['repaymentScheduleVerified', 'repaymentScheduleSource'])
    if not isinstance(raw, list) or not raw:
        raise _Unavailable('还本计划必须是非空列表', ['repaymentSchedule'])
    payments = {}
    previous = schedule[0]
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise _Unavailable('还本计划记录格式无效', ['repaymentSchedule'])
        when = _date(entry.get('date'), 'repaymentSchedule.date')
        amount = _decimal(entry.get('principalPct'), 'repaymentSchedule.principalPct')
        if when <= previous or when not in schedule[1:] or amount <= 0:
            raise _Unavailable('还本计划必须按规则付息日期递增，且本金金额为正', ['repaymentSchedule'])
        payments[when] = amount
        previous = when
    if sum(payments.values()) != Decimal(100) or previous != schedule[-1]:
        raise _Unavailable('完整还本计划必须按原始面值 100 合计还清，并结束于到期日', ['repaymentSchedule'])
    return payments, True


def _format(value):
    rounded = value.quantize(Decimal('0.00000001'))
    # Preserve positivity for unusual but valid yields that give tiny durations.
    return format(value, '.8E') if value != 0 and rounded == 0 else format(rounded, 'f')


def _evidence_value(value):
    """Keep input evidence serializable even when rejected numbers are nonfinite."""
    if isinstance(value, Mapping):
        return {str(key): _evidence_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_evidence_value(item) for item in value]
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (Decimal, float)):
        return str(value)
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)


def calculate_duration(detail, target, yield_pct):
    """Return JSON-safe duration, bucket, assumptions and rejected-input reasons.

    Missing optional day count/principal evidence is listed in ``missingFields``
    even when ``status == 'estimated'``. Such estimates are usable only with that
    qualification. ``calculated`` additionally requires verified principal cash
    flows, ACT/ACT day-count input, and ``yieldCompounding='coupon_frequency'``.
    It still means self-calculated, not an official market-source value.
    """
    fields = ('parCouponRate', 'couponType', 'couponFrqncy', 'frstValueDate',
              'frstCpnDt', 'mrtyDate', 'intrstBss', 'exerciseInfoFlag',
              'repaymentSchedule', 'repaymentScheduleVerified',
              'repaymentScheduleSource', 'yieldCompounding', 'cashflowConstraintEvidence')
    result = dict(status='unavailable', modifiedYears=None, macaulayYears=None,
                  bucketYears=None, methodVersion=METHOD_VERSION, assumptions=[],
                  missingFields=[], reason='', inputs={})
    if not isinstance(detail, Mapping):
        result['reason'] = '债券详情必须是字段字典'
        return result
    result['inputs'] = {key: _evidence_value(detail.get(key)) for key in fields}
    result['inputs'].update(targetDate=_evidence_value(target),
                            yieldPct=str(yield_pct) if yield_pct is not None else None)
    assumptions, missing = result['assumptions'], result['missingFields']
    try:
        with localcontext() as ctx:
            ctx.prec = 34
            # Collect all missing required fields, useful to acquisition/UI callers.
            required = ('parCouponRate', 'couponType', 'couponFrqncy',
                        'frstValueDate', 'frstCpnDt', 'mrtyDate')
            absent = [key for key in required if _empty(detail.get(key))]
            if _empty(target):
                absent.append('targetDate')
            if _empty(yield_pct):
                absent.append('yieldPct')
            if absent:
                raise _Unavailable('缺少久期计算所需字段：' + '、'.join(absent), absent)
            if str(detail['couponType']).strip() not in _FIXED:
                raise _Unavailable('仅支持规则付息的固定利率债；浮息、贴现发行等需专用现金流规则', ['couponType'])
            frequency = _FREQUENCIES.get(str(detail['couponFrqncy']).strip().lower())
            if frequency is None:
                raise _Unavailable('仅支持每年或每半年付息', ['couponFrqncy'])
            coupon = _decimal(detail['parCouponRate'], 'parCouponRate')
            annual_yield = _decimal(yield_pct, 'yieldPct') / 100
            base = 1 + annual_yield / frequency
            if coupon < 0:
                raise _Unavailable('票息不能为负', ['parCouponRate'])
            if base <= 0:
                raise _Unavailable('收益率必须大于负的年付息频率乘以 100%', ['yieldPct'])
            settlement = _date(target, 'targetDate')
            start = _date(detail['frstValueDate'], 'frstValueDate')
            first = _date(detail['frstCpnDt'], 'frstCpnDt')
            maturity = _date(detail['mrtyDate'], 'mrtyDate')
            if settlement < start or settlement >= maturity:
                raise _Unavailable('目标日须不早于起息日且早于到期日', ['targetDate'])
            schedule = _coupon_schedule(start, first, maturity, frequency)
            principal, verified = _principal_schedule(detail, schedule, assumptions, missing)
            _check_clauses(detail, verified)
            day_count = detail.get('intrstBss')
            if _empty(day_count):
                assumptions.append('计息基准未提供，按 Actual/Actual 付息周期分数估算')
                missing.append('intrstBss')
            elif str(day_count).upper().replace(' ', '') not in _ACTUAL_COUPON:
                raise _Unavailable('当前模型不支持该计息基准；不能替换为 Actual/Actual', ['intrstBss'])
            convention = detail.get('yieldCompounding')
            if not _empty(convention) and convention != 'coupon_frequency':
                raise _Unavailable('当前模型仅支持按年付息频率复利的名义年收益率', ['yieldCompounding'])
            if _empty(convention):
                assumptions.append('收益率复利口径未核验，暂按年付息频率复利的名义年收益率计算')
                missing.append('yieldCompounding')
            assumptions.append('采用合同日历付息日期，不推算节假日顺延；目标日到期的当期本息按已支付处理')
            next_index = next(index for index, when in enumerate(schedule) if when > settlement)
            period_start, next_coupon = schedule[next_index - 1:next_index + 1]
            fraction = Decimal((next_coupon - settlement).days) / Decimal((next_coupon - period_start).days)
            price, weighted_time = Decimal(0), Decimal(0)
            outstanding = Decimal(100)
            for index, when in enumerate(schedule[1:], 1):
                repayment = principal.get(when, Decimal(0))
                cashflow = outstanding * coupon / 100 / frequency + repayment
                outstanding -= repayment
                if when <= settlement:
                    continue
                periods = fraction + index - next_index
                years = periods / frequency
                present_value = cashflow / (base ** periods)
                price += present_value
                weighted_time += present_value * years
            if price <= 0:
                raise _Unavailable('目标日之后没有有效的正现金流')
            macaulay = weighted_time / price
            modified = macaulay / base
            if not modified.is_finite() or modified <= 0:
                raise _Unavailable('计算结果不是有限的正久期')
            # Bucket from the unrounded value, preserving close midpoint decisions.
            status = 'calculated' if verified and not _empty(day_count) and not _empty(convention) else 'estimated'
            result.update(status=status, modifiedYears=_format(modified),
                          macaulayYears=_format(macaulay), bucketYears=duration_bucket(modified),
                          reason=('根据已核验还本计划自行计算的个券修正久期（非官方久期）' if status == 'calculated'
                                  else '基于目标日收益率和已列明假设估算的个券修正久期（非官方久期）'))
            result['inputs'].update(frequencyPerYear=frequency, dayCountUsed='Actual/Actual coupon period',
                                    principalModel='verified_schedule' if verified else 'assumed_bullet',
                                    fullPricePer100=_format(price), futureCouponCount=len(schedule) - next_index,
                                    nextCouponDate=next_coupon.isoformat())
    except _Unavailable as exc:
        result['reason'] = str(exc)
        missing.extend(key for key in exc.fields if key not in missing)
    except (DecimalException, ValueError, TypeError, OverflowError) as exc:
        # Invalid provider input is data unavailability, not an endpoint failure.
        result.update(status='unavailable', modifiedYears=None, macaulayYears=None, bucketYears=None)
        result['reason'] = '现金流输入或计算范围无效：' + type(exc).__name__
    return result
