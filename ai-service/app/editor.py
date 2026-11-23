import json
import hashlib
import logging
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, Union
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .context_translation import contextual_model, translate_subtitle_cues
from .mysql_api import (Owner, TranslationInput, UPLOADS, local_lock, owned_video,
                        save_revision, save_translation, subtitle_time, transaction, uploaded_media_path,
                        translation_lock_available, translation_targets)
from .subtitle_algorithms import caption_costs, normalize, TimedText
from .subtitle_import import MAX_IMPORT_BYTES, read_subtitles
from .translate import PAIRS, translate_batch


router = APIRouter(prefix='/api/v1', tags=['editor'])
EXPORTS = UPLOADS.parent / 'exports'
logger = logging.getLogger(__name__)


def owned_track(cursor, track_id, user_id, lock=False):
    cursor.execute('''SELECT t.track_id,t.video_id,t.source_track_id,t.language,v.duration_ms
                      FROM SUBTITLE_TRACK t JOIN VIDEO v ON v.video_id=t.video_id
                      JOIN PROJECT p ON p.project_id=v.project_id WHERE t.track_id=%s AND p.user_id=%s'''
                   + (' FOR UPDATE' if lock else ''), (track_id, user_id))
    row = cursor.fetchone()
    if row is None:
        raise HTTPException(404, 'Không tìm thấy track phụ đề.')
    return row


def segments(cursor, track_id):
    cursor.execute('''SELECT segment_id,sequence_number,start_ms,end_ms,content,confidence,speaker_id
                      FROM SUBTITLE_SEGMENT WHERE track_id=%s ORDER BY sequence_number''', (track_id,))
    return cursor.fetchall()


def current_version(cursor, track_id):
    cursor.execute('SELECT COALESCE(MAX(version_no),0) AS version FROM SUBTITLE_REVISION WHERE track_id=%s',
                   (track_id,))
    return cursor.fetchone()['version']


class TrackTranslation(TranslationInput):
    mode: Literal['local', 'beam', 'context'] = 'local'


class GlossaryTerm(TranslationInput):
    source_term: str = Field(min_length=1, max_length=255)
    translated_term: str = Field(min_length=1, max_length=255)


@router.get('/glossary')
def glossary_terms(source: Literal['vi', 'en'], target: Literal['vi', 'en'], user_id: Owner):
    with transaction(user_id) as cursor:
        cursor.execute('''SELECT CAST(glossary_id AS CHAR) AS id,source_term,translated_term FROM GLOSSARY
                          WHERE user_id=%s AND source_lang=%s AND target_lang=%s ORDER BY source_term''',
                       (user_id, source, target))
        return cursor.fetchall()


@router.put('/glossary')
def save_glossary_term(body: GlossaryTerm, user_id: Owner):
    source, target = body.source_term.strip(), body.translated_term.strip()
    if not source or not target or (body.source_language, body.target_language) not in PAIRS:
        raise HTTPException(422, 'Nhập thuật ngữ và bản dịch cho hai ngôn ngữ khác nhau.')
    with transaction(user_id) as cursor:
        cursor.execute('''INSERT INTO GLOSSARY (user_id,source_lang,target_lang,source_term,translated_term)
                          VALUES (%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE translated_term=%s''',
                       (user_id, body.source_language, body.target_language, source, target, target))
    return {'saved': True}


@router.delete('/glossary/{term_id}', status_code=204)
def delete_glossary_term(term_id: int, user_id: Owner):
    with transaction(user_id) as cursor:
        cursor.execute('DELETE FROM GLOSSARY WHERE glossary_id=%s AND user_id=%s', (term_id, user_id))
        if not cursor.rowcount:
            raise HTTPException(404, 'Không tìm thấy thuật ngữ.')


