from bisect import bisect_right
from collections import defaultdict
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class SpeechInterval(BaseModel):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0, le=10**12)
    speaker: str | None = Field(default=None, max_length=28, pattern=r'^[A-Za-z0-9_-]+$')
    dialect: Literal['NORTHERN', 'CENTRAL', 'SOUTHERN', 'UNKNOWN'] = 'UNKNOWN'
    dialect_confidence: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode='after')
    def validate_range(self):
        if self.end_ms <= self.start_ms:
            raise ValueError('Speech interval end must follow its start.')
        return self


def save_speech_intervals(cursor, job, intervals, detector):
    if intervals is None:
        return None
    job_id = job['job_id']
    cursor.execute('''SELECT audio_id,duration_ms FROM AUDIO_ASSET
                      WHERE job_id=%s AND audio_type IN ('NORMALIZED','DENOISED')
                      ORDER BY audio_type='DENOISED' DESC,audio_id DESC LIMIT 1''', (job_id,))
    asset = cursor.fetchone()
    if not asset:
        return None

    cursor.execute('DELETE FROM SPEECH_SEGMENT WHERE job_id=%s', (job_id,))
    spans, ends, ids = [], [], {}
    dialects = defaultdict(lambda: defaultdict(float))
    compared_duration = defaultdict(int)
    voiced_duration = defaultdict(int)
    for span in sorted(intervals, key=lambda item: (item.start_ms, item.end_ms)):
        start, end = max(span.start_ms, ends[-1] if ends else 0), min(span.end_ms, asset['duration_ms'])
        if start >= end:
            continue
        if span.speaker and span.speaker not in ids:
            cursor.execute('INSERT INTO SPEAKER (video_id,speaker_label) VALUES (%s,%s)',
                           (job['video_id'], f"{job_id}:{span.speaker}"))
            ids[span.speaker] = cursor.lastrowid
        speaker_id = ids.get(span.speaker)
        if speaker_id is not None:
            voiced_duration[speaker_id] += end - start
        if speaker_id is not None and span.dialect != 'UNKNOWN' and span.dialect_confidence is not None:
            dialects[speaker_id][span.dialect] += (end - start) * span.dialect_confidence
            compared_duration[speaker_id] += end - start
        spans.append((start, end, speaker_id, span.dialect, span.dialect_confidence))
        ends.append(end)
    if spans:
        cursor.executemany('''INSERT INTO SPEECH_SEGMENT
            (audio_id,job_id,start_ms,end_ms,speaker_id,dialect_label,dialect_confidence)
            VALUES (%s,%s,%s,%s,%s,%s,%s)''',
            [(asset['audio_id'], job_id, *span) for span in spans])
    for speaker_id, scores in dialects.items():
        label = max(scores, key=scores.get)
        if compared_duration[speaker_id] < voiced_duration[speaker_id] * 0.5 or scores[label] < sum(scores.values()) * 0.7:
            continue
        cursor.execute('UPDATE SPEAKER SET dialect_label=%s,dialect_confidence=%s WHERE speaker_id=%s',
                       (label, scores[label] / compared_duration[speaker_id], speaker_id))
    cursor.execute('''UPDATE PROCESSING_STEP
                      SET metrics=JSON_SET(COALESCE(metrics,JSON_OBJECT()),'$.detector',%s,'$.speakers',%s,
                                           '$.speech_intervals',%s)
                      WHERE job_id=%s AND step_type='TRANSCRIBE' ''', (detector, len(ids), len(spans), job_id))
    return spans, ends


def speaker_for_range(speech, start, end):
    if speech is None:
        return None
    spans, ends = speech
    scores = defaultdict(int)
    for index in range(bisect_right(ends, start), len(spans)):
        left, right, speaker, _, _ = spans[index]
        if left >= end:
            break
        if speaker:
            scores[speaker] += min(end, right) - max(start, left)
    return max(scores, key=scores.get) if scores else None
