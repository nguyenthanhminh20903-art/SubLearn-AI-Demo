import importlib.util
import json
import logging
import os
import subprocess
import sys

from .dialects import REGIONS, dialect_windows, vietnamese
from .media import MODEL_DIRECTORY, cpu_thread_count, local_model_path, voiced_ranges


logger = logging.getLogger(__name__)


def local_dialect_runtime():
    checkpoint = local_model_path(os.getenv('SUBTITLE_DIALECT_CHECKPOINT', 'vn-dialect'))
    if not checkpoint or not all((checkpoint / filename).is_file() for filename in
                                 ('config.json', 'preprocessor_config.json', 'model.safetensors')):
        return None
    runtime = MODEL_DIRECTORY.parent / 'dialect-runtime'
    python = runtime / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if python.is_file():
        return python, checkpoint
    if all(importlib.util.find_spec(package) for package in ('torch', 'transformers', 'safetensors')):
        return sys.executable, checkpoint
    return None


def classify_local_dialect(audio, language, segments, *, runtime=None):
    if not vietnamese(language):
        return {}
    runtime = runtime or local_dialect_runtime()
    if not runtime:
        return {'dialect_status': 'not_configured'}
    python, checkpoint = runtime
    from faster_whisper.audio import decode_audio

    ranges, detector = voiced_ranges(decode_audio(str(audio)), padding_ms=80)
    windows = dialect_windows([{'start_ms': start // 16, 'end_ms': end // 16} for start, end in ranges])
    if not windows and detector != 'silero':
        windows = dialect_windows(segments)
        detector = 'whisper_asr'
    if not windows:
        return {'dialect_status': 'insufficient_speech', 'dialect_model': checkpoint.name}
    try:
        output = subprocess.run(
            [str(python), '-m', 'app.dialect_inference', str(audio.resolve()), str(checkpoint),
             '--threads', str(min(cpu_thread_count(), 4))],
            cwd=MODEL_DIRECTORY.parent.parent, input=json.dumps(windows),
            capture_output=True, encoding='utf-8', timeout=min(1200, 90 + len(windows) * 8), check=True,
        )
        intervals = json.loads(output.stdout)
        if not isinstance(intervals, list) or len(intervals) != len(windows):
            raise ValueError('Incomplete dialect results')
        if any(span['dialect'] in REGIONS for span in intervals):
            status = 'completed'
        elif any(span['dialect_confidence'] is not None for span in intervals):
            status = 'uncertain'
        else:
            status = 'insufficient_speech'
    except (OSError, subprocess.SubprocessError, TypeError, ValueError, KeyError) as error:
        logger.warning('Local dialect inference stopped: %s', type(error).__name__)
        return {'dialect_status': 'unavailable', 'dialect_model': checkpoint.name}
    return {'dialect_status': status, 'dialect_model': checkpoint.name,
            'speech_intervals': intervals, 'speech_detector': detector}