@router.post('/tracks/{track_id}/translations', status_code=202)
def queue_translation(track_id: int, body: TrackTranslation, user_id: Owner, tasks: BackgroundTasks):
    if (body.source_language, body.target_language) not in PAIRS:
        raise HTTPException(422, 'Hiện hỗ trợ dịch Việt ↔ Anh.')
    model = contextual_model() if body.mode == 'context' else None
    if body.mode == 'context' and model is None:
        raise HTTPException(503, 'Máy chủ chưa cấu hình mô hình dịch theo ngữ cảnh.')
    environment = 'GPU_SERVER' if model else 'LOCAL'
    with transaction(user_id) as cursor:
        track = owned_track(cursor, track_id, user_id, lock=True)
        version = current_version(cursor, track_id)
        if version < 1 or not segments(cursor, track_id):
            raise HTTPException(422, 'Track nguồn chưa có phụ đề.')
        parameters = {'track_id': track_id, 'version': version, 'owner_id': user_id, 'mode': body.mode,
                      'source_language': body.source_language, 'target_language': body.target_language}
        cursor.execute('''INSERT INTO PROCESSING_JOB (video_id,job_type,processing_parameters)
                          VALUES (%s,'TRANSLATE',%s)''', (track['video_id'], json.dumps(parameters)))
        job_id = cursor.lastrowid
        model_id = None
        if model:
            cursor.execute("""SELECT model_id FROM MODEL_CONFIG WHERE model_name=%s AND model_type='TRANSLATION'
                              AND execution_environment=%s AND is_active=TRUE ORDER BY model_id DESC LIMIT 1""",
                           (model[1], environment))
            record = cursor.fetchone()
            if record:
                model_id = record['model_id']
                cursor.execute('INSERT INTO JOB_MODEL (job_id,model_id) VALUES (%s,%s)', (job_id, model_id))
        cursor.execute('''INSERT INTO PROCESSING_STEP (job_id,model_id,sequence_number,step_type,execution_environment,parameters)
                          VALUES (%s,%s,1,'TRANSLATE',%s,%s)''',
                       (job_id, model_id, environment, json.dumps({'mode': body.mode, 'model': model[1] if model else 'OPUS-MT'})))
    tasks.add_task(run_translation, job_id)
    return {'job_id': str(job_id), 'status': 'pending'}


def run_translation(job_id):
    with local_lock:
        with transaction() as cursor:
            cursor.execute("""SELECT status,processing_parameters FROM PROCESSING_JOB
                              WHERE job_id=%s AND job_type='TRANSLATE' FOR UPDATE""", (job_id,))
            job = cursor.fetchone()
            if not job or job['status'] != 'PENDING':
                return
            cursor.execute("UPDATE PROCESSING_JOB SET status='RUNNING',started_at=CURRENT_TIMESTAMP(3) WHERE job_id=%s",
                           (job_id,))
            cursor.execute("UPDATE PROCESSING_STEP SET status='RUNNING',started_at=CURRENT_TIMESTAMP(3) WHERE job_id=%s",
                           (job_id,))
        try:
            params = json.loads(job['processing_parameters'])
            owner, track_id, version = params['owner_id'], params['track_id'], params['version']
            use_context = params.get('mode') == 'context'
            with transaction(owner) as cursor:
                track = owned_track(cursor, track_id, owner, lock=True)
                _, rows = read_revision(cursor, track_id, version)
                if current_version(cursor, track_id) != version:
                    raise ValueError('Source revision changed.')
                cursor.execute('''SELECT source_term,translated_term FROM GLOSSARY
                                  WHERE user_id=%s AND source_lang=%s AND target_lang=%s''',
                               (owner, params['source_language'], params['target_language']))
                glossary = {row['source_term']: row['translated_term'] for row in cursor.fetchall()}
                model = contextual_model() if use_context else None
                if use_context and not model:
                    raise ValueError('Contextual model is no longer configured.')
                profile = hashlib.sha256(json.dumps([params['source_language'], params['target_language'],
                    params['mode'], model[1] if model else 'OPUS-MT', glossary], sort_keys=True).encode()).hexdigest()
                _, targets = translation_targets(cursor, track, params['target_language'])
                cursor.execute('''SELECT JSON_EXTRACT(processing_parameters,'$.version') AS version,
                                  JSON_UNQUOTE(JSON_EXTRACT(processing_parameters,'$.profile')) AS profile
                                  FROM PROCESSING_JOB WHERE video_id=%s AND job_type='TRANSLATE'
                                  AND status='COMPLETED' AND JSON_EXTRACT(processing_parameters,'$.track_id')=%s
                                  AND JSON_UNQUOTE(JSON_EXTRACT(processing_parameters,'$.target_language'))=%s
                                  ORDER BY job_id DESC LIMIT 1''', (track['video_id'], track_id, params['target_language']))
                previous = cursor.fetchone()
                old = {}
                if previous and previous['profile'] == profile:
                    _, snapshot = read_revision(cursor, track_id, int(previous['version']))
                    old = {row['segment_id']: row['content'] for row in snapshot}
            selected, used = [], set()
            for row in rows:
                identifier = row['segment_id']
                target = targets.get(identifier)
                if not target or target['segment_id'] in used or (
                    not target['is_locked'] and old.get(identifier) != row['content']
                ):
                    selected.append(row)
                if target:
                    used.add(target['segment_id'])
            pending = {str(row['segment_id']) for row in selected}
            texts = {}
            if selected and use_context:
                cues = [(row['segment_id'], row['content']) for row in rows]
                result = translate_subtitle_cues(cues, params['target_language'], glossary, model,
                                                selected=pending, source_language=params['source_language'])
                texts = {row['segment_id']: text for row, text in zip(rows, result, strict=True)
                         if str(row['segment_id']) in pending}
            elif selected:
                result = translate_batch([row['content'] for row in selected], params['source_language'],
                                         params['target_language'], use_beam=params.get('mode') == 'beam',
                                         glossary=glossary)
                texts = {row['segment_id']: text for row, text in zip(selected, result, strict=True)}
            if any(not text.strip() for text in texts.values()):
                raise ValueError('Empty translation output')
            with transaction(owner) as cursor:
                owned_track(cursor, track_id, owner, lock=True)
                if current_version(cursor, track_id) != version:
                    raise ValueError('Source revision changed.')
                cursor.execute('SELECT status FROM PROCESSING_JOB WHERE job_id=%s FOR UPDATE', (job_id,))
                if cursor.fetchone()['status'] != 'RUNNING':
                    return
                target_id = save_translation(cursor, track, rows, [texts.get(row['segment_id']) for row in rows],
                                             params['target_language'], user_id=owner, job_id=job_id)
                cursor.execute("""UPDATE PROCESSING_JOB SET processing_parameters=JSON_SET(processing_parameters,
                                   '$.profile',%s,'$.target_track_id',%s) WHERE job_id=%s""", (profile, target_id, job_id))
                cursor.execute("""UPDATE PROCESSING_STEP SET metrics=JSON_SET(COALESCE(metrics,JSON_OBJECT()),
                                   '$.translated_cues',%s,'$.source_cues',%s) WHERE job_id=%s""",
                               (len(texts), len(rows), job_id))
                set_translation_status(cursor, job_id, 'COMPLETED', 100)
        except Exception:
            logger.exception('Translation job %s failed', job_id)
            with transaction() as cursor:
                cursor.execute('SELECT status FROM PROCESSING_JOB WHERE job_id=%s FOR UPDATE', (job_id,))
                current = cursor.fetchone()
                if current and current['status'] == 'RUNNING':
                    set_translation_status(cursor, job_id, 'FAILED', 0,
                                           'Không dịch được hoặc phiên bản phụ đề nguồn đã thay đổi.')


