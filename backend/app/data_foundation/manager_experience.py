"""Convert documented local manager-history structure without guessing ratios.

The immutable source subject identifies the manager. Dictionary keys identify
funds only when corroborated by each child's explicit code. The collection is
atomic so a malformed child cannot silently become a reported departure.
"""
from app.data_foundation.canonical import FoundationError


def experience_body(source, raw):
    manager = source.subject
    history = raw.get('investment_history')
    if not isinstance(manager, str) or not manager.strip():
        raise FoundationError('IDENTITY_UNRESOLVED', '经理经历缺少固定来源经理标识。')
    if not isinstance(history, dict):
        raise FoundationError('SOURCE_SCHEMA_INVALID', '经理经历缺少明确的任职集合，未按空集合发布。')
    assignments, quality = [], {}
    for code, entry in sorted(history.items()):
        if (not isinstance(code, str) or not code.strip() or not isinstance(entry, dict)
                or entry.get('code') != code):
            raise FoundationError('IDENTITY_CONFLICT', '经理任职基金代码与集合键不一致，保留整组待修复。')
        item = {'fund_code': code}
        for original, target in (('name', 'name'), ('hqcode', 'quotation_code'), ('type', 'fund_type'),
                                 ('start', 'start_text'), ('end', 'end_text')):
            value = entry.get(original)
            if value is not None and not isinstance(value, str):
                raise FoundationError('SOURCE_SCHEMA_INVALID', '经理任职文本类型无效，未将异常成员当作退出。')
            item[target] = value
        assignments.append(item)
    # Raw quantitative details stay in source evidence, never canonical JSON.
    # Field-level reasons are visible to the shared strict/partial read gate.
    quality['performance'] = 'PERFORMANCE_UNITS_UNVERIFIED' if any(
        any(entry.get(key) is not None for key in ('hs_rate', 'sh_rate', 'rate1_b0300',
            'rate_common_type_avg_fq_net', 'sy_info')) for entry in history.values()) else 'MISSING'
    for field in ('awards', 'heavy_assets'):
        quality[field] = 'SEMANTICS_UNVERIFIED' if raw.get(field) is not None else 'MISSING'
    return dict(manager_id=manager, assignments=assignments, performance=None, awards=None, heavy_assets=None), quality
