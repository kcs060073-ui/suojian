"""Conservative P0 disk estimates without proxy-media assumptions."""


def estimate_p0_bytes(file_count: int) -> int:
    if file_count < 0:
        raise ValueError("文件数量不能小于 0")
    return file_count * 8192 * 120 // 100