def set_translation_status(cursor, job_id, status, progress, error=None):
    cursor.execute('''UPDATE PROCESSING_JOB SET status=%s,progress=%s,error_message=%s,
                      completed_at=CURRENT_TIMESTAMP(3) WHERE job_id=%s''', (status, progress, error, job_id))
    cursor.execute('''UPDATE PROCESSING_STEP SET status=%s,error_message=%s,completed_at=CURRENT_TIMESTAMP(3)
                      WHERE job_id=%s''', (status, error, job_id))


class UpdateSegment(BaseModel):
    operation: Literal['update']
    segment_id: int
    content: str = Field(max_length=16000)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)


class SplitSegment(BaseModel):
    operation: Literal['split']
    segment_id: int
    at_ms: int = Field(gt=0)
    left: str = Field(min_length=1, max_length=8000)
    right: str = Field(min_length=1, max_length=8000)


class DeleteSegment(BaseModel):
    operation: Literal['delete']
    segment_id: int


class MergeSegments(BaseModel):
    operation: Literal['merge']
    segment_id: int
    next_segment_id: int


Operation = Annotated[
    Union[UpdateSegment, SplitSegment, DeleteSegment, MergeSegments], Field(discriminator='operation')
]


class TrackEdits(BaseModel):
    baseVersion: int = Field(ge=0)
    operations: list[Operation] = Field(min_length=1, max_length=100)


class LockSegment(BaseModel):
    locked: bool


@router.patch('/tracks/{track_id}/segments/{segment_id}/lock')
def set_translation_lock(track_id: int, segment_id: int, body: LockSegment, user_id: Owner):
    with transaction(user_id) as cursor:
        track = owned_track(cursor, track_id, user_id, lock=True)
        if not track['source_track_id']:
            raise HTTPException(422, 'Chỉ khóa nội dung của track dịch.')
        if not translation_lock_available(cursor):
            raise HTTPException(503, 'Chưa áp dụng migration khóa bản dịch. Chạy 003_translation_segment_lock.sql bằng tài khoản quản trị MySQL.')
        cursor.execute('SELECT segment_id FROM SUBTITLE_SEGMENT WHERE track_id=%s AND segment_id=%s FOR UPDATE',
                       (track_id, segment_id))
        if cursor.fetchone() is None:
            raise HTTPException(404, 'Không tìm thấy đoạn dịch.')
        cursor.execute('UPDATE SUBTITLE_SEGMENT SET is_locked=%s WHERE segment_id=%s',
                       (body.locked, segment_id))
    return {'segment_id': str(segment_id), 'locked': body.locked}


