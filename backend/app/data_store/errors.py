"""Stable, non-sensitive errors for the current data store."""

MESSAGES = {
    'DATA_CHANGED': '当前数据已改变，请放弃本次分页并重新读取。',
    'REBUILD_REQUIRED': '当前字段、规则或口径不兼容，需要重建受影响分区。',
    'SCHEMA_UNSUPPORTED': '当前存储不支持该字段类型或精度，请使用明确的精确编码。',
    'DATASET_MISSING': '尚未登记此数据集。',
    'KEY_ORDER_INVALID': '输入业务键必须严格递增且唯一。',
    'SOURCE_CONFLICT': '当前来源确认依据已改变，不能提交该输入。',
    'REPORT_INCOMPLETE': '整份报告的完整性尚未确认，未提交任何成员。',
    'BATCH_BUDGET_EXCEEDED': '批次或相关分片超过限制，请按独立范围分批处理。',
    'QUERY_BUDGET_EXCEEDED': '查询结果超过限制，请缩小范围或页大小。',
    'QUERY_TIMEOUT': '操作达到时间上限，未返回不完整结果。',
    'MEMORY_PRESSURE': '进程内存达到限制，已停止本次操作。',
    'INVALID_CURSOR': '查询游标无效或不属于本次查询。',
    'HISTORY_UNSUPPORTED': '当前底座不提供历史发布或快照查询。',
    'COMMIT_UNKNOWN': '提交结果暂不能确认，请恢复核查，勿盲目重试或删除文件。',
    'CATALOG_UNAVAILABLE': '当前目录暂不可访问。',
    'CATALOG_MISMATCH': '目录数据库与本机共享存储不匹配。',
    'FILE_INVALID': '当前文件不完整或不可读取，未返回数据。',
    'ISSUE_BUDGET_EXCEEDED': '当前问题超过上限，请聚合受影响范围并处理限制。',
    'INVALID_VALUE': '数据的类型、数值精度或时间不符合当前契约。',
    'INVALID_CONFIGURATION': '数据存储的资源上限配置无效。',
    'CONTROL_BUDGET_EXCEEDED': '当前状态信息超过大小或复杂度上限。',
    'SCRATCH_BUDGET_EXCEEDED': '暂存与查询临时空间已达到共同上限。',
    'DISK_PRESSURE': '磁盘剩余空间不足，已停止新增写入。',
    'GARBAGE_BUDGET_EXCEEDED': '待清理文件已达到上限，请先完成清理。',
    'LOCK_TIMEOUT': '数据正在提交或处理，请稍后重试。',
    'OPERATION_CANCELLED': '操作已取消。',
    'UNSUPPORTED_FILESYSTEM': '当前文件系统尚未支持安全写入，请使用受支持的本机共享目录。',
    'UNSAFE_STORAGE_PATH': '数据存储路径或锁文件不符合安全要求。',
    'STORAGE_UNAVAILABLE': '无法访问数据存储，请检查本机目录及权限。',
    'LOCK_CONTEXT_EXPIRED': '写入锁上下文已失效，不能继续提交。',
}


class DataStoreError(ValueError):
    """Only allow fixed public messages, never paths, SQL or provider payloads."""

    def __init__(self, code: str):
        if code not in MESSAGES:
            raise ValueError('Unknown data-store error code')
        self.code = code
        super().__init__(MESSAGES[code])
