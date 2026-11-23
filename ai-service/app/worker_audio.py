from pathlib import Path

from faster_whisper.audio import decode_audio

from .dialect_inference import classify_windows
from .dialects import dialect_windows, vietnamese


def speech_intervals(media, device, language, speaker_checkpoint=None, dialect_checkpoint=None):
    from faster_whisper.vad import get_speech_timestamps

    audio = decode_audio(str(media))
    status = {'dialect_status': 'not_configured'} if vietnamese(language) else {}
    spans = None
    if speaker_checkpoint:
        try:
            spans = diarize(audio, speaker_checkpoint, device)
        except (ImportError, OSError, ValueError, RuntimeError, AttributeError) as error:
            print(f'Diarization unavailable ({type(error).__name__}).')
    engine = 'pyannote' if spans else 'silero'
    if not spans:
        try:
            spans = [{'start_ms': sample['start'] // 16, 'end_ms': sample['end'] // 16}
                     for sample in get_speech_timestamps(audio) if sample['end'] // 16 > sample['start'] // 16]
        except (ImportError, OSError, ValueError, RuntimeError) as error:
            print(f'Silero interval extraction unavailable ({type(error).__name__}).')
            return {'dialect_status': 'unavailable'} if dialect_checkpoint and vietnamese(language) else status
    if dialect_checkpoint and vietnamese(language):
        status = {'dialect_status': 'insufficient_speech', 'dialect_model': Path(dialect_checkpoint).name}
        try:
            windows = dialect_windows(spans, keep_short=engine == 'pyannote')
            if windows:
                spans = classify_windows(audio, windows, dialect_checkpoint, device)
                if any(span['dialect_confidence'] is not None for span in spans):
                    status['dialect_status'] = 'completed'
        except (ImportError, OSError, ValueError, RuntimeError) as error:
            status['dialect_status'] = 'unavailable'
            print(f'Dialect checkpoint unavailable ({type(error).__name__}).')
    return {**status, 'speech_intervals': spans[:20000], 'speech_detector': engine}


def diarize(audio, checkpoint, device):
    import torch
    from pyannote.audio import Pipeline

    pipeline = Pipeline.from_pretrained(str(checkpoint))
    pipeline.to(torch.device(device))
    result = pipeline({'waveform': torch.from_numpy(audio).unsqueeze(0), 'sample_rate': 16000})
    spans = []
    for turn, _, speaker in result.exclusive_speaker_diarization.itertracks(yield_label=True):
        start, end = max(0, round(turn.start * 1000)), round(turn.end * 1000)
        if end > start:
            spans.append({'start_ms': start, 'end_ms': end, 'speaker': speaker})
    return spans