def invalidate(cursor, segment_id):
    cursor.execute('UPDATE WORD_ALIGNMENT SET is_stale=TRUE WHERE segment_id=%s', (segment_id,))
    cursor.execute("UPDATE SUBTITLE_ISSUE SET status='STALE' WHERE segment_id=%s OR related_segment_id=%s",
                   (segment_id, segment_id))


def remove_segment(cursor, segment_id):
    cursor.execute('DELETE FROM SEGMENT_LINK WHERE source_segment_id=%s OR target_segment_id=%s',
                   (segment_id, segment_id))
    cursor.execute('DELETE FROM SUBTITLE_ISSUE WHERE segment_id=%s OR related_segment_id=%s',
                   (segment_id, segment_id))
    cursor.execute('DELETE FROM WORD_ALIGNMENT WHERE segment_id=%s', (segment_id,))
    cursor.execute('DELETE FROM SUBTITLE_SEGMENT WHERE segment_id=%s', (segment_id,))


def copy_links(cursor, old_id, new_id):
    cursor.execute('''INSERT IGNORE INTO SEGMENT_LINK
                      SELECT %s,target_segment_id FROM SEGMENT_LINK WHERE source_segment_id=%s''',
                   (new_id, old_id))
    cursor.execute('''INSERT IGNORE INTO SEGMENT_LINK
                      SELECT source_segment_id,%s FROM SEGMENT_LINK WHERE target_segment_id=%s''',
                   (new_id, old_id))


def merge_word_alignment(cursor, left, right):
    cursor.execute('''SELECT word_id,segment_id,word_index,start_ms,end_ms,word_text,is_stale
                      FROM WORD_ALIGNMENT WHERE segment_id IN (%s,%s) ORDER BY word_index''',
                   (left['segment_id'], right['segment_id']))
    words = cursor.fetchall()
    first = [word for word in words if word['segment_id'] == left['segment_id'] and not word['is_stale']]
    second = [word for word in words if word['segment_id'] == right['segment_id'] and not word['is_stale']]
    if not first or not second or first[-1]['end_ms'] > second[0]['start_ms']:
        return False
    if any(normalize(' '.join(word['word_text'] for word in group)) != normalize(cue['content'])
           for group, cue in ((first, left), (second, right))):
        return False
    offset = max(word['word_index'] for word in words if word['segment_id'] == left['segment_id'])
    cursor.executemany('UPDATE WORD_ALIGNMENT SET segment_id=%s,word_index=%s WHERE word_id=%s',
                       [(left['segment_id'], offset + index, word['word_id']) for index, word in enumerate(second, 1)])
    return True


@router.get('/tracks/{track_id}')
def track_details(track_id: int, user_id: Owner):
    with transaction(user_id) as cursor:
        track = owned_track(cursor, track_id, user_id)
        cursor.execute('SELECT version_no FROM SUBTITLE_REVISION WHERE track_id=%s ORDER BY version_no DESC',
                       (track_id,))
        versions = [row['version_no'] for row in cursor.fetchall()]
        return {**track, 'version': versions[0] if versions else 0, 'versions': versions,
                'segments': segments(cursor, track_id)}


@router.get('/tracks/{track_id}/revisions/{version}')
def revision_details(track_id: int, version: int, user_id: Owner):
    with transaction(user_id) as cursor:
        owned_track(cursor, track_id, user_id)
        _, rows = read_revision(cursor, track_id, version)
    return {'version': version, 'segments': [
        {**row, 'segment_id': str(row['segment_id']),
         'source_segment_ids': [str(identifier) for identifier in row.get('source_segment_ids', [])]}
        for row in rows
    ]}


