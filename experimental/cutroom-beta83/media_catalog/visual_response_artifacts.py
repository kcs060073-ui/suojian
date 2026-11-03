"""Private, bounded provider evidence. Never return these paths in public job data."""
import hashlib
import json
import os
from pathlib import Path

MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def _immutable_write(path, content):
    try:
        with path.open('xb') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    except FileExistsError:
        if path.read_bytes() != content:
            raise ValueError('VISUAL_RESPONSE_ARTIFACT_CONFLICT: 原始响应证据已存在且不一致') from None


def persist_visual_response(database_path, raw, *, task_id, job_id, item_index,
                            asset_fingerprint, input_digest, sample_times_us):
    """Keep original bytes and immutable binding; oversized replies retain metadata only."""
    if not isinstance(raw, (str, bytes)) or type(item_index) is not int or item_index < 0:
        raise ValueError('VISUAL_RESPONSE_ARTIFACT_INVALID: 响应证据类型无效')
    data = raw.encode('utf-8') if isinstance(raw, str) else raw
    identity = dict(taskId=task_id, jobId=job_id, itemIndex=item_index,
                    assetFingerprint=asset_fingerprint, inputDigest=input_digest,
                    sampleTimesUs=list(sample_times_us))
    # Hash IDs instead of interpreting them as paths. This private tree is outside
    # task artifacts, static routes and diagnostic-log collection.
    scope = hashlib.sha256(json.dumps([task_id, job_id], ensure_ascii=False).encode()).hexdigest()
    root = Path(database_path).resolve().parent / 'private' / 'visual-responses'
    folder = root / scope / str(item_index)
    folder.mkdir(parents=True, exist_ok=True)
    if not folder.resolve().is_relative_to(root.resolve()):
        raise ValueError('VISUAL_RESPONSE_ARTIFACT_INVALID: 证据路径无效')
    oversized = len(data) > MAX_RESPONSE_BYTES
    metadata = dict(identity, responseSha256=hashlib.sha256(data).hexdigest(),
                    responseBytes=len(data), oversized=oversized, version=1)
    encoded = json.dumps(metadata, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    # Receipt first preserves response arrival even if disk writing is interrupted.
    receipt = folder / 'receipt.json'
    _immutable_write(receipt, encoded)
    if oversized:
        raise ValueError('VISUAL_RESPONSE_TOO_LARGE: 响应超过2MiB，已保留摘要证据并停止解析')
    _immutable_write(folder / 'response.bin', data)
    return receipt
