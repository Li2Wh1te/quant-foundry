"""Stable, non-sensitive errors for the current data store."""

MESSAGES = {
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