@router.get('/videos/{video_id}/preview')
def preview(video_id: int, user_id: Owner, track_id: int | None = None, include_segments: bool = True):
    with transaction(user_id) as cursor:
        video = owned_video(cursor, video_id, user_id)
        cursor.execute('SELECT track_id,language,track_type,source_track_id,created_by_job_id IS NULL AS imported '
                       'FROM SUBTITLE_TRACK '
                       'WHERE video_id=%s ORDER BY track_id DESC', (video_id,))
        tracks = cursor.fetchall()
        chosen = next((track for track in tracks if track['track_id'] == track_id), None) if track_id else next(
            (track for track in tracks if track['track_type'] == 'ORIGINAL'), None
        )
        if track_id and not chosen:
            raise HTTPException(404, 'Track không thuộc video.')
        return {
            'video_id': str(video_id), 'title': video['title'], 'duration_ms': video['duration_ms'],
            'media_url': f'/api/v1/videos/{video_id}/media' if video['file_path'] else None,
            'tracks': [{**row, 'track_id': str(row['track_id']), 'imported': bool(row['imported']),
                        'source_track_id': str(row['source_track_id']) if row['source_track_id'] else None}
                       for row in tracks],
            'segments': segments(cursor, chosen['track_id']) if chosen and include_segments else [],
        }


@router.post('/videos/{video_id}/tracks/import', status_code=201)
def import_track(video_id: int, user_id: Owner, file: UploadFile = File(...),
                 language: Literal['vi', 'en'] = Form(...), source_track_id: int | None = Form(None)):
    extension = Path(file.filename or '').suffix.lower()
    if extension not in {'.srt', '.vtt'}:
        raise HTTPException(415, 'Chọn tệp phụ đề SRT hoặc VTT.')
    data = file.file.read(MAX_IMPORT_BYTES + 1)
    if len(data) > MAX_IMPORT_BYTES:
        raise HTTPException(413, 'Tệp phụ đề vượt quá 2 MB.')
    try:
        rows = read_subtitles(data, extension)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    with transaction(user_id) as cursor:
        video = owned_video(cursor, video_id, user_id, lock=True)
        duration = video['duration_ms']
        if duration and any(end > duration for _, end, _ in rows):
            raise HTTPException(422, 'Tệp có đoạn vượt quá thời lượng video.')
        if source_track_id is not None:
            source = owned_track(cursor, source_track_id, user_id, lock=True)
            if source['video_id'] != video_id or source['language'] == language:
                raise HTTPException(422, 'Track nguồn phải cùng video và khác ngôn ngữ.')
            if not translation_lock_available(cursor):
                raise HTTPException(503, 'Cần áp dụng migration khóa bản dịch trước khi nhập bản dịch.')
        cursor.execute('''INSERT INTO SUBTITLE_TRACK (video_id,source_track_id,language,track_type)
                          VALUES (%s,%s,%s,%s)''',
                       (video_id, source_track_id, language, 'TRANSLATION' if source_track_id else 'ORIGINAL'))
        track_id = cursor.lastrowid
        cursor.executemany('''INSERT INTO SUBTITLE_SEGMENT (track_id,sequence_number,start_ms,end_ms,content)
                             VALUES (%s,%s,%s,%s,%s)''',
                           [(track_id, index, *row) for index, row in enumerate(rows, 1)])
        if source_track_id is not None:
            cursor.execute('UPDATE SUBTITLE_SEGMENT SET is_locked=TRUE WHERE track_id=%s', (track_id,))
            cursor.execute('''INSERT IGNORE INTO SEGMENT_LINK (source_segment_id,target_segment_id)
                              SELECT s.segment_id,t.segment_id FROM SUBTITLE_SEGMENT s
                              JOIN SUBTITLE_SEGMENT t ON t.track_id=%s AND t.start_ms<s.end_ms AND t.end_ms>s.start_ms
                              WHERE s.track_id=%s''', (track_id, source_track_id))
        version = save_revision(cursor, track_id, editor_id=user_id)
        issues = record_quality_issues(cursor, track_id)
    return {'track_id': str(track_id), 'version': version, 'segment_count': len(rows), 'issues': issues}


