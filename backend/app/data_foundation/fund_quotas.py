"""QDII category reports preserve display text without invented monetary units."""
import json
from app.data_foundation.canonical import FoundationError


def required_text(row, key):
    value = row.get(key)
    if not isinstance(value, str):
        raise FoundationError('SOURCE_SCHEMA_INVALID', 'QDII报告文本缺失或类型无效，未将缺项改成零。')
    return value


def quota_body(source, raw):
    rows = raw.get('item')
    if not isinstance(rows, list) or len(rows) > 1:
        raise FoundationError('SOURCE_SCHEMA_INVALID', 'QDII请求分类须返回明确的零或一个分类容器，未合并冲突分类。')
    if not isinstance(source.subject, str) or not source.subject.strip():
        raise FoundationError('IDENTITY_UNRESOLVED', 'QDII观察缺少固定请求分类。')
    if raw.get('category_scope') not in (None, 'explicit_categories_only'):
        raise FoundationError('SOURCE_SCHEMA_INVALID', 'QDII观察的分类覆盖范围未经验证。')
    scope = raw.get('collection_scope')
    if scope is not None:
        try:
            valid = isinstance(scope, dict) and json.loads(scope['tab']) == [source.subject]
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            raise FoundationError('IDENTITY_CONFLICT', 'QDII请求分类与固定来源主体不一致。')
    groups = []
    summary = source.dataset == 'fund_quota_summary'
    for row in rows:
        if not isinstance(row, dict) or row.get('name') != source.subject:
            raise FoundationError('IDENTITY_CONFLICT', 'QDII响应包含其他分类，整组隔离。')
        group = dict(category_key=source.subject)
        if summary:
            for field in ('buy', 'total', 'total_limit', 'unlimited'):
                group['reported_' + field + '_text'] = required_text(row, field)
        else:
            if not isinstance(row.get('sub_tab'), list):
                raise FoundationError('SOURCE_SCHEMA_INVALID', 'QDII分类缺少明确的子分类列表。')
            subcategories = []
            for tab in row['sub_tab']:
                if not isinstance(tab, dict) or not isinstance(tab.get('fund_list'), list):
                    raise FoundationError('SOURCE_SCHEMA_INVALID', 'QDII子分类缺少明确的基金列表，整组隔离。')
                funds = []
                for fund in tab['fund_list']:
                    if not isinstance(fund, dict):
                        raise FoundationError('SOURCE_SCHEMA_INVALID', 'QDII基金成员不是有效对象，整组隔离。')
                    code = required_text(fund, 'thscode')
                    if '.' not in code or not code.strip():
                        raise FoundationError('IDENTITY_UNRESOLVED', 'QDII基金成员缺少来源代码。')
                    # These three source states are present in the actual full
                    # capture. An omitted classification is not an empty list.
                    classify = fund.get('classify')
                    state = 'absent' if 'classify' not in fund else 'null' if classify is None else 'value'
                    if classify is not None and (not isinstance(classify, list) or any(not isinstance(v, str) for v in classify)):
                        raise FoundationError('SOURCE_SCHEMA_INVALID', 'QDII基金分类标签类型无效，整组隔离。')
                    funds.append(dict(source_code=code, fund_name=required_text(fund, 'fund_name'),
                        reported_quota_text=required_text(fund, 'quota'), reported_year_text=required_text(fund, 'year'),
                        classify_presence=state, reported_classify=classify))
                subcategories.append(dict(category_key=required_text(tab, 'name'), funds=sorted(funds, key=lambda f: f['source_code'])))
            group['subcategories'] = sorted(subcategories, key=lambda g: g['category_key'])
        groups.append(group)
    body = dict(collection_key=source.subject, scope_basis='explicit_requested_category_only', reported_groups=groups,
        comparable_quota_amounts=None, quota_currency=None, current_subscription_eligibility=None)
    quality = dict(comparable_quota_amounts='QUOTA_UNITS_AND_FORMULA_UNVERIFIED', quota_currency='CURRENCY_UNVERIFIED',
        current_subscription_eligibility='CURRENT_ELIGIBILITY_UNVERIFIED')
    if not summary:
        body['annual_return'] = None
        quality['annual_return'] = 'REPORTED_YEAR_MEANING_UNVERIFIED'
    return body, quality
