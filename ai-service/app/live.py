import asyncio
import json
import logging
import os
import wave
from contextlib import aclosing
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, model_validator
from starlette.concurrency import run_in_threadpool

from .accounts import SESSION_COOKIE, session_user
from .context_translation import contextual_model, contextual_translator, translate_with_context
from .media import transcribe
from .mysql_api import Owner, insert_project, local_lock, lock_project, save_revision, transaction
from .subtitle_algorithms import TimedText, deduplicate
from .translate import translate_batch


router = APIRouter(prefix='/api/v1/live', tags=['live'])
logger = logging.getLogger(__name__)
SAMPLE_RATE = 16000
BYTES_PER_MS = SAMPLE_RATE * 2 // 1000
MAX_DURATION_MS = 60 * 60 * 1000
streams: set[int] = set()


class LiveConfig(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    project_id: int | None = Field(default=None, gt=0)
    source_language: Literal['vi', 'en']
    source_type: Literal['MICROPHONE', 'STREAM_URL'] = 'MICROPHONE'
    stream_url: str | None = Field(default=None, max_length=2083)
    target_language: Literal['vi', 'en'] | None = None
    translation_mode: Literal['local', 'context'] = 'local'
    chunk_size_ms: int = Field(default=6000, ge=2000, le=10000)
    overlap_ms: int = Field(default=1000, ge=0, le=2000)

    @model_validator(mode='after')
    def check_configuration(self):
        self.title = self.title.strip()
        if not self.title or self.overlap_ms >= self.chunk_size_ms or self.target_language == self.source_language:
            raise ValueError('Cấu hình phiên Live không hợp lệ.')
        if (self.source_type == 'STREAM_URL') != bool(self.stream_url):
            raise ValueError('Nhập đúng URL cho nguồn Live đã chọn.')
        if self.translation_mode == 'context' and not self.target_language:
            raise ValueError('Chọn ngôn ngữ đích để bật dịch theo ngữ cảnh.')
        return self


def allowed_stream(url):
    try:
        allowed = json.loads(os.getenv('SUBTITLE_LIVE_STREAM_URLS', '[]'))
        location = urlsplit(url)
    except ValueError:
        return False
    return (isinstance(allowed, list) and url in allowed and location.scheme == 'https'
            and bool(location.hostname) and location.username is None and location.password is None)


@router.post('/sessions', status_code=201)
def create_session(body: LiveConfig, user_id: Owner):
    if body.stream_url and not allowed_stream(body.stream_url):
        raise HTTPException(422, 'URL Live phải là luồng HTTPS đã cấu hình trên máy chủ.')
    if body.translation_mode == 'context' and contextual_model() is None:
        raise HTTPException(503, 'Máy chủ chưa cấu hình mô hình dịch theo ngữ cảnh.')
    with transaction(user_id) as cursor:
        project = body.project_id
        if project is None:
            project = insert_project(cursor, body.title, user_id)
        else:
            lock_project(cursor, project, user_id)
        cursor.execute("""INSERT INTO VIDEO (project_id,title,source_type,source_url,detected_language,status)
                          VALUES (%s,%s,'LIVE',%s,%s,'RUNNING')""", (project, body.title, body.stream_url, body.source_language))
        video = cursor.lastrowid
        cursor.execute("""INSERT INTO LIVE_SESSION
                          (video_id,source_type,source_language,target_language,chunk_size_ms,overlap_ms,started_at)
                          VALUES (%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP(3))""",
                       (video, body.source_type, body.source_language, body.target_language, body.chunk_size_ms, body.overlap_ms))
        session = cursor.lastrowid
        cursor.execute("""INSERT INTO PROCESSING_JOB (video_id,job_type,status,processing_parameters,started_at)
                          VALUES (%s,'LIVE','RUNNING',%s,CURRENT_TIMESTAMP(3))""",
                       (video, json.dumps({'translation_mode': body.translation_mode})))
        job = cursor.lastrowid
        cursor.execute("""INSERT INTO PROCESSING_STEP (job_id,sequence_number,step_type,execution_environment,status,started_at)
                          VALUES (%s,1,'LIVE_ASR','LOCAL','RUNNING',CURRENT_TIMESTAMP(3))""", (job,))
        cursor.execute("""INSERT INTO SUBTITLE_TRACK (video_id,created_by_job_id,language,track_type)
                          VALUES (%s,%s,%s,'ORIGINAL')""", (video, job, body.source_language))
        track = cursor.lastrowid
        if body.target_language:
            cursor.execute("""INSERT INTO SUBTITLE_TRACK (video_id,source_track_id,created_by_job_id,language,track_type)
                              VALUES (%s,%s,%s,%s,'TRANSLATION')""", (video, track, job, body.target_language))
            cursor.execute("""INSERT INTO PROCESSING_STEP
                              (job_id,sequence_number,step_type,execution_environment,status,started_at)
                              VALUES (%s,2,'LIVE_TRANSLATE',%s,'RUNNING',CURRENT_TIMESTAMP(3))""",
                           (job, 'GPU_SERVER' if body.translation_mode == 'context' else 'LOCAL'))
    return {'session_id': str(session), 'project_id': str(project), 'track_id': str(track),
            'websocket_path': f'/api/v1/live/sessions/{session}/ws'}


def owned_session(cursor, session, user_id, *, lock=False):
    cursor.execute("""SELECT l.*,v.project_id,v.source_url,j.job_id,j.processing_parameters,o.track_id,
                             t.track_id AS translation_id
                      FROM LIVE_SESSION l JOIN VIDEO v ON v.video_id=l.video_id
                      JOIN PROJECT p ON p.project_id=v.project_id
                      JOIN PROCESSING_JOB j ON j.video_id=v.video_id AND j.job_type='LIVE'
                      JOIN SUBTITLE_TRACK o ON o.created_by_job_id=j.job_id AND o.track_type='ORIGINAL'
                      LEFT JOIN SUBTITLE_TRACK t ON t.created_by_job_id=j.job_id AND t.source_track_id=o.track_id
                      WHERE l.session_id=%s AND p.user_id=%s""" + (' FOR UPDATE' if lock else ''), (session, user_id))
    result = cursor.fetchone()
    if result is None:
        raise HTTPException(404, 'Không tìm thấy phiên Live.')
    return result


def load_session(session, user_id):
    with transaction(user_id) as cursor:
        return owned_session(cursor, session, user_id)


def finish_session(session, user_id, status, detail=None):
    with transaction() as cursor:
        row = owned_session(cursor, session, user_id, lock=True)
        if row['status'] == 'RUNNING':
            cursor.execute("""SELECT MAX(end_ms) AS duration_ms FROM LIVE_CHUNK
                              WHERE session_id=%s AND status='COMPLETED'""", (session,))
            duration = cursor.fetchone()['duration_ms'] or 0
            cursor.execute('UPDATE LIVE_SESSION SET status=%s,ended_at=CURRENT_TIMESTAMP(3) WHERE session_id=%s',
                           (status, session))
            cursor.execute('UPDATE VIDEO SET status=%s,duration_ms=%s WHERE video_id=%s',
                           (status, duration, row['video_id']))
            cursor.execute("""UPDATE PROCESSING_JOB SET status=%s,error_message=%s,progress=%s,
                              completed_at=CURRENT_TIMESTAMP(3) WHERE job_id=%s""",
                           (status, detail, 100 if status == 'COMPLETED' else 0, row['job_id']))
            cursor.execute("""UPDATE PROCESSING_STEP SET status=%s,error_message=%s,
                              completed_at=CURRENT_TIMESTAMP(3) WHERE job_id=%s""",
                           (status, detail, row['job_id']))
            cursor.execute("UPDATE LIVE_CHUNK SET status='FAILED' WHERE session_id=%s AND status='RUNNING'", (session,))
            save_revision(cursor, row['track_id'], job_id=row['job_id'])
            if row['translation_id']:
                save_revision(cursor, row['translation_id'], job_id=row['job_id'])
        else:
            status = row['status']
    return {'type': 'finished', 'status': status, 'project_id': str(row['project_id'])}


def recover_sessions():
    with transaction() as cursor:
        cursor.execute('''SELECT l.session_id,p.user_id FROM LIVE_SESSION l
                          JOIN VIDEO v ON v.video_id=l.video_id JOIN PROJECT p ON p.project_id=v.project_id
                          JOIN PROCESSING_JOB j ON j.video_id=v.video_id AND j.job_type='LIVE'
                          WHERE l.status='RUNNING' ''')
        interrupted = cursor.fetchall()
    for session in interrupted:
        finish_session(session['session_id'], session['user_id'], 'CANCELLED', 'Máy chủ đã khởi động lại.')


@router.post('/sessions/{session}/stop')
async def stop_session(session: int, user_id: Owner):
    await run_in_threadpool(load_session, session, user_id)
    if session in streams:
        raise HTTPException(409, 'Hãy kết thúc phiên trên kết nối đang ghi để lưu phần âm thanh còn lại.')
    streams.add(session)
    try:
        return await run_in_threadpool(finish_session, session, user_id, 'COMPLETED')
    finally:
        streams.discard(session)


def open_chunk(config, number, start, end, user_id):
    with transaction(user_id) as cursor:
        row = owned_session(cursor, config['session_id'], user_id, lock=True)
        if row['status'] != 'RUNNING':
            raise HTTPException(409, 'Phiên Live đã kết thúc.')
        cursor.execute("""INSERT INTO LIVE_CHUNK
                          (session_id,processing_job_id,sequence_number,start_ms,end_ms,status)
                          VALUES (%s,%s,%s,%s,%s,'RUNNING')""",
                       (config['session_id'], config['job_id'], number, start, end))
        return cursor.lastrowid


def speech_from_pcm(audio, language):
    import numpy as np

    samples = np.frombuffer(audio, dtype='<i2').astype('float32')
    if not samples.size or np.sqrt(np.mean(samples * samples)) < 100:
        return []
    with TemporaryDirectory() as directory:
        file = Path(directory) / 'chunk.wav'
        with wave.open(str(file), 'wb') as writer:
            writer.setparams((1, 2, SAMPLE_RATE, 0, 'NONE', 'not compressed'))
            writer.writeframes(audio)
        with local_lock:
            cues, _ = transcribe(file, language=language)
    return cues


def select_cues(cues, previous, start, end, cutoff, number):
    candidates = [
        TimedText(start + round(a * 1000), min(end, start + round(b * 1000)), text, str(number))
        for a, b, text in cues if start + round(a * 1000) < cutoff
    ]
    unique = deduplicate([*previous, *candidates], overlap=0.3)
    boundary = previous[-1].end_ms if previous else 0
    result = []
    for cue in unique:
        if cue.chunk != str(number):
            continue
        offset = max(cue.start_ms, boundary)
        if cue.end_ms > offset:
            result.append(TimedText(offset, cue.end_ms, cue.text, cue.chunk))
            boundary = cue.end_ms
    return result


def store_chunk(config, chunk, cues, translations, sequence, asr_ms, translation_ms, user_id):
    rows = []
    with transaction(user_id) as cursor:
        for sequence, (cue, translation) in enumerate(zip(cues, translations, strict=True), sequence + 1):
            cursor.execute("""INSERT INTO SUBTITLE_SEGMENT
                              (track_id,created_by_job_id,live_chunk_id,sequence_number,start_ms,end_ms,content)
                              VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                           (config['track_id'], config['job_id'], chunk, sequence, cue.start_ms, cue.end_ms, cue.text))
            segment = cursor.lastrowid
            if translation:
                cursor.execute("""INSERT INTO SUBTITLE_SEGMENT
                                  (track_id,created_by_job_id,live_chunk_id,sequence_number,start_ms,end_ms,content)
                                  VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                               (config['translation_id'], config['job_id'], chunk, sequence,
                                cue.start_ms, cue.end_ms, translation))
                cursor.execute('INSERT INTO SEGMENT_LINK (source_segment_id,target_segment_id) VALUES (%s,%s)',
                               (segment, cursor.lastrowid))
            rows.append({'id': str(segment), 'start_ms': cue.start_ms, 'end_ms': cue.end_ms,
                         'text': cue.text, 'translation': translation})
        cursor.execute("""UPDATE LIVE_CHUNK SET status='COMPLETED',recognized_text=%s,asr_latency_ms=%s,
                          translation_latency_ms=%s,latency_ms=%s WHERE chunk_id=%s""",
                       ('\n'.join(cue.text for cue in cues), asr_ms, translation_ms, asr_ms + (translation_ms or 0), chunk))
    return rows


async def microphone_frames(websocket):
    while True:
        message = await asyncio.wait_for(websocket.receive(), timeout=30)
        if message['type'] == 'websocket.disconnect':
            raise WebSocketDisconnect()
        if message.get('text') == 'stop':
            break
        yield message.get('bytes') or b''
        await websocket.send_json({'type': 'ack'})


def stream_pcm(url):
    import av

    if not allowed_stream(url):
        raise ValueError('Stream is outside the configured sources.')
    resampler = av.AudioResampler(format='s16', layout='mono', rate=SAMPLE_RATE)
    with av.open(url, timeout=(8, 5), options={'protocol_whitelist': 'https,tls,tcp,crypto', 'tls_verify': '1'}) as media:
        if not media.streams.audio:
            raise ValueError('Stream has no audio.')
        buffer = bytearray()
        for decoded in media.decode(audio=0):
            for frame in resampler.resample(decoded):
                buffer.extend(frame.to_ndarray().astype('<i2', copy=False).tobytes())
            while len(buffer) >= 8192:
                yield bytes(buffer[:8192])
                del buffer[:8192]
        aligned = len(buffer) - len(buffer) % BYTES_PER_MS
        if aligned:
            yield bytes(buffer[:aligned])


async def stream_frames(websocket, url):
    async def wait_for_stop():
        message = await websocket.receive()
        if message['type'] == 'websocket.disconnect':
            raise WebSocketDisconnect()
        if message.get('text') != 'stop':
            raise ValueError('Invalid stream command')

    stop = asyncio.create_task(wait_for_stop())
    reader = stream_pcm(url)
    try:
        while not stop.done():
            frame = await run_in_threadpool(next, reader, None)
            if frame is None:
                break
            yield frame
        if stop.done():
            stop.result()
    finally:
        stop.cancel()
        await asyncio.gather(stop, return_exceptions=True)
        await run_in_threadpool(reader.close)


async def serve_audio(websocket, config, user_id):
    input_frames = (stream_frames(websocket, config['source_url']) if config['source_type'] == 'STREAM_URL'
                    else microphone_frames(websocket))
    async with aclosing(input_frames) as frames:
        await process_frames(websocket, config, user_id, frames)


async def process_frames(websocket, config, user_id, frames):
    size = config['chunk_size_ms'] * BYTES_PER_MS
    overlap = config['overlap_ms'] * BYTES_PER_MS
    stride = size - overlap
    buffer = bytearray()
    previous = []
    prior_text = []
    use_context = json.loads(config.get('processing_parameters') or '{}').get('translation_mode') == 'context'
    glossary = await run_in_threadpool(live_glossary, config, user_id) if use_context else {}
    start = number = sequence = received = 0
    await websocket.send_json({'type': 'ready'})
    while True:
        frame = await anext(frames, None)
        finished = frame is None
        if not finished:
            if not frame or len(frame) % BYTES_PER_MS or len(frame) > size:
                raise ValueError('Invalid PCM frame')
            frame = frame[:MAX_DURATION_MS * BYTES_PER_MS - received]
            buffer.extend(frame)
            received += len(frame)
            finished = received >= MAX_DURATION_MS * BYTES_PER_MS
        while len(buffer) >= size or (finished and buffer):
            audio = bytes(buffer[:size])
            number += 1
            end = start + len(audio) // BYTES_PER_MS
            cutoff = end if finished and len(buffer) <= size else start + stride // BYTES_PER_MS
            chunk = await run_in_threadpool(open_chunk, config, number, start, end, user_id)
            began = monotonic()
            recognition = await run_in_threadpool(speech_from_pcm, audio, config['source_language'])
            asr_ms = round((monotonic() - began) * 1000)
            cues = select_cues(recognition, previous, start, end, cutoff, number)
            await websocket.send_json({'type': 'partial', 'text': ' '.join(text for _, _, text in recognition)})
            translations = [''] * len(cues)
            translation_ms = None
            warning = None
            if cues and config['target_language']:
                began = monotonic()
                try:
                    if use_context:
                        current = [{'id': str(sequence + index + 1), 'text': cue.text} for index, cue in enumerate(cues)]
                        translations = await run_in_threadpool(translate_live_context, current, prior_text, config, glossary)
                    else:
                        translations = await run_in_threadpool(translate_batch, [cue.text for cue in cues],
                                                               config['source_language'], config['target_language'])
                except Exception:
                    logger.exception('Live translation failed for session %s', config['session_id'])
                    warning = 'Chưa dịch được đoạn này. Phụ đề gốc đã được lưu.'
                if use_context:
                    prior_text = (prior_text + current)[-2:]
                translation_ms = round((monotonic() - began) * 1000)
            rows = await run_in_threadpool(store_chunk, config, chunk, cues, translations, sequence,
                                           asr_ms, translation_ms, user_id)
            sequence += len(rows)
            if cues:
                previous = cues[-10:]
            await websocket.send_json({'type': 'final', 'segments': rows, 'latency_ms': asr_ms + (translation_ms or 0),
                                       'warning': warning})
            if finished and len(buffer) <= size:
                buffer.clear()
            else:
                del buffer[:stride]
                start += stride // BYTES_PER_MS
        if finished:
            return


def live_glossary(config, user_id):
    with transaction(user_id) as cursor:
        cursor.execute('''SELECT source_term,translated_term FROM GLOSSARY
                          WHERE user_id=%s AND source_lang=%s AND target_lang=%s''',
                       (user_id, config['source_language'], config['target_language']))
        return {row['source_term']: row['translated_term'] for row in cursor.fetchall()}


def translate_live_context(rows, previous, config, glossary):
    model = contextual_model()
    if model is None:
        raise ValueError('Contextual translation model unavailable')
    return translate_with_context(rows, config['target_language'], glossary, contextual_translator(*model),
                                  previous_rows=previous)


@router.websocket('/sessions/{session}/ws')
async def live_socket(websocket: WebSocket, session: int):
    if websocket.headers.get('origin') not in ('http://localhost:5173', 'http://127.0.0.1:5173', 'http://localhost:5174', 'http://127.0.0.1:5174'):
        await websocket.close(code=1008)
        return
    try:
        user = await run_in_threadpool(session_user, websocket.cookies.get(SESSION_COOKIE))
        user_id = int(user['user_id'])
        config = await run_in_threadpool(load_session, session, user_id)
    except HTTPException:
        await websocket.close(code=1008)
        return
    if config['status'] != 'RUNNING' or session in streams:
        await websocket.close(code=1008)
        return
    streams.add(session)
    try:
        await websocket.accept()
        await serve_audio(websocket, config, user_id)
        result = await run_in_threadpool(finish_session, session, user_id, 'COMPLETED')
        await websocket.send_json(result)
        await websocket.close()
    except WebSocketDisconnect:
        await run_in_threadpool(finish_session, session, user_id, 'CANCELLED', 'Kết nối Live đã ngắt.')
    except Exception:
        logger.exception('Live session %s failed', session)
        detail = 'Phiên Live gặp lỗi. Các đoạn đã chốt được lưu trong dự án.'
        await run_in_threadpool(finish_session, session, user_id, 'FAILED', detail)
        try:
            await websocket.send_json({'type': 'error', 'message': detail})
            await websocket.close(code=1011)
        except (WebSocketDisconnect, RuntimeError):
            pass
    finally:
        streams.discard(session)