@lru_cache(maxsize=32)
def media_waveform(path, modified_ns):
    import numpy as np
    from faster_whisper.audio import decode_audio

    with path.open('rb') as stream:
        audio = decode_audio(stream, sampling_rate=1000)
    duration_ms = len(audio)
    if not duration_ms:
        return {'peaks': [], 'duration_ms': 0}
    step = max(1, (duration_ms + 1199) // 1200)
    peaks = np.maximum.reduceat(np.abs(audio), np.arange(0, duration_ms, step))
    return {'peaks': [round(float(peak), 3) for peak in peaks], 'duration_ms': duration_ms, 'bucket_ms': step}


@router.get('/videos/{video_id}/waveform')
def waveform(video_id: int, user_id: Owner):
    with transaction(user_id) as cursor:
        path = uploaded_media_path(owned_video(cursor, video_id, user_id))
    try:
        return media_waveform(path, path.stat().st_mtime_ns)
    except (OSError, ValueError) as error:
        raise HTTPException(422, 'Không đọc được waveform của video.') from error


@router.patch('/tracks/{track_id}/segments')
def edit_track(track_id: int, body: TrackEdits, user_id: Owner):
    with transaction(user_id) as cursor:
        track = owned_track(cursor, track_id, user_id, lock=True)
        if body.baseVersion != current_version(cursor, track_id):
            raise HTTPException(409, 'Track đã thay đổi. Tải lại phiên bản mới trước khi lưu.')
        rows = segments(cursor, track_id)
        # Move positions out of the way before assigning consecutive sequence numbers.
        offset = max((row['sequence_number'] for row in rows), default=0) + 100
        cursor.execute('UPDATE SUBTITLE_SEGMENT SET sequence_number=sequence_number+%s WHERE track_id=%s '
                       'ORDER BY sequence_number DESC', (offset, track_id))
        sequence = 2 * offset
        freeze_text = track['source_track_id'] and translation_lock_available(cursor)
        for operation in body.operations:
            position = next((i for i, row in enumerate(rows) if row['segment_id'] == operation.segment_id), None)
            if position is None:
                raise HTTPException(422, 'Đoạn phụ đề không thuộc track.')
            row = rows[position]
            identifier = row['segment_id']
            aligned_merge = False
            if operation.operation == 'delete':
                remove_segment(cursor, identifier)
                rows.pop(position)
                continue
            if operation.operation == 'update':
                if freeze_text and row['content'] != operation.content:
                    cursor.execute('UPDATE SUBTITLE_SEGMENT SET is_locked=TRUE WHERE segment_id=%s', (identifier,))
                row.update(content=operation.content, start_ms=operation.start_ms, end_ms=operation.end_ms)
            elif operation.operation == 'split':
                if not row['start_ms'] < operation.at_ms < row['end_ms']:
                    raise HTTPException(422, 'Mốc tách phải nằm trong đoạn.')
                sequence += 1
                cursor.execute('''INSERT INTO SUBTITLE_SEGMENT (track_id,sequence_number,start_ms,end_ms,content,speaker_id)
                                  VALUES (%s,%s,%s,%s,%s,%s)''',
                               (track_id, sequence, operation.at_ms, row['end_ms'], operation.right, row['speaker_id']))
                new_id = cursor.lastrowid
                if freeze_text:
                    copy_links(cursor, identifier, new_id)
                    cursor.execute('UPDATE SUBTITLE_SEGMENT SET is_locked=TRUE WHERE segment_id IN (%s,%s)', (identifier, new_id))
                cursor.execute('''SELECT word_id,start_ms,end_ms,word_text FROM WORD_ALIGNMENT
                                  WHERE segment_id=%s AND is_stale=FALSE ORDER BY word_index''', (identifier,))
                aligned = cursor.fetchall()
                left = [word for word in aligned if word['end_ms'] <= operation.at_ms]
                right = [word for word in aligned if word['start_ms'] >= operation.at_ms]
                if (left and right and len(left) + len(right) == len(aligned)
                        and normalize(' '.join(word['word_text'] for word in left)) == normalize(operation.left)
                        and normalize(' '.join(word['word_text'] for word in right)) == normalize(operation.right)):
                    cursor.executemany('UPDATE WORD_ALIGNMENT SET segment_id=%s,word_index=%s WHERE word_id=%s',
                                       [(new_id, index, word['word_id']) for index, word in enumerate(right, 1)])
                else:
                    invalidate(cursor, identifier)
                rows.insert(position + 1, {'segment_id': new_id, 'start_ms': operation.at_ms, 'end_ms': row['end_ms'],
                                          'content': operation.right, 'speaker_id': row['speaker_id']})
                row.update(end_ms=operation.at_ms, content=operation.left)
            else:
                if position + 1 >= len(rows) or rows[position + 1]['segment_id'] != operation.next_segment_id:
                    raise HTTPException(422, 'Chỉ có thể gộp hai đoạn liền nhau.')
                after = rows.pop(position + 1)
                aligned_merge = merge_word_alignment(cursor, row, after)
                if freeze_text:
                    cursor.execute('UPDATE SUBTITLE_SEGMENT SET is_locked=TRUE WHERE segment_id=%s', (identifier,))
                row.update(end_ms=max(row['end_ms'], after['end_ms']), content=f"{row['content']} {after['content']}")
                copy_links(cursor, after['segment_id'], identifier)
                remove_segment(cursor, after['segment_id'])
            if operation.operation != 'split' and not aligned_merge:
                invalidate(cursor, identifier)
        duration = track['duration_ms']
        for sequence, row in enumerate(rows, 1):
            if row['start_ms'] >= row['end_ms'] or (duration is not None and row['end_ms'] > duration):
                raise HTTPException(422, 'Mốc thời gian đoạn phụ đề không hợp lệ.')
            if not row['content'].strip() or len(row['content']) > 16000:
                raise HTTPException(422, 'Đoạn phụ đề rỗng hoặc quá dài.')
            cursor.execute('''UPDATE SUBTITLE_SEGMENT SET sequence_number=%s,start_ms=%s,end_ms=%s,content=%s
                              WHERE segment_id=%s''',
                           (sequence, row['start_ms'], row['end_ms'], row['content'], row['segment_id']))
        version = save_revision(cursor, track_id, editor_id=user_id)
        record_quality_issues(cursor, track_id)
        if any(operation.operation != 'update' for operation in body.operations):
            cursor.execute('SELECT track_id FROM SUBTITLE_TRACK WHERE source_track_id=%s FOR UPDATE', (track_id,))
            for dependent in cursor.fetchall():
                save_revision(cursor, dependent['track_id'], editor_id=user_id)
        return {'version': version, 'segments': segments(cursor, track_id)}


@router.get('/tracks/{track_id}/quality-check')
def saved_quality(track_id: int, user_id: Owner):
    with transaction(user_id) as cursor:
        owned_track(cursor, track_id, user_id)
        cursor.execute('''SELECT CAST(i.segment_id AS CHAR) AS segment_id,i.issue_type,
                                 i.measured_value,i.threshold_value
                          FROM SUBTITLE_ISSUE i JOIN SUBTITLE_SEGMENT s ON s.segment_id=i.segment_id
                          WHERE s.track_id=%s AND i.status='OPEN' ORDER BY i.issue_id''', (track_id,))
        return {'issues': cursor.fetchall()}


@router.post('/tracks/{track_id}/quality-check')
def quality_check(track_id: int, user_id: Owner):
    with transaction(user_id) as cursor:
        owned_track(cursor, track_id, user_id, lock=True)
        return {'issues': record_quality_issues(cursor, track_id)}


def record_quality_issues(cursor, track_id, job_id=None):
    rows = segments(cursor, track_id)
    cursor.execute('SELECT language FROM SUBTITLE_TRACK WHERE track_id=%s', (track_id,))
    costs = caption_costs(cursor.fetchone()['language'])
    cursor.execute('''UPDATE SUBTITLE_ISSUE i JOIN SUBTITLE_SEGMENT s ON s.segment_id=i.segment_id
                      SET i.status='STALE' WHERE s.track_id=%s AND i.status='OPEN' ''', (track_id,))
    issues = []
    previous = None
    last_by_text = {}
    for row in sorted(rows, key=lambda item: item['start_ms']):
        cue = TimedText(row['start_ms'], row['end_ms'], row['content'])
        duration = cue.end_ms - cue.start_ms
        checks = [
            ('CPS', len(cue.text.replace('\n', '')) * 1000 / duration, costs.max_cps, None),
            ('CPL', max(map(len, cue.text.splitlines()), default=0), costs.max_cpl, None),
            ('DURATION_SHORT', costs.min_duration_ms - duration, 0, None),
            ('DURATION_LONG', duration, costs.max_duration_ms, None),
            ('EMPTY', int(not cue.text.strip()), 0, None),
        ]
        if row['confidence'] is not None and row['confidence'] < 0.55:
            checks.append(('LOW_CONFIDENCE', row['confidence'], 0.55, None))
        words = cue.text.casefold().split()
        repeated = False
        for size in range(2, len(words) // 2 + 1):
            if len(words) % size == 0 and words == words[:size] * (len(words) // size):
                repeated = True
                break
        if repeated:
            checks.append(('HALLUCINATION_REPEAT', len(words), 2, None))
        if previous:
            other, other_id = previous
            checks.append(('OVERLAP', min(cue.end_ms, other.end_ms) - cue.start_ms, 0, other_id))
        key = normalize(cue.text)
        if len(key) >= 8 and key in last_by_text:
            other, other_id = last_by_text[key]
            if cue.start_ms - other.end_ms <= 1000:
                checks.append(('DUPLICATE', 1.0, 0.5, other_id))
        if key:
            last_by_text[key] = (cue, row['segment_id'])
        for kind, value, threshold, related in checks:
            if kind != 'LOW_CONFIDENCE' and value <= threshold:
                continue
            cursor.execute('''INSERT INTO SUBTITLE_ISSUE
                (segment_id,related_segment_id,job_id,issue_type,severity,measured_value,threshold_value)
                VALUES (%s,%s,%s,%s,'WARNING',%s,%s)''',
                (row['segment_id'], related, job_id, kind, value, threshold))
            issues.append({'segment_id': str(row['segment_id']), 'issue_type': kind,
                           'measured_value': value, 'threshold_value': threshold})
        if previous is None or cue.end_ms >= previous[0].end_ms:
            previous = (cue, row['segment_id'])
    return issues


class ExportRequest(BaseModel):
    format: Literal['srt', 'vtt']
    version: int | None = Field(default=None, ge=1)
    source_version: int | None = Field(default=None, ge=1)


def read_revision(cursor, track_id, version):
    cursor.execute('''SELECT revision_id,snapshot FROM SUBTITLE_REVISION
                      WHERE track_id=%s AND (%s IS NULL OR version_no=%s)
                      ORDER BY version_no DESC LIMIT 1''', (track_id, version, version))
    row = cursor.fetchone()
    if row is None:
        raise HTTPException(404, 'Không tìm thấy phiên bản phụ đề.')
    return row['revision_id'], json.loads(row['snapshot'])


@router.post('/tracks/{track_id}/exports', status_code=201)
def create_export(track_id: int, body: ExportRequest, user_id: Owner):
    path = None
    try:
        with transaction(user_id) as cursor:
            track = owned_track(cursor, track_id, user_id)
            revision, rows = read_revision(cursor, track_id, body.version)
            source_revision = None
            source = []
            if body.source_version is not None:
                if not track['source_track_id']:
                    raise HTTPException(422, 'Chọn track dịch để xuất song ngữ.')
                owned_track(cursor, track['source_track_id'], user_id)
                source_revision, source = read_revision(cursor, track['source_track_id'], body.source_version)
            source_by_time = {(row['start_ms'], row['end_ms']): row['content'] for row in source}
            source_by_id = {row['segment_id']: row['content'] for row in source}
            separator = ',' if body.format == 'srt' else '.'
            output = ['WEBVTT', ''] if body.format == 'vtt' else []
            for index, row in enumerate(rows, 1):
                begin, end = row['start_ms'], row['end_ms']
                text = ' '.join(source_by_id[identifier] for identifier in row.get('source_segment_ids', [])
                                if identifier in source_by_id) or source_by_time.get((begin, end))
                if body.format == 'srt':
                    output.append(str(index))
                output.extend([f'{subtitle_time(begin / 1000, separator)} --> {subtitle_time(end / 1000, separator)}',
                               f"{text}\n{row['content']}" if text else row['content'], ''])
            EXPORTS.mkdir(parents=True, exist_ok=True)
            path = EXPORTS / f'{uuid4()}.{body.format}'
            path.write_text('\n'.join(output) + '\n', encoding='utf-8')
            cursor.execute('INSERT INTO EXPORT_FILE (revision_id,source_revision_id,format,file_path) VALUES (%s,%s,%s,%s)',
                           (revision, source_revision, body.format.upper(), str(path)))
            export_id = cursor.lastrowid
        return {'export_id': str(export_id), 'download_url': f'/api/v1/exports/{export_id}/download'}
    except Exception:
        if path:
            path.unlink(missing_ok=True)
        raise


@router.get('/exports/{export_id}/download')
def download_export(export_id: int, user_id: Owner):
    with transaction(user_id) as cursor:
        cursor.execute('''SELECT e.file_path,e.format FROM EXPORT_FILE e
                          JOIN SUBTITLE_REVISION r ON r.revision_id=e.revision_id
                          JOIN SUBTITLE_TRACK t ON t.track_id=r.track_id JOIN VIDEO v ON v.video_id=t.video_id
                          JOIN PROJECT p ON p.project_id=v.project_id WHERE e.export_id=%s AND p.user_id=%s''',
                       (export_id, user_id))
        row = cursor.fetchone()
    path = Path(row['file_path']).resolve() if row else None
    if path is None or path.parent != EXPORTS.resolve() or not path.is_file():
        raise HTTPException(404, 'Không tìm thấy bản xuất.')
    return FileResponse(path, filename=f"subtitles.{row['format'].lower()}")
