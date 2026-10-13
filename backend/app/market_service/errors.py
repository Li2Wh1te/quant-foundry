"""Safe market errors: never relay SQL, paths, evidence bodies or credentials."""
from app.data_store.errors import DataStoreError
from sqlalchemy.exc import SQLAlchemyError
from fastapi import HTTPException

MESSAGES = {
    'IDENTITY_UNKNOWN': '未找到获准的标的身份。',
    'IDENTITY_UNAVAILABLE': '该标的缺少可核验的可信代码映射。',
    'IDENTITY_AMBIGUOUS': '代码对应多个身份，请使用明确的标的入口。',
    'IDENTITY_MAPPING_UNAVAILABLE': '可信身份映射资料暂不可用。',
    'CAPABILITY_UNAVAILABLE': '该业务能力尚未提供。',
    'PRICE_BASIS_UNVERIFIED': '所选价格口径尚未核验。',
    'UNIT_UNVERIFIED': '所选能力缺少已声明单位。',
    'FACTOR_ANCHOR_UNVERIFIED': '复权所需的基准或锚点尚未核验。',
    'FACTOR_UNAVAILABLE': '所选复权缺少对应业务日的合格因子，未前填或改选口径。',
    'NUMERIC_RANGE_UNSUPPORTED': '复权数值超过共享精确算术范围，未输出近似价格。',
}


class MarketError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(MESSAGES[code])


def http_error(error: MarketError | DataStoreError | SQLAlchemyError) -> HTTPException:
    if isinstance(error, SQLAlchemyError):
        state = getattr(getattr(error, 'orig', None), 'sqlstate', None)
        error = DataStoreError('LOCK_TIMEOUT' if state == '55P03' else
                               'QUERY_TIMEOUT' if state == '57014' else 'CATALOG_UNAVAILABLE')
    status = (404 if error.code == 'IDENTITY_UNKNOWN' else
              409 if error.code in {'IDENTITY_AMBIGUOUS', 'DATA_CHANGED', 'DATA_RESTRICTED', 'REBUILD_REQUIRED'} else
              503 if error.code in {'DATA_STORE_REBUILDING', 'DATA_STORE_NOT_INITIALIZED',
                                    'CATALOG_UNAVAILABLE', 'STORAGE_UNAVAILABLE', 'LOCK_TIMEOUT',
                                    'IDENTITY_MAPPING_UNAVAILABLE'} else 422)
    return HTTPException(status, detail={'code': error.code, 'message': str(error)})
