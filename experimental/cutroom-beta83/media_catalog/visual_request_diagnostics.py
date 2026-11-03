"""Read/write bounded diagnostic receipts without changing frozen job inputs."""
import json
from .visual_protocol import safe_visual_diagnostics


def record_visual_request_diagnostics(connection, job_id, diagnostics, *, item_key=''):
    safe = safe_visual_diagnostics(diagnostics)
    if not safe:
        return
    connection.execute("""INSERT INTO visual_request_diagnostics(job_id,item_key,diagnostics_json)
        VALUES(?,?,?) ON CONFLICT(job_id,item_key) DO UPDATE SET
        diagnostics_json=excluded.diagnostics_json,updated_at=datetime('now')""",
        (job_id,str(item_key),json.dumps(safe,sort_keys=True)))


def read_visual_request_diagnostics(connection, job_id):
    return [{'itemKey':row['item_key'],'diagnostics':safe_visual_diagnostics(json.loads(row['diagnostics_json']))}
            for row in connection.execute('SELECT item_key,diagnostics_json FROM visual_request_diagnostics WHERE job_id=? ORDER BY item_key',(job_id,))]
