"""Bounded user-facing facts from frozen voice execution receipts; no state changes."""
import json

_FAILURE_CODES = {'TTS_AUDIO_DECODE_FAILED', 'TTS_PROVIDER_REJECTED', 'TTS_HTTP_REJECTED', 'TTS_STREAM_INCOMPLETE', 'TTS_RESPONSE_UNKNOWN'}

def summarize_voice_generation(input_json, execution_json):
    try:
        inputs = json.loads(input_json)
        execution = json.loads(execution_json or '{}')
        if not isinstance(inputs, dict) or not isinstance(execution, dict): return None
        parts = inputs.get('segments')
        if not isinstance(parts, list) or not 0 < len(parts) <= 100: return None
        ids = [p.get('segment_id') for p in parts if isinstance(p, dict)]
        if len(ids) != len(parts) or any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids): return None
        selected = inputs.get('generation_segment_ids') if inputs.get('schema_version') == 2 else ids
        if not isinstance(selected, list) or not selected or any(not isinstance(i, str) or i not in ids for i in selected) or len(set(selected)) != len(selected): return None
        events = execution.get('events', [])
        if not isinstance(events, list): return None
        latest = {}
        for event in events:
            if isinstance(event, dict) and event.get('phase') == 'result' and event.get('segment_id') in selected:
                latest[event['segment_id']] = event
        completed = 0
        failed_number = None
        failure_code = None
        for identifier in selected:
            event = latest.get(identifier, {})
            decoded = event.get('decoded')
            frames = decoded.get('frame_count') if isinstance(decoded, dict) else None
            if event.get('status') == 'succeeded' and type(frames) is int and frames > 0:
                completed += 1
            elif event.get('status') in {'failed', 'unknown'} and failed_number is None:
                failed_number = ids.index(identifier) + 1
                code = event.get('error_code')
                failure_code = code if isinstance(code, str) and code in _FAILURE_CODES else None
        return dict(totalSegments=len(selected), completedSegments=completed, failedSegmentNumber=failed_number, failureCode=failure_code)
    except (TypeError, ValueError):
        return None

def voice_progress_for_job(db, job):
    if job['job_type'] != 'tts': return None
    row = db.connection.execute('SELECT input_json, execution_json FROM voice_generation_runs WHERE job_id=?', (job['job_id'],)).fetchone()
    return summarize_voice_generation(row['input_json'], row['execution_json']) if row is not None else None
