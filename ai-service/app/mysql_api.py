import json
import logging
import os
import subprocess
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from secrets import compare_digest, token_urlsafe
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread
from time import time
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from mysql.connector.errors import IntegrityError
from pydantic import BaseModel, Field, HttpUrl, model_validator
from starlette.concurrency import run_in_threadpool

from .accounts import current_user_id
from .audio_results import SpeechInterval, save_speech_intervals, speaker_for_range
from .context_translation import contextual_model
from .dialect_runtime import classify_local_dialect, local_dialect_runtime
from .dialects import region_for_range, vietnamese
from .media import (VIETNAMESE_ASR_NAME, denoise_audio, download_video, extract_raw_audio, local_whisper_model,
                    prepare_audio, probe_media, transcribe_aligned)
from .media_preview import browser_preview
from .social_video import INVALID_VIDEO_URL, SUPPORTED_PLATFORMS, VideoDownloadError, parse_video_url
from .mysql_db import mysql_transaction
from .project_cleanup import delete_project_content
from .resource_metrics import ResourceMetrics, monitor_local, save_sample
from .subtitle_algorithms import TimedText, optimize_transcript
from .translate import PAIRS, translate_batch
from .video_export import VideoExportRequest, create_video_export, video_download


@asynccontextmanager
async def lifespan(_):
    from .live import recover_sessions

    try:
        with mysql_transaction() as cursor:
            cursor.execute("SHOW COLUMNS FROM SUBTITLE_SEGMENT LIKE 'is_locked'")
            if cursor.fetchone() is None:
                cursor.execute("ALTER TABLE SUBTITLE_SEGMENT ADD COLUMN is_locked BOOLEAN NOT NULL DEFAULT FALSE")
    except HTTPException:
        logger.warning('Could not ensure SUBTITLE_SEGMENT.is_locked during startup.')

    bootstrap_admin = os.getenv('SUBTITLE_BOOTSTRAP_ADMIN_USERNAME', '').strip()
    if bootstrap_admin:
        try:
            with mysql_transaction() as cursor:
                cursor.execute("UPDATE USER SET role='ADMIN' WHERE username=%s", (bootstrap_admin,))
        except HTTPException:
            logger.warning('Bootstrap admin unavailable while MySQL is starting.')

    try:
        await run_in_threadpool(recover_sessions)
    except HTTPException:
        logger.info('Live recovery will be available when MySQL is connected.')
    stop = Event()
    worker = Thread(target=resume_local_jobs, args=(stop,), daemon=True)
    monitor = Thread(target=monitor_local, args=(stop,), daemon=True)
    worker.start()
    monitor.start()
    try:
        yield
    finally:
        stop.set()
        worker.join(timeout=2)
        monitor.join(timeout=2)


app = FastAPI(title='SubLearn AI · MySQL', lifespan=lifespan)
worker_app = FastAPI(title='SubLearn AI · Colab Worker API', docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=['http://localhost:5173', 'http://127.0.0.1:5173', 'http://localhost:5174', 'http://127.0.0.1:5174'],
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)
UPLOADS = Path(__file__).resolve().parent.parent / 'data' / 'uploads'
WORKER_TOKEN = os.getenv('SUBTITLE_WORKER_TOKEN', '')
REMOTE_GPU = os.getenv('SUBTITLE_REMOTE_GPU', '').lower() == 'true' and bool(WORKER_TOKEN)
WORKER_ACTIVITY = UPLOADS.parent / '.worker-active'
local_lock = Lock()
logger = logging.getLogger(__name__)
MAX_UPLOAD = 500 * 1024 * 1024
Language = Annotated[str, Field(pattern=r'^[a-zA-Z]{2,8}(?:-[a-zA-Z0-9]{1,8})?$', max_length=20)]


def gpu_worker_available():
    try:
        return REMOTE_GPU and time() - WORKER_ACTIVITY.stat().st_mtime < 30
    except OSError:
        return False


def note_worker_activity():
    try:
        WORKER_ACTIVITY.touch()
    except OSError:
        logger.warning('Could not record remote worker activity')


@contextmanager
def transaction(user_id=None):
    try:
        with mysql_transaction() as cursor:
            if user_id is not None:
                check_owner(cursor, user_id)
            yield cursor
    except IntegrityError as error:
        raise HTTPException(409, 'Thao tác xung đột với dữ liệu liên quan trong MySQL.') from error


def check_owner(cursor, user_id):
    cursor.execute("SELECT user_id FROM `USER` WHERE user_id=%s AND status='ACTIVE' FOR SHARE", (user_id,))
    if cursor.fetchone() is None:
        raise HTTPException(401, 'Phiên đăng nhập đã hết hạn.')


PROJECTS = '''
    SELECT CAST(p.project_id AS CHAR) AS id, p.project_name AS name,
        p.created_at,p.description,CAST(v.video_id AS CHAR) AS video_id,v.title AS video_title,v.duration_ms,
        v.source_url, v.file_path IS NOT NULL AS media_ready,
        v.detected_language AS source_language, COALESCE(v.source_type, 'MANUAL') AS source_type,
        (SELECT t.language FROM SUBTITLE_TRACK t WHERE t.source_track_id=(
            SELECT MAX(track_id) FROM SUBTITLE_TRACK WHERE video_id=v.video_id AND track_type='ORIGINAL'
        ) ORDER BY t.track_id DESC LIMIT 1) AS target_language,
        LOWER(COALESCE(j.status, NULLIF(v.status, 'PENDING'), 'READY')) AS status,
        CAST(j.job_id AS CHAR) AS job_id,j.error_message,COALESCE(j.progress,0) AS progress,
        (SELECT execution_environment FROM PROCESSING_STEP
            WHERE job_id=j.job_id AND step_type IN ('TRANSCRIBE','LIVE_ASR') LIMIT 1) AS execution_environment,
        CAST((SELECT MAX(track_id) FROM SUBTITLE_TRACK WHERE video_id=v.video_id AND track_type='ORIGINAL') AS CHAR) AS track_id,
        CAST((SELECT MAX(track_id) FROM SUBTITLE_TRACK WHERE source_track_id=(
            SELECT MAX(track_id) FROM SUBTITLE_TRACK WHERE video_id=v.video_id AND track_type='ORIGINAL'
        )) AS CHAR) AS target_track_id,
        (SELECT COUNT(*) FROM SUBTITLE_SEGMENT s WHERE s.track_id=(
            SELECT MAX(track_id) FROM SUBTITLE_TRACK WHERE video_id=v.video_id AND track_type='ORIGINAL'
        )) AS segment_count
    FROM PROJECT p LEFT JOIN VIDEO v ON v.project_id=p.project_id AND v.video_id=COALESCE(%s,(
        SELECT MAX(video_id) FROM VIDEO WHERE project_id=p.project_id
    ))
    LEFT JOIN PROCESSING_JOB j ON j.job_id=(
        SELECT MAX(job_id) FROM PROCESSING_JOB WHERE video_id=v.video_id AND job_type IN ('TRANSCRIBE','LIVE')
    )
    WHERE p.user_id=%s
'''


def project_row(cursor, project_id, user_id, video_id=None):
    clause = ' AND v.video_id IS NOT NULL' if video_id is not None else ''
    cursor.execute(PROJECTS + ' AND p.project_id=%s' + clause, (video_id, user_id, project_id))
    return cursor.fetchone()


def lock_project(cursor, project_id, user_id):
    cursor.execute('SELECT project_id FROM PROJECT WHERE project_id=%s AND user_id=%s FOR UPDATE',
                   (project_id, user_id))
    if cursor.fetchone() is None:
        raise HTTPException(404, 'Không tìm thấy dự án.')


Owner = Annotated[int, Depends(current_user_id)]


class ProjectInput(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    source_url: HttpUrl
    skip_asr: bool = False


class VideoUrlInput(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    source_url: HttpUrl


class SegmentInput(BaseModel):
    source_text: str = Field(max_length=16000)
    translated_text: str = Field(max_length=16000)
    start_time: float = Field(ge=0, le=1e9, allow_inf_nan=False)
    end_time: float = Field(gt=0, le=1e9, allow_inf_nan=False)
    base_version: int | None = Field(default=None, ge=0)
    target_track_id: int | None = None


class TranslationInput(BaseModel):
    source_language: Literal['vi', 'en']
    target_language: Literal['vi', 'en']


class ProjectUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=16000)


class WorkerSegment(BaseModel):
    start_ms: int = Field(ge=0, le=10**12)
    end_ms: int = Field(gt=0, le=10**12)
    content: str = Field(min_length=1, max_length=16000)
    confidence: float | None = Field(default=None, ge=0, le=1)
    chunk: str | None = Field(default=None, max_length=100)

    @model_validator(mode='after')
    def check_range(self):
        if self.end_ms <= self.start_ms:
            raise ValueError('end_ms must be greater than start_ms')
        return self


class WorkerResult(BaseModel):
    language: Language
    segments: list[WorkerSegment] = Field(max_length=20000)
    words: list[WorkerSegment] = Field(default_factory=list, max_length=100000)
    model_name: str | None = Field(default=None, max_length=255)
    alignment: Literal['whisperx_ctc'] | None = None
    speech_intervals: list[SpeechInterval] | None = Field(default=None, max_length=20000)
    speech_detector: Literal['silero', 'energy', 'pyannote', 'whisper_asr'] | None = None
    dialect_status: Literal['completed', 'uncertain', 'not_configured', 'insufficient_speech', 'unavailable'] | None = None
    dialect_model: str | None = Field(default=None, max_length=255)
    retry_windows: int = Field(default=0, ge=0, le=20000)
    replaced_windows: int = Field(default=0, ge=0, le=20000)


class WorkerError(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


@app.get('/health')
def health():
    with mysql_transaction():
        return {'status': 'ok', 'mode': 'mysql', 'schema': 'subtitle_ai_db'}


@app.get('/api/v1/capabilities')
def capabilities():
    return {'database': 'mysql', 'speech_to_text': 'remote_gpu' if gpu_worker_available() else 'local',
            'url_ingestion': 'social', 'url_platforms': SUPPORTED_PLATFORMS, 'subtitle_export': ['srt', 'vtt'],
            'video_export': ['mp4'],
            'context_translation': contextual_model() is not None,
            'local_dialect': local_dialect_runtime() is not None}


@app.get('/api/v1/projects')
def list_projects(user_id: Owner):
    with transaction(user_id) as cursor:
        cursor.execute(PROJECTS + ' ORDER BY p.created_at DESC, p.project_id DESC', (None, user_id))
        return cursor.fetchall()


@app.get('/api/v1/projects/{project_id}')
def get_project(project_id: int, user_id: Owner, video_id: int | None = None):
    with transaction(user_id) as cursor:
        row = project_row(cursor, project_id, user_id, video_id)
        if row is None:
            raise HTTPException(404, 'Không tìm thấy dự án.')
        return row


@app.get('/api/v1/projects/{project_id}/videos')
def project_videos(project_id: int, user_id: Owner):
    with transaction(user_id) as cursor:
        if project_row(cursor, project_id, user_id) is None:
            raise HTTPException(404, 'Không tìm thấy dự án.')
        cursor.execute('''SELECT CAST(video_id AS CHAR) AS id,title,source_type,LOWER(status) AS status
                          FROM VIDEO WHERE project_id=%s ORDER BY video_id DESC''', (project_id,))
        return cursor.fetchall()


@app.patch('/api/v1/projects/{project_id}')
def update_project(project_id: int, body: ProjectUpdate, user_id: Owner):
    name = body.name.strip()
    if not name:
        raise HTTPException(422, 'Tên dự án không được để trống.')
    with transaction(user_id) as cursor:
        lock_project(cursor, project_id, user_id)
        if 'description' in body.model_fields_set:
            cursor.execute('UPDATE PROJECT SET project_name=%s,description=%s WHERE project_id=%s',
                           (name, body.description, project_id))
        else:
            cursor.execute('UPDATE PROJECT SET project_name=%s WHERE project_id=%s', (name, project_id))
        return project_row(cursor, project_id, user_id)


@app.post('/api/v1/jobs/{job_id}/cancel', status_code=202)
def cancel_owned_job(job_id: int, user_id: Owner):
    with transaction(user_id) as cursor:
        cursor.execute('''SELECT j.job_id,j.video_id,j.status
                          FROM PROCESSING_JOB j JOIN VIDEO v ON v.video_id=j.video_id
                          JOIN PROJECT p ON p.project_id=v.project_id
                          WHERE j.job_id=%s AND p.user_id=%s FOR UPDATE''', (job_id, user_id))
        job = cursor.fetchone()
        if job is None:
            raise HTTPException(404, 'Không tìm thấy tác vụ.')
        if job['status'] not in ('PENDING', 'RUNNING'):
            raise HTTPException(409, 'Chỉ có thể hủy tác vụ đang chờ hoặc đang chạy.')
        finish_job(cursor, job, 'CANCELLED', 'Tác vụ đã được người dùng hủy.')
    return {'job_id': str(job_id), 'status': 'cancelled'}


@app.get('/api/v1/jobs/{job_id}')
def get_job(job_id: int, user_id: Owner):
    with transaction(user_id) as cursor:
        cursor.execute('''SELECT CAST(j.job_id AS CHAR) AS job_id,j.job_type,j.status,j.progress,j.error_message,
                                 CAST(j.retry_of_job_id AS CHAR) AS retry_of_job_id
                          FROM PROCESSING_JOB j JOIN VIDEO v ON v.video_id=j.video_id
                          JOIN PROJECT p ON p.project_id=v.project_id WHERE j.job_id=%s AND p.user_id=%s''',
                       (job_id, user_id))
        job = cursor.fetchone()
        if not job:
            raise HTTPException(404, 'Không tìm thấy tác vụ.')
        steps = job_steps(cursor, job_id)
        cursor.execute('''SELECT audio_type,sample_rate,channels,duration_ms
                          FROM AUDIO_ASSET WHERE job_id=%s ORDER BY audio_id''', (job_id,))
        assets = cursor.fetchall()
        cursor.execute('''SELECT dialect_label,COUNT(*) AS intervals,SUM(end_ms-start_ms) AS duration_ms,
                                 AVG(dialect_confidence) AS confidence
                          FROM SPEECH_SEGMENT WHERE job_id=%s AND dialect_label IS NOT NULL
                            AND (dialect_label<>'UNKNOWN' OR dialect_confidence IS NOT NULL)
                          GROUP BY dialect_label ORDER BY dialect_label''', (job_id,))
        return {**job, 'steps': steps, 'audio_assets': assets, 'dialects': cursor.fetchall()}


def job_steps(cursor, job_id):
    cursor.execute('''SELECT s.step_type,s.execution_environment,s.status,s.error_message,s.metrics,
                             s.started_at,s.completed_at,m.model_name
                      FROM PROCESSING_STEP s LEFT JOIN MODEL_CONFIG m ON m.model_id=s.model_id
                      WHERE s.job_id=%s ORDER BY s.sequence_number''', (job_id,))
    return [{**row, 'metrics': json.loads(row['metrics'] or '{}')} for row in cursor.fetchall()]


def insert_project(cursor, name, user_id):
    cursor.execute('INSERT INTO PROJECT (user_id, project_name) VALUES (%s,%s)', (user_id, name))
    return cursor.lastrowid


@app.post('/api/v1/projects', status_code=201)
def create_project(body: ProjectInput, user_id: Owner, tasks: BackgroundTasks):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, 'Tên dự án không được để trống.')
    validate_video_url(body.source_url)
    with transaction(user_id) as cursor:
        project_id = insert_project(cursor, name, user_id)
        video_id = insert_url_video(cursor, project_id, name, body.source_url)
        queue_transcription(cursor, video_id, tasks, skip_asr=body.skip_asr)
        return project_row(cursor, project_id, user_id)


def validate_video_url(url):
    if not parse_video_url(str(url)):
        raise HTTPException(422, INVALID_VIDEO_URL)


def insert_url_video(cursor, project_id, name, url):
    cursor.execute('''INSERT INTO VIDEO (project_id,title,source_type,source_url)
                      VALUES (%s,%s,'URL',%s)''', (project_id, name, str(url)))
    return cursor.lastrowid


@app.post('/api/v1/projects/{project_id}/videos/url', status_code=201)
def add_url_video(project_id: int, body: VideoUrlInput, user_id: Owner, tasks: BackgroundTasks):
    validate_video_url(body.source_url)
    title = body.title.strip()
    if not title:
        raise HTTPException(422, 'Tên video không được để trống.')
    with transaction(user_id) as cursor:
        lock_project(cursor, project_id, user_id)
        video_id = insert_url_video(cursor, project_id, title, body.source_url)
        queued = queue_transcription(cursor, video_id, tasks)
        return {**queued, **project_row(cursor, project_id, user_id, video_id)}


@app.post('/api/v1/upload', status_code=201)
def upload_video(user_id: Owner, tasks: BackgroundTasks, file: UploadFile = File(...), skip_asr: bool = Form(False)):
    return save_upload(user_id, tasks, file, skip_asr=skip_asr)


@app.post('/api/v1/projects/{project_id}/videos/upload', status_code=201)
def add_uploaded_video(project_id: int, user_id: Owner, tasks: BackgroundTasks, file: UploadFile = File(...),
                       skip_asr: bool = Form(False)):
    with transaction(user_id) as cursor:
        lock_project(cursor, project_id, user_id)
    return save_upload(user_id, tasks, file, project_id, skip_asr=skip_asr)


def save_upload(user_id, tasks, file, project_id=None, *, skip_asr=False):
    filename = Path(file.filename or '').name
    suffix = Path(filename).suffix.lower()
    if suffix not in {'.mp4', '.mov', '.mkv', '.webm', '.mp3', '.wav', '.m4a'}:
        raise HTTPException(400, 'Định dạng tệp không được hỗ trợ.')
    UPLOADS.mkdir(parents=True, exist_ok=True)
    target = UPLOADS / f'{uuid4()}{suffix}'
    size = 0
    try:
        with target.open('xb') as output:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise HTTPException(413, 'Tệp vượt quá 500 MB.')
                output.write(chunk)
        if size == 0:
            raise HTTPException(400, 'Tệp rỗng.')
        try:
            duration = probe_media(target)
        except Exception as error:
            raise HTTPException(415, 'Không đọc được định dạng tệp hoặc tệp không có âm thanh.') from error
        with transaction(user_id) as cursor:
            name = Path(filename).stem[:255]
            if project_id is None:
                project_id = insert_project(cursor, name, user_id)
            else:
                lock_project(cursor, project_id, user_id)
            cursor.execute('''
                INSERT INTO VIDEO (project_id,title,source_type,file_path,file_size_bytes,duration_ms)
                VALUES (%s,%s,'UPLOAD',%s,%s,%s)
            ''', (project_id, name, str(target), size, duration))
            video_id = cursor.lastrowid
            if not skip_asr:
                queue_transcription(cursor, video_id, tasks)
            project = project_row(cursor, project_id, user_id, video_id)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return {**project, 'file_name': filename, 'video_id': str(video_id)}


@app.get('/api/v1/projects/{project_id}/media')
def project_media(project_id: int, user_id: Owner):
    with transaction(user_id) as cursor:
        cursor.execute('''SELECT v.file_path FROM VIDEO v JOIN PROJECT p ON p.project_id=v.project_id
                          WHERE p.project_id=%s AND p.user_id=%s ORDER BY v.video_id DESC LIMIT 1''',
                       (project_id, user_id))
        row = cursor.fetchone()
    return media_response(row)


@app.get('/api/v1/videos/{video_id}/media')
def video_media(video_id: int, user_id: Owner):
    with transaction(user_id) as cursor:
        row = owned_video(cursor, video_id, user_id)
    return media_response(row)


def media_response(row):
    return FileResponse(browser_preview(uploaded_media_path(row)))


def uploaded_media_path(row):
    path = Path(row['file_path']).resolve() if row and row['file_path'] else None
    if path is None or path.parent != UPLOADS.resolve() or not path.is_file():
        raise HTTPException(404, 'Không tìm thấy video.')
    return path


@app.post('/api/v1/projects/{project_id}/video-exports', status_code=201)
def export_project_video(project_id: int, body: VideoExportRequest, user_id: Owner, video_id: int | None = None):
    with transaction(user_id) as cursor:
        project = project_row(cursor, project_id, user_id, video_id)
        if project is None:
            raise HTTPException(404, 'Không tìm thấy dự án hoặc video.')
        if not project['video_id'] or not project['media_ready']:
            raise HTTPException(409, 'Video nguồn chưa sẵn sàng.')
        video = owned_video(cursor, project['video_id'], user_id)
        path = uploaded_media_path(video)
    return create_video_export(path, video['title'], body, owner=user_id)


@app.get('/api/v1/video-exports/{export_id}')
def download_project_video(export_id: UUID, user_id: Owner, filename: str = Query(default='video-phu-de.mp4', max_length=200)):
    return video_download(export_id, filename, owner=user_id)


def owned_video(cursor, video_id, user_id, lock=False):
    cursor.execute('''SELECT v.video_id,v.project_id,v.title,v.file_path,v.duration_ms,v.source_type,v.detected_language
                      FROM VIDEO v JOIN PROJECT p ON p.project_id=v.project_id
                      WHERE v.video_id=%s AND p.user_id=%s''' + (' FOR UPDATE' if lock else ''),
                   (video_id, user_id))
    row = cursor.fetchone()
    if not row:
        raise HTTPException(404, 'Không tìm thấy video.')
    return row


@app.post('/api/v1/videos/{video_id}/jobs', status_code=202)
def create_video_job(video_id: int, user_id: Owner, tasks: BackgroundTasks):
    with transaction(user_id) as cursor:
        row = owned_video(cursor, video_id, user_id, lock=True)
        if row['source_type'] not in ('URL', 'UPLOAD'):
            raise HTTPException(422, 'Chọn video tải lên hoặc đường dẫn video được hỗ trợ.')
        return queue_transcription(cursor, video_id, tasks)


@app.get('/api/v1/projects/{project_id}/jobs')
def project_history(project_id: int, user_id: Owner):
    with transaction(user_id) as cursor:
        if project_row(cursor, project_id, user_id) is None:
            raise HTTPException(404, 'Không tìm thấy dự án.')
        cursor.execute('''SELECT CAST(j.job_id AS CHAR) AS job_id,j.job_type,j.status,j.progress,
                              j.created_at,j.error_message,v.title
                          FROM PROCESSING_JOB j JOIN VIDEO v ON v.video_id=j.video_id
                          WHERE v.project_id=%s ORDER BY j.job_id DESC LIMIT 100''', (project_id,))
        return cursor.fetchall()


@app.delete('/api/v1/projects/{project_id}')
def delete_project(project_id: int, user_id: Owner):
    with transaction(user_id) as cursor:
        lock_project(cursor, project_id, user_id)
        paths = delete_project_content(cursor, project_id, UPLOADS.parent)
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning('Unable to remove project file %s', path.name)
    return {'deleted': True}


@app.post('/api/v1/projects/{project_id}/process', status_code=202)
def process_project(project_id: int, user_id: Owner, tasks: BackgroundTasks,
                    source_language: Literal['auto', 'vi', 'en'] = 'auto', accurate: bool = False,
                    force: bool = False, video_id: int | None = None):
    with transaction(user_id) as cursor:
        lock_project(cursor, project_id, user_id)
        cursor.execute('''SELECT video_id,source_type,source_url FROM VIDEO
                          WHERE project_id=%s AND (%s IS NULL OR video_id=%s)
                          ORDER BY video_id DESC LIMIT 1 FOR UPDATE''', (project_id, video_id, video_id))
        video = cursor.fetchone()
        if video_id is not None and video is None:
            raise HTTPException(404, 'Không tìm thấy video trong dự án.')
        if video is None or (
            video['source_type'] != 'UPLOAD' and not
            (video['source_type'] == 'URL' and parse_video_url(video['source_url']))
        ):
            raise HTTPException(422, INVALID_VIDEO_URL)
        return queue_transcription(cursor, video['video_id'], tasks, language=source_language,
                                   accurate=accurate, force=force)


def queue_transcription(cursor, video_id, tasks, *, language='auto', accurate=False, force=False, retry_of=None,
                        skip_asr=False):
    cursor.execute('''
        SELECT j.job_id,j.status,s.execution_environment FROM PROCESSING_JOB j
        JOIN PROCESSING_STEP s ON s.job_id=j.job_id AND s.step_type='TRANSCRIBE'
        WHERE j.video_id=%s AND j.job_type='TRANSCRIBE' ORDER BY j.job_id DESC LIMIT 1
    ''', (video_id,))
    previous = cursor.fetchone()
    if previous and (
        (previous['status'] == 'COMPLETED' and not force)
        or (previous['status'] in ('PENDING', 'RUNNING') and previous['execution_environment'] != 'COLAB')
    ):
        return {'job_id': str(previous['job_id']), 'status': previous['status'].lower(), 'pipeline': previous['execution_environment']}
    if previous and previous['status'] in ('PENDING','RUNNING'):
        finish_job(cursor, {'job_id': previous['job_id'], 'video_id': video_id}, 'FAILED',
                   'Tác vụ Colab cũ được thay bằng tác vụ mới.')
    cursor.execute('''
        INSERT INTO PROCESSING_JOB (video_id,retry_of_job_id,job_type,processing_parameters)
        VALUES (%s,%s,'TRANSCRIBE',%s)
    ''', (video_id, retry_of or (previous['job_id'] if previous and previous['status'] != 'COMPLETED' else None),
          json.dumps({'language': None if language == 'auto' else language, 'accurate': accurate, 'skip_asr': skip_asr})))
    job_id = cursor.lastrowid
    environment = 'GPU_SERVER' if not skip_asr and gpu_worker_available() else 'LOCAL'
    cursor.execute('''
        INSERT INTO PROCESSING_STEP (job_id,sequence_number,step_type,execution_environment)
        VALUES (%s,1,'PREPARE_AUDIO','LOCAL'),(%s,2,'TRANSCRIBE',%s),(%s,3,'OPTIMIZE','LOCAL')
    ''', (job_id, job_id, environment, job_id))
    cursor.execute("UPDATE VIDEO SET status='PENDING' WHERE video_id=%s", (video_id,))
    if environment == 'LOCAL':
        tasks.add_task(run_local_transcription, job_id)
    return {'job_id': str(job_id), 'status': 'pending', 'pipeline': environment}


def worker_auth(authorization: Annotated[str | None, Header()] = None):
    if not REMOTE_GPU or not compare_digest(
        (authorization or '').encode(), f'Bearer {WORKER_TOKEN}'.encode()
    ):
        raise HTTPException(401, 'Worker token không hợp lệ.')


def worker_job(cursor, job_id, environment='GPU_SERVER', *, required=True):
    cursor.execute('''
        SELECT j.job_id,j.video_id,j.status,j.processing_parameters,v.file_path,v.source_type,v.source_url,v.duration_ms,
            JSON_UNQUOTE(JSON_EXTRACT(s.parameters,'$.lease')) AS lease,
            JSON_EXTRACT(s.parameters,'$.expires') > UNIX_TIMESTAMP() AS lease_valid
        FROM PROCESSING_JOB j
        JOIN VIDEO v ON v.video_id=j.video_id JOIN PROJECT p ON p.project_id=v.project_id
        JOIN PROCESSING_STEP s ON s.job_id=j.job_id AND s.step_type='TRANSCRIBE'
        WHERE j.job_id=%s AND j.job_type='TRANSCRIBE'
            AND s.execution_environment=%s
        FOR UPDATE
    ''', (job_id, environment))
    job = cursor.fetchone()
    if job is None and required:
        raise HTTPException(404, 'Không tìm thấy tác vụ Colab.')
    return job


@worker_app.post('/api/v1/worker/jobs/claim', dependencies=[Depends(worker_auth)])
def worker_claim(job_id: int | None = None):
    note_worker_activity()
    with transaction() as cursor:
        cursor.execute('''
            SELECT j.job_id,j.video_id,j.processing_parameters FROM PROCESSING_JOB j
            JOIN VIDEO v ON v.video_id=j.video_id JOIN PROJECT p ON p.project_id=v.project_id
            JOIN PROCESSING_STEP s ON s.job_id=j.job_id AND s.step_type='TRANSCRIBE'
            WHERE j.job_type='TRANSCRIBE' AND j.status='PENDING'
                AND s.execution_environment='GPU_SERVER'
                AND (%s IS NULL OR j.job_id=%s)
                AND EXISTS (SELECT 1 FROM AUDIO_ASSET a WHERE a.job_id=j.job_id AND a.audio_type='NORMALIZED')
            ORDER BY j.job_id LIMIT 1 FOR UPDATE SKIP LOCKED
        ''', (job_id, job_id))
        job = cursor.fetchone()
        if not job:
            return {'job_id': None}
        lease = token_urlsafe(32)
        cursor.execute('''UPDATE PROCESSING_STEP SET status='RUNNING',started_at=CURRENT_TIMESTAMP(3),
                              parameters=JSON_OBJECT('lease',%s,'expires',UNIX_TIMESTAMP()+90)
                          WHERE job_id=%s AND step_type='TRANSCRIBE' ''', (lease, job['job_id']))
        cursor.execute("UPDATE PROCESSING_JOB SET status='RUNNING',started_at=CURRENT_TIMESTAMP(3) WHERE job_id=%s",
                       (job['job_id'],))
        cursor.execute("UPDATE VIDEO SET status='RUNNING' WHERE video_id=%s", (job['video_id'],))
        params = json.loads(job['processing_parameters'] or '{}')
        cursor.execute('''SELECT snr_estimate_db FROM AUDIO_ASSET WHERE job_id=%s AND audio_type='NORMALIZED'
                          ORDER BY audio_id DESC LIMIT 1''', (job['job_id'],))
        audio = cursor.fetchone()
        return {'job_id': str(job['job_id']), 'lease': lease, 'language': params.get('language'),
                'snr_estimate_db': audio['snr_estimate_db'] if audio else None}


Lease = Annotated[str, Header(alias='X-Worker-Lease')]


def verify_lease(job, lease):
    if not job['lease'] or not job['lease_valid'] or not compare_digest(job['lease'], lease):
        raise HTTPException(409, 'Quyền xử lý tác vụ đã hết hạn.')


@worker_app.post('/api/v1/worker/jobs/{job_id}/heartbeat', dependencies=[Depends(worker_auth)])
def worker_heartbeat(job_id: int, lease: Lease, metrics: ResourceMetrics | None = None):
    with transaction() as cursor:
        job = worker_job(cursor, job_id)
        verify_lease(job, lease)
        if job['status'] != 'RUNNING':
            raise HTTPException(409, 'Tác vụ đã kết thúc.')
        cursor.execute("""UPDATE PROCESSING_STEP SET parameters=JSON_SET(parameters,'$.expires',UNIX_TIMESTAMP()+90)
                          WHERE job_id=%s AND step_type='TRANSCRIBE'""", (job_id,))
        if metrics:
            save_sample(cursor, job_id, 'GPU_SERVER', metrics)
    note_worker_activity()
    return {'status': 'running'}


@worker_app.get('/api/v1/worker/jobs/{job_id}/media', dependencies=[Depends(worker_auth)])
def worker_media(job_id: int, lease: Lease):
    with transaction() as cursor:
        job = worker_job(cursor, job_id)
        verify_lease(job, lease)
        if job['status'] != 'RUNNING':
            raise HTTPException(409, 'Tác vụ đã kết thúc.')
        cursor.execute('''SELECT file_path FROM AUDIO_ASSET WHERE job_id=%s AND audio_type IN ('NORMALIZED','DENOISED')
                          ORDER BY audio_type='DENOISED' DESC,audio_id DESC LIMIT 1''', (job_id,))
        audio = cursor.fetchone()
        path = Path(audio['file_path']).resolve() if audio else None
        if path is None or path.parent != (UPLOADS.parent / 'audio').resolve() or not path.is_file():
            raise HTTPException(404, 'Không tìm thấy audio đã chuẩn hóa.')
    return FileResponse(path, filename=path.name)


def finish_job(cursor, job, status, error=None, *, failed_step=None):
    cursor.execute('''UPDATE PROCESSING_JOB SET status=%s,completed_at=CURRENT_TIMESTAMP(3),error_message=%s
                      WHERE job_id=%s''', (status, error, job['job_id']))
    if failed_step:
        cursor.execute('''UPDATE PROCESSING_STEP SET status=IF(step_type=%s,'FAILED','CANCELLED'),
                              completed_at=CURRENT_TIMESTAMP(3),error_message=IF(step_type=%s,%s,NULL)
                          WHERE job_id=%s AND status IN ('PENDING','RUNNING')''',
                       (failed_step, failed_step, error, job['job_id']))
    else:
        cursor.execute('''UPDATE PROCESSING_STEP SET status=%s,completed_at=CURRENT_TIMESTAMP(3),error_message=%s
                          WHERE job_id=%s AND status IN ('PENDING','RUNNING')''', (status, error, job['job_id']))
    cursor.execute('UPDATE VIDEO SET status=%s WHERE video_id=%s', (status, job['video_id']))


def run_local_transcription(job_id):
    with local_lock:
        with transaction() as cursor:
            job = worker_job(cursor, job_id, 'LOCAL')
            if job['status'] != 'PENDING':
                return
            for table in ('PROCESSING_JOB', 'PROCESSING_STEP'):
                cursor.execute(f"""UPDATE {table} SET status='RUNNING',started_at=CURRENT_TIMESTAMP(3)
                                   WHERE job_id=%s AND status='PENDING'""",
                               (job_id,))
            cursor.execute("UPDATE VIDEO SET status='RUNNING' WHERE video_id=%s", (job['video_id'],))
        try:
            normalized, snr = prepare_job_audio(job)
            settings = json.loads(job['processing_parameters'] or '{}')
            if settings.get('skip_asr'):
                with transaction() as cursor:
                    job = worker_job(cursor, job_id, 'LOCAL')
                    if job['status'] == 'RUNNING':
                        cursor.execute("""UPDATE PROCESSING_STEP SET metrics=JSON_OBJECT('skipped',TRUE)
                                          WHERE job_id=%s AND step_type IN ('TRANSCRIBE','OPTIMIZE')""", (job_id,))
                        cursor.execute('UPDATE PROCESSING_JOB SET progress=100 WHERE job_id=%s', (job_id,))
                        finish_job(cursor, job, 'COMPLETED')
                return
            accurate = settings.get('accurate', False) or (snr is not None and snr < 12)
            local_model = get_local_transcription_config()
            selected = local_whisper_model(local_model['model_name'], local_model['model_path']) if local_model else None
            if local_model and not selected:
                logger.warning('Configured Local model %s is unavailable. Using the default profile.', local_model['model_id'])
            result = transcribe_aligned(normalized, language=settings.get('language'), accurate=accurate,
                                        model_name=selected)
            if selected:
                result['model_name'] = local_model['model_name']
            if vietnamese(result['language']) and result['segments']:
                with transaction() as cursor:
                    cursor.execute("UPDATE PROCESSING_JOB SET progress=75 WHERE job_id=%s AND status='RUNNING'",
                                   (job_id,))
                    if not cursor.rowcount:
                        return
                timing = result.get('words') or result['segments']
                result.update(classify_local_dialect(normalized, result['language'], timing))
            with transaction() as cursor:
                job = worker_job(cursor, job_id, 'LOCAL', required=False)
                if job and job['status'] == 'RUNNING':
                    persist_result(cursor, job, WorkerResult.model_validate(result),
                                   configured_model_id=local_model['model_id'] if selected else None)
        except Exception as error:
            logger.exception('Local transcription failed for job %s', job_id)
            with transaction() as cursor:
                job = worker_job(cursor, job_id, 'LOCAL', required=False)
                if job and job['status'] == 'RUNNING':
                    if isinstance(error, VideoDownloadError):
                        finish_job(cursor, job, 'FAILED', str(error), failed_step='PREPARE_AUDIO')
                    else:
                        finish_job(cursor, job, 'FAILED', 'Không đọc hoặc nhận dạng được âm thanh. Kiểm tra nguồn rồi thử lại.')


def get_local_transcription_config():
    with transaction() as cursor:
        cursor.execute('''SELECT model_id,model_name,model_path FROM MODEL_CONFIG
                          WHERE model_type='ASR' AND execution_environment='LOCAL' AND is_active=TRUE
                            AND JSON_UNQUOTE(JSON_EXTRACT(settings,'$.pipeline_default'))='true'
                          ORDER BY model_id DESC LIMIT 1''')
        return cursor.fetchone()


def prepare_job_audio(job):
    audio_dir = UPLOADS.parent / 'audio'
    with transaction() as cursor:
        cursor.execute('''SELECT file_path,snr_estimate_db FROM AUDIO_ASSET WHERE job_id=%s AND audio_type IN ('NORMALIZED','DENOISED')
                          ORDER BY audio_type='DENOISED' DESC,audio_id DESC LIMIT 1''', (job['job_id'],))
        previous = cursor.fetchone()
        if previous:
            existing = Path(previous['file_path']).resolve()
            if existing.parent == audio_dir.resolve() and existing.is_file():
                return existing, previous['snr_estimate_db']
    path = Path(job['file_path']).resolve() if job['file_path'] else None
    if path is None and job['source_type'] == 'URL':
        UPLOADS.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=UPLOADS) as directory:
            downloaded = download_video(job['source_url'], Path(directory))
            duration = probe_media(downloaded)
            path = downloaded.replace(UPLOADS / f'{uuid4()}{downloaded.suffix}')
        try:
            with transaction() as cursor:
                cursor.execute('UPDATE VIDEO SET file_path=%s,file_size_bytes=%s,duration_ms=%s WHERE video_id=%s',
                               (str(path), path.stat().st_size, duration, job['video_id']))
        except Exception:
            path.unlink(missing_ok=True)
            raise
    if path is None or path.parent != UPLOADS.resolve() or not path.is_file():
        raise FileNotFoundError('Video unavailable')
    audio_dir.mkdir(parents=True, exist_ok=True)
    raw, normalized, denoised = (audio_dir / f'{uuid4()}.{extension}' for extension in ('mka', 'wav', 'wav'))
    try:
        raw_info = None
        try:
            raw_info = extract_raw_audio(path, raw)
        except (OSError, ValueError):
            raw.unlink(missing_ok=True)
            logger.info('Could not archive the original audio stream for job %s', job['job_id'])
        duration, snr, speech = prepare_audio(path, normalized)
        accuracy_requested = json.loads(job['processing_parameters'] or '{}').get('accurate', False)
        profile = 'accurate' if accuracy_requested or (snr is not None and snr < 12) else 'base'
        preferred = normalized
        denoise_status = 'skipped'
        binary = os.getenv('SUBTITLE_DENOISE_BINARY')
        if binary and snr is not None and snr < 12:
            try:
                denoised_duration = denoise_audio(binary, normalized, denoised)
                if abs(denoised_duration - duration) > 100:
                    raise ValueError('Denoising changed audio duration')
                preferred, denoise_status = denoised, 'completed'
            except (OSError, ValueError, subprocess.SubprocessError):
                denoised.unlink(missing_ok=True)
                denoise_status = 'failed'
                logger.info('Denoising failed for job %s; using normalized audio', job['job_id'])
        with transaction() as cursor:
            raw_id = None
            if raw_info:
                cursor.execute('''INSERT INTO AUDIO_ASSET
                    (video_id,job_id,audio_type,file_path,sample_rate,channels,duration_ms)
                    VALUES (%s,%s,'RAW',%s,%s,%s,%s)''',
                    (job['video_id'], job['job_id'], str(raw), *raw_info, duration))
                raw_id = cursor.lastrowid
            cursor.execute('''INSERT INTO AUDIO_ASSET
                (video_id,job_id,parent_audio_id,audio_type,file_path,sample_rate,channels,duration_ms,snr_estimate_db)
                VALUES (%s,%s,%s,'NORMALIZED',%s,16000,1,%s,%s)''',
                (job['video_id'], job['job_id'], raw_id, str(normalized), duration, snr))
            audio_id = cursor.lastrowid
            if preferred == denoised:
                cursor.execute('''INSERT INTO AUDIO_ASSET
                    (video_id,job_id,parent_audio_id,audio_type,file_path,sample_rate,channels,duration_ms,snr_estimate_db)
                    VALUES (%s,%s,%s,'DENOISED',%s,16000,1,%s,%s)''',
                    (job['video_id'], job['job_id'], audio_id, str(denoised), denoised_duration, snr))
                audio_id = cursor.lastrowid
            if speech:
                cursor.executemany('''INSERT INTO SPEECH_SEGMENT (audio_id,job_id,start_ms,end_ms,dialect_label)
                                      VALUES (%s,%s,%s,%s,'UNKNOWN')''',
                                   [(audio_id, job['job_id'], start, end) for start, end in speech])
            cursor.execute('UPDATE PROCESSING_JOB SET progress=35 WHERE job_id=%s', (job['job_id'],))
            cursor.execute("""UPDATE PROCESSING_STEP SET status='COMPLETED',metrics=%s,
                              started_at=COALESCE(started_at,CURRENT_TIMESTAMP(3)),completed_at=CURRENT_TIMESTAMP(3)
                              WHERE job_id=%s AND step_type='PREPARE_AUDIO'""",
                           (json.dumps({'raw_audio': bool(raw_id), 'denoise': denoise_status,
                                        'speech_intervals': len(speech)}), job['job_id']))
            cursor.execute('''UPDATE PROCESSING_STEP SET metrics=JSON_OBJECT('snr_estimate_db',%s,'profile',%s)
                              WHERE job_id=%s AND step_type='TRANSCRIBE' ''',
                           (snr, profile, job['job_id']))
    except Exception:
        for asset in (raw, normalized, denoised):
            asset.unlink(missing_ok=True)
        raise
    return preferred, snr


def resume_local_jobs(stop):
    recovered = False
    while not stop.is_set():
        try:
            with local_lock, transaction() as cursor:
                if not recovered:
                    cursor.execute('''
                        UPDATE PROCESSING_JOB j JOIN PROCESSING_STEP s ON s.job_id=j.job_id
                        JOIN VIDEO v ON v.video_id=j.video_id
                        SET j.status='PENDING',s.status='PENDING',v.status='PENDING'
                        WHERE j.job_type='TRANSCRIBE' AND j.status='RUNNING'
                            AND s.step_type='TRANSCRIBE' AND s.execution_environment='LOCAL'
                    ''')
                    cursor.execute('''
                        UPDATE PROCESSING_JOB j JOIN PROCESSING_STEP s ON s.job_id=j.job_id
                        SET j.status='PENDING',s.status='PENDING'
                        WHERE j.job_type='TRANSLATE' AND j.status='RUNNING' AND s.step_type='TRANSLATE'
                    ''')
                cursor.execute('''
                    UPDATE PROCESSING_STEP s JOIN PROCESSING_JOB j ON j.job_id=s.job_id
                    LEFT JOIN PROCESSING_STEP a ON a.job_id=j.job_id AND a.step_type='PREPARE_AUDIO'
                    SET s.execution_environment='LOCAL'
                    WHERE s.step_type='TRANSCRIBE' AND s.execution_environment='GPU_SERVER'
                        AND s.status='PENDING' AND j.status='PENDING'
                        AND (NOT %s OR a.completed_at < CURRENT_TIMESTAMP(3) - INTERVAL 45 SECOND)
                ''', (REMOTE_GPU,))
                cursor.execute('''
                    UPDATE PROCESSING_STEP s JOIN PROCESSING_JOB j ON j.job_id=s.job_id
                    JOIN VIDEO v ON v.video_id=j.video_id
                    SET s.execution_environment='LOCAL',s.status='PENDING',s.parameters=NULL,
                        j.status='PENDING',v.status='PENDING'
                    WHERE s.step_type='TRANSCRIBE' AND s.execution_environment='GPU_SERVER'
                      AND s.status='RUNNING' AND j.status='RUNNING'
                      AND JSON_EXTRACT(s.parameters,'$.expires') <= UNIX_TIMESTAMP()
                ''')
                cursor.execute('''SELECT j.job_id,j.job_type,s.execution_environment FROM PROCESSING_JOB j
                                  JOIN PROCESSING_STEP s ON s.job_id=j.job_id
                                  WHERE j.job_type IN ('TRANSCRIBE','TRANSLATE') AND j.status='PENDING'
                                      AND s.step_type=j.job_type AND (
                                          j.job_type='TRANSLATE' OR s.execution_environment='LOCAL' OR (
                                              s.execution_environment='GPU_SERVER' AND %s
                                              AND NOT EXISTS (SELECT 1 FROM AUDIO_ASSET a
                                                  WHERE a.job_id=j.job_id AND a.audio_type='NORMALIZED')
                                          )
                                      )
                                  ORDER BY j.job_id LIMIT 1''', (REMOTE_GPU,))
                next_job = cursor.fetchone()
            recovered = True
            if next_job and not stop.is_set():
                if next_job['job_type'] == 'TRANSLATE':
                    from .editor import run_translation

                    run_translation(next_job['job_id'])
                elif next_job['execution_environment'] == 'LOCAL':
                    run_local_transcription(next_job['job_id'])
                else:
                    prepare_remote_job(next_job['job_id'])
                continue
        except Exception:
            if recovered:
                logger.exception('Unable to resume MySQL processing jobs')
        stop.wait(3)


def prepare_remote_job(job_id):
    with transaction() as cursor:
        job = worker_job(cursor, job_id)
        if job['status'] != 'PENDING':
            return
        cursor.execute("""UPDATE PROCESSING_STEP SET status='RUNNING',started_at=CURRENT_TIMESTAMP(3)
                          WHERE job_id=%s AND step_type='PREPARE_AUDIO'""", (job_id,))
    try:
        prepare_job_audio(job)
    except Exception as error:
        logger.exception('Audio preparation failed for remote job %s', job_id)
        with transaction() as cursor:
            job = worker_job(cursor, job_id)
            if job['status'] == 'PENDING':
                finish_job(cursor, job, 'FAILED', str(error) if isinstance(error, VideoDownloadError)
                           else 'Không chuẩn hóa được audio trên máy chủ.', failed_step='PREPARE_AUDIO')


def save_revision(cursor, track_id, *, job_id=None, editor_id=None):
    cursor.execute('''
        SELECT segment_id,sequence_number,start_ms,end_ms,content,confidence
        FROM SUBTITLE_SEGMENT WHERE track_id=%s ORDER BY sequence_number
    ''', (track_id,))
    rows = cursor.fetchall()
    cursor.execute('''SELECT l.source_segment_id,l.target_segment_id FROM SEGMENT_LINK l
                      JOIN SUBTITLE_SEGMENT s ON s.segment_id=l.target_segment_id
                      WHERE s.track_id=%s ORDER BY l.source_segment_id''', (track_id,))
    links = {}
    for link in cursor.fetchall():
        links.setdefault(link['target_segment_id'], []).append(link['source_segment_id'])
    for row in rows:
        if row['segment_id'] in links:
            row['source_segment_ids'] = links[row['segment_id']]
    snapshot = json.dumps(rows, ensure_ascii=False)
    cursor.execute('SELECT COALESCE(MAX(version_no),0)+1 AS next_version FROM SUBTITLE_REVISION WHERE track_id=%s',
                   (track_id,))
    version = cursor.fetchone()['next_version']
    cursor.execute('''
        INSERT INTO SUBTITLE_REVISION (track_id,edited_by,created_by_job_id,version_no,snapshot)
        VALUES (%s,%s,%s,%s,%s)
    ''', (track_id, editor_id, job_id, version, snapshot))
    return version


@worker_app.post('/api/v1/worker/jobs/{job_id}/result', dependencies=[Depends(worker_auth)])
def worker_result(job_id: int, result: WorkerResult, lease: Lease):
    with transaction() as cursor:
        job = worker_job(cursor, job_id)
        verify_lease(job, lease)
        if job['status'] == 'COMPLETED':
            return {'status': 'completed'}
        if job['status'] != 'RUNNING':
            raise HTTPException(409, 'Tác vụ chưa chạy hoặc đã thất bại.')
        status = persist_result(cursor, job, result)
    return {'status': status}


def persist_result(cursor, job, result, *, configured_model_id=None):
    from .editor import record_quality_issues

    job_id = job['job_id']
    raw = [TimedText(item.start_ms, item.end_ms, item.content, item.chunk, item.confidence)
           for item in result.segments]
    words = [TimedText(item.start_ms, item.end_ms, item.content, item.chunk, item.confidence)
             for item in result.words]
    cues = optimize_transcript(raw, words, result.language, job.get('duration_ms'),
                               split_untimed=result.model_name == VIETNAMESE_ASR_NAME)
    if not cues:
        finish_job(cursor, job, 'FAILED', 'Đã đọc được video nhưng mô hình chưa nhận diện được lời nói rõ.')
        return 'failed'
    speech = save_speech_intervals(cursor, job, result.speech_intervals, result.speech_detector)
    if result.dialect_status:
        cursor.execute("""UPDATE PROCESSING_STEP SET metrics=JSON_SET(COALESCE(metrics,JSON_OBJECT()),
                           '$.dialect_status',%s,'$.dialect_model',%s)
                          WHERE job_id=%s AND step_type='TRANSCRIBE'""",
                       (result.dialect_status, result.dialect_model, job_id))
    cursor.execute("""UPDATE PROCESSING_STEP SET status='COMPLETED',completed_at=CURRENT_TIMESTAMP(3)
                      WHERE job_id=%s AND step_type='TRANSCRIBE'""", (job_id,))
    cursor.execute("""UPDATE PROCESSING_STEP SET status='RUNNING',
                      started_at=COALESCE(started_at,CURRENT_TIMESTAMP(3))
                      WHERE job_id=%s AND step_type='OPTIMIZE'""", (job_id,))
    if result.model_name:
        cursor.execute("""UPDATE PROCESSING_STEP
                          SET metrics=JSON_SET(COALESCE(metrics,JSON_OBJECT()),'$.model',%s)
                          WHERE job_id=%s AND step_type='TRANSCRIBE'""", (result.model_name, job_id))
        model_id = configured_model_id
        if model_id is None:
            cursor.execute('''SELECT m.model_id FROM MODEL_CONFIG m JOIN PROCESSING_STEP s
                                ON s.execution_environment=m.execution_environment
                              WHERE s.job_id=%s AND s.step_type='TRANSCRIBE' AND m.is_active=TRUE
                                AND m.model_type='ASR' AND m.model_name=%s
                              ORDER BY m.model_id DESC LIMIT 1''', (job_id, result.model_name))
            match = cursor.fetchone()
            model_id = match['model_id'] if match else None
        if model_id:
            cursor.execute('INSERT INTO JOB_MODEL (job_id,model_id) VALUES (%s,%s)', (job_id, model_id))
            cursor.execute("UPDATE PROCESSING_STEP SET model_id=%s WHERE job_id=%s AND step_type='TRANSCRIBE'",
                           (model_id, job_id))
    aligned_words = sum(len(group) for _, group in cues)
    cursor.execute("""UPDATE PROCESSING_STEP SET metrics=JSON_SET(COALESCE(metrics,JSON_OBJECT()),
                       '$.alignment',%s,'$.aligned_words',%s,'$.retry_windows',%s,'$.replaced_windows',%s)
                      WHERE job_id=%s AND step_type='TRANSCRIBE'""",
                   ((result.alignment or 'asr_words') if aligned_words else 'segments', aligned_words,
                    result.retry_windows, result.replaced_windows, job_id))
    cursor.execute('''
            INSERT INTO SUBTITLE_TRACK (video_id,created_by_job_id,language,track_type)
            VALUES (%s,%s,%s,'ORIGINAL')
    ''', (job['video_id'], job_id, result.language))
    track_id = cursor.lastrowid
    for number, (cue, group) in enumerate(cues, 1):
        cursor.execute('''
            INSERT INTO SUBTITLE_SEGMENT
                (track_id,created_by_job_id,sequence_number,start_ms,end_ms,content,confidence,speaker_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        ''', (track_id, job_id, number, cue.start_ms, cue.end_ms, cue.text, cue.confidence,
              speaker_for_range(speech, cue.start_ms, cue.end_ms)))
        segment_id = cursor.lastrowid
        if group:
            cursor.executemany('''
                INSERT INTO WORD_ALIGNMENT (segment_id,job_id,word_index,word_text,start_ms,end_ms,confidence)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
            ''', [(segment_id, job_id, index, word.text, word.start_ms, word.end_ms, word.confidence)
                  for index, word in enumerate(group, 1)])
    save_revision(cursor, track_id, job_id=job_id)
    record_quality_issues(cursor, track_id, job_id)
    finish_job(cursor, job, 'COMPLETED')
    cursor.execute('UPDATE PROCESSING_JOB SET progress=100 WHERE job_id=%s', (job_id,))
    cursor.execute('UPDATE VIDEO SET detected_language=%s WHERE video_id=%s', (result.language, job['video_id']))
    return 'completed'


@worker_app.post('/api/v1/worker/jobs/{job_id}/error', dependencies=[Depends(worker_auth)])
def worker_error(job_id: int, error: WorkerError, lease: Lease):
    with transaction() as cursor:
        job = worker_job(cursor, job_id)
        verify_lease(job, lease)
        if job['status'] in ('PENDING', 'RUNNING'):
            finish_job(cursor, job, 'FAILED', error.message)
        elif job['status'] != 'FAILED':
            raise HTTPException(409, 'Tác vụ đã kết thúc.')
    return {'status': 'failed'}


def translation_lock_available(cursor):
    cursor.execute("SHOW COLUMNS FROM SUBTITLE_SEGMENT LIKE 'is_locked'")
    return cursor.fetchone() is not None


@app.get('/api/v1/projects/{project_id}/segments')
def get_segments(project_id: int, user_id: Owner, video_id: int | None = None,
                 track_id: int | None = None, target_track_id: int | None = None):
    with transaction(user_id) as cursor:
        project = project_row(cursor, project_id, user_id, video_id)
        if project is None:
            raise HTTPException(404, 'Không tìm thấy dự án.')
        if track_id is not None:
            cursor.execute('SELECT track_id FROM SUBTITLE_TRACK WHERE track_id=%s AND video_id=%s',
                           (track_id, project['video_id']))
            if not cursor.fetchone():
                raise HTTPException(404, 'Track không thuộc video.')
        chosen_track = track_id or project['track_id']
        locked = 'COALESCE(tt.is_locked,FALSE)' if translation_lock_available(cursor) else 'FALSE'
        cursor.execute(f'''
            SELECT CAST(s.segment_id AS CHAR) AS id,s.sequence_number,
                s.start_ms / 1000.0 AS start_time,s.end_ms / 1000.0 AS end_time,
                s.content AS source_text,COALESCE(tt.content,'') AS translated_text,s.confidence,
                CAST(tt.segment_id AS CHAR) AS translated_id,{locked} AS translated_locked,
                tt.start_ms AS translated_start_ms,tt.end_ms AS translated_end_ms,
                SUBSTRING_INDEX(sp.speaker_label,':',-1) AS speaker_label,sp.dialect_label
            FROM SUBTITLE_SEGMENT s
            LEFT JOIN SPEAKER sp ON sp.speaker_id=s.speaker_id
            LEFT JOIN SUBTITLE_SEGMENT tt ON tt.segment_id=(
                SELECT l.target_segment_id FROM SEGMENT_LINK l
                JOIN SUBTITLE_SEGMENT t ON t.segment_id=l.target_segment_id
                WHERE l.source_segment_id=s.segment_id AND t.track_id=(
                    SELECT MAX(track_id) FROM SUBTITLE_TRACK WHERE source_track_id=s.track_id
                        AND (%s IS NULL OR track_id=%s)
                ) ORDER BY l.target_segment_id LIMIT 1
            )
            WHERE s.track_id=%s
            ORDER BY s.sequence_number
        ''', (target_track_id, target_track_id, chosen_track))
        rows = cursor.fetchall()
        cursor.execute('''SELECT CAST(w.segment_id AS CHAR) AS segment_id,w.word_text,w.start_ms,w.end_ms,w.confidence
                          FROM WORD_ALIGNMENT w JOIN SUBTITLE_SEGMENT s ON s.segment_id=w.segment_id
                          WHERE s.track_id=%s AND w.is_stale=FALSE ORDER BY w.word_index''',
                       (chosen_track,))
        words = {}
        for word in cursor.fetchall():
            words.setdefault(word.pop('segment_id'), []).append(word)
        cursor.execute('SELECT language,created_by_job_id FROM SUBTITLE_TRACK WHERE track_id=%s', (chosen_track,))
        track = cursor.fetchone()
        spans = []
        if track and vietnamese(track['language']) and track['created_by_job_id']:
            cursor.execute('''SELECT start_ms,end_ms,dialect_label,dialect_confidence FROM SPEECH_SEGMENT
                              WHERE job_id=%s AND dialect_confidence IS NOT NULL
                              ORDER BY start_ms,end_ms''', (track['created_by_job_id'],))
            spans = cursor.fetchall()
        ends = [span['end_ms'] for span in spans]
        for row in rows:
            row['dialect_confidence'] = None
            if spans:
                label, confidence = region_for_range(spans, ends, round(row['start_time'] * 1000),
                                                     round(row['end_time'] * 1000))
                row.update(dialect_label=label, dialect_confidence=confidence)
        return [{**row, 'translated_locked': bool(row['translated_locked']),
                 'start_time': float(row['start_time']), 'end_time': float(row['end_time']),
                 'words': words.get(row['id'], [])} for row in rows]


@app.post('/api/v1/projects/{project_id}/translate')
def translate_project(project_id: int, body: TranslationInput, user_id: Owner):
    pair = (body.source_language, body.target_language)
    if pair not in PAIRS:
        raise HTTPException(422, 'Hiện hỗ trợ dịch tiếng Việt và tiếng Anh theo hai chiều.')
    with transaction(user_id) as cursor:
        lock_project(cursor, project_id, user_id)
        source, rows = translation_source(cursor, project_id)
    try:
        results = translate_batch([row['content'] for row in rows], *pair)
    except Exception:
        logger.exception('Translation failed for project %s', project_id)
        raise HTTPException(503, 'Không chạy được mô hình dịch.')
    with transaction(user_id) as cursor:
        lock_project(cursor, project_id, user_id)
        current, current_rows = translation_source(cursor, project_id)
        if current != source or current_rows != rows:
            raise HTTPException(409, 'Phụ đề đã thay đổi trong lúc dịch.')
        save_translation(cursor, source, rows, results, body.target_language, user_id=user_id)
    return {'target_language': body.target_language, 'segments': get_segments(project_id, user_id)}


def translation_targets(cursor, source, language, *, lock=False):
    cursor.execute('''SELECT track_id FROM SUBTITLE_TRACK
                      WHERE video_id=%s AND source_track_id=%s AND language=%s AND track_type='TRANSLATION'
                      ORDER BY track_id DESC LIMIT 1''' + (' FOR UPDATE' if lock else ''),
                   (source['video_id'], source['track_id'], language))
    track = cursor.fetchone()
    if not track:
        return None, {}
    locked = 'tt.is_locked' if translation_lock_available(cursor) else 'FALSE'
    cursor.execute(f'''SELECT l.source_segment_id,tt.segment_id,tt.start_ms,tt.end_ms,tt.content,
                              {locked} AS is_locked
                       FROM SUBTITLE_SEGMENT tt JOIN SEGMENT_LINK l ON l.target_segment_id=tt.segment_id
                       JOIN SUBTITLE_SEGMENT s ON s.segment_id=l.source_segment_id
                       WHERE tt.track_id=%s AND s.track_id=%s
                       ORDER BY is_locked DESC,tt.segment_id''' + (' FOR UPDATE' if lock else ''),
                   (track['track_id'], source['track_id']))
    mapping = {}
    for row in cursor.fetchall():
        mapping.setdefault(row['source_segment_id'], row)
    return track['track_id'], mapping


def save_translation(cursor, source, rows, results, language, *, user_id=None, job_id=None):
    from .editor import record_quality_issues, remove_segment

    track_id, targets = translation_targets(cursor, source, language, lock=True)
    if not track_id:
        cursor.execute('''INSERT INTO SUBTITLE_TRACK (video_id,source_track_id,language,track_type,created_by_job_id)
                          VALUES (%s,%s,%s,'TRANSLATION',%s)''',
                       (source['video_id'], source['track_id'], language, job_id))
        track_id = cursor.lastrowid
    lock_available = translation_lock_available(cursor)
    cursor.execute('SELECT COALESCE(MAX(sequence_number),0) AS n FROM SUBTITLE_SEGMENT WHERE track_id=%s', (track_id,))
    next_sequence = cursor.fetchone()['n'] + 1
    used = set()
    for row, text in zip(rows, results, strict=True):
        target = targets.get(row['segment_id'])
        if target:
            if target['segment_id'] in used:
                cursor.execute('DELETE FROM SEGMENT_LINK WHERE source_segment_id=%s AND target_segment_id=%s',
                               (row['segment_id'], target['segment_id']))
                target = None
        if target:
            used.add(target['segment_id'])
            if not target['is_locked']:
                cursor.execute('''UPDATE SUBTITLE_SEGMENT SET start_ms=%s,end_ms=%s,content=%s
                                  WHERE segment_id=%s''',
                               (row['start_ms'], row['end_ms'], text if text is not None else target['content'],
                                target['segment_id']))
        elif text:
            cursor.execute('''INSERT INTO SUBTITLE_SEGMENT
                              (track_id,sequence_number,start_ms,end_ms,content,created_by_job_id)
                              VALUES (%s,%s,%s,%s,%s,%s)''',
                           (track_id, next_sequence, row['start_ms'], row['end_ms'], text, job_id))
            next_sequence += 1
            used.add(cursor.lastrowid)
            cursor.execute('INSERT INTO SEGMENT_LINK (source_segment_id,target_segment_id) VALUES (%s,%s)',
                           (row['segment_id'], cursor.lastrowid))
    locked = 'is_locked' if lock_available else 'FALSE'
    cursor.execute(f'''SELECT segment_id,{locked} AS is_locked FROM SUBTITLE_SEGMENT WHERE track_id=%s
                       ORDER BY start_ms,end_ms,segment_id''', (track_id,))
    kept = []
    for row in cursor.fetchall():
        if row['segment_id'] in used or row['is_locked']:
            kept.append(row['segment_id'])
        else:
            remove_segment(cursor, row['segment_id'])
    cursor.execute('UPDATE SUBTITLE_SEGMENT SET sequence_number=sequence_number+%s WHERE track_id=%s '
                   'ORDER BY sequence_number DESC', (next_sequence, track_id))
    cursor.executemany('UPDATE SUBTITLE_SEGMENT SET sequence_number=%s WHERE segment_id=%s',
                       [(number, identifier) for number, identifier in enumerate(kept, 1)])
    save_revision(cursor, track_id, editor_id=user_id, job_id=job_id)
    record_quality_issues(cursor, track_id, job_id)
    return track_id


def translation_source(cursor, project_id):
    cursor.execute('''SELECT t.track_id,t.video_id FROM SUBTITLE_TRACK t JOIN VIDEO v ON v.video_id=t.video_id
                      WHERE v.project_id=%s AND t.track_type='ORIGINAL'
                      ORDER BY v.video_id DESC,t.track_id DESC LIMIT 1 FOR UPDATE''', (project_id,))
    track = cursor.fetchone()
    if not track:
        raise HTTPException(422, 'Dự án chưa có phụ đề gốc.')
    cursor.execute('''SELECT segment_id,sequence_number,start_ms,end_ms,content
                      FROM SUBTITLE_SEGMENT WHERE track_id=%s ORDER BY sequence_number FOR UPDATE''',
                   (track['track_id'],))
    rows = cursor.fetchall()
    if not rows:
        raise HTTPException(422, 'Dự án chưa có phụ đề gốc.')
    return track, rows


@app.put('/api/v1/segments/{segment_id}')
def update_segment(segment_id: int, body: SegmentInput, user_id: Owner):
    start, end = round(body.start_time * 1000), round(body.end_time * 1000)
    if end <= start or not body.source_text.strip():
        raise HTTPException(422, 'Nhập nội dung phụ đề và mốc thời gian hợp lệ.')
    from .editor import record_quality_issues

    with transaction(user_id) as cursor:
        cursor.execute('''
            SELECT t.track_id,t.source_track_id,v.duration_ms,s.content,s.start_ms,s.end_ms FROM SUBTITLE_SEGMENT s
            JOIN SUBTITLE_TRACK t ON t.track_id=s.track_id
            JOIN VIDEO v ON v.video_id=t.video_id JOIN PROJECT p ON p.project_id=v.project_id
            WHERE s.segment_id=%s AND p.user_id=%s FOR UPDATE
        ''', (segment_id, user_id))
        segment = cursor.fetchone()
        if segment is None:
            raise HTTPException(404, 'Không tìm thấy đoạn phụ đề.')
        cursor.execute('SELECT COALESCE(MAX(version_no),0) AS version FROM SUBTITLE_REVISION WHERE track_id=%s',
                       (segment['track_id'],))
        source_version = cursor.fetchone()['version']
        if body.base_version is not None and body.base_version != source_version:
            raise HTTPException(409, 'Phụ đề đã thay đổi. Tải lại phiên bản mới trước khi lưu.')
        if segment['duration_ms'] is not None and end > segment['duration_ms']:
            raise HTTPException(422, 'Mốc kết thúc vượt quá thời lượng video.')
        if (start, end) != (segment['start_ms'], segment['end_ms']):
            cursor.execute('''SELECT 1 FROM SUBTITLE_SEGMENT WHERE track_id=%s AND segment_id<>%s
                              AND start_ms<%s AND end_ms>%s LIMIT 1''', (segment['track_id'], segment_id, end, start))
            if cursor.fetchone():
                raise HTTPException(422, 'Mốc thời gian chồng lên một đoạn khác trong track.')
        cursor.execute('''SELECT t.track_id,t.segment_id,t.content,t.start_ms,t.end_ms FROM SEGMENT_LINK l
                          JOIN SUBTITLE_SEGMENT t ON t.segment_id=l.target_segment_id
                          WHERE l.source_segment_id=%s AND t.track_id=(
                              SELECT MAX(track_id) FROM SUBTITLE_TRACK WHERE source_track_id=%s
                                  AND (%s IS NULL OR track_id=%s)
                          ) ORDER BY t.segment_id LIMIT 1 FOR UPDATE''',
                       (segment_id, segment['track_id'], body.target_track_id, body.target_track_id))
        translation = cursor.fetchone()
        if translation is None and body.translated_text.strip():
            raise HTTPException(422, 'Hãy tạo bản dịch trước khi chỉnh sửa.')
        changed_tracks = set()
        changes = [(segment, segment_id, body.source_text, start, end, bool(segment['source_track_id']))]
        if translation:
            synced = (translation['start_ms'], translation['end_ms']) == (segment['start_ms'], segment['end_ms'])
            changes.append((translation, translation['segment_id'], body.translated_text,
                            start if synced else translation['start_ms'], end if synced else translation['end_ms'], True))
        locked = None
        for row, identifier, content, cue_start, cue_end, is_translation in changes:
            if (row['content'], row['start_ms'], row['end_ms']) == (content, cue_start, cue_end):
                continue
            if not content.strip():
                raise HTTPException(422, 'Đoạn phụ đề không được để trống.')
            cursor.execute('UPDATE SUBTITLE_SEGMENT SET content=%s,start_ms=%s,end_ms=%s WHERE segment_id=%s',
                           (content, cue_start, cue_end, identifier))
            if is_translation and row['content'] != content and translation_lock_available(cursor):
                cursor.execute('UPDATE SUBTITLE_SEGMENT SET is_locked=TRUE WHERE segment_id=%s', (identifier,))
                if translation and identifier == translation['segment_id']:
                    locked = True
            cursor.execute('UPDATE WORD_ALIGNMENT SET is_stale=TRUE WHERE segment_id=%s', (identifier,))
            cursor.execute("UPDATE SUBTITLE_ISSUE SET status='STALE' WHERE segment_id=%s OR related_segment_id=%s",
                           (identifier, identifier))
            changed_tracks.add(row['track_id'])
        if changed_tracks:
            changed_tracks.add(segment['track_id'])
            for track_id in sorted(changed_tracks):
                revision = save_revision(cursor, track_id, editor_id=user_id)
                record_quality_issues(cursor, track_id)
                if track_id == segment['track_id']:
                    source_version = revision
    return {'saved': True, 'version': source_version, 'translated_locked': locked}


def subtitle_time(seconds, separator):
    total, millis = divmod(round(seconds * 1000), 1000)
    minutes, second = divmod(total, 60)
    hour, minute = divmod(minutes, 60)
    return f'{hour:02}:{minute:02}:{second:02}{separator}{millis:03}'


@app.get('/api/v1/projects/{project_id}/export/{fmt}')
def export_subtitles(project_id: int, fmt: str, user_id: Owner,
                     track: Literal['original', 'translation', 'bilingual'] = 'original'):
    if fmt not in ('srt', 'vtt'):
        raise HTTPException(400, 'Chỉ hỗ trợ SRT và VTT.')
    separator = ',' if fmt == 'srt' else '.'
    lines = ['WEBVTT', ''] if fmt == 'vtt' else []
    index = 0
    for row in get_segments(project_id, user_id):
        content = []
        if track != 'translation':
            content.append(row['source_text'])
        if track != 'original':
            content.append(row['translated_text'])
        content = '\n'.join(text for text in content if text.strip())
        if not content:
            continue
        index += 1
        if fmt == 'srt':
            lines.append(str(index))
        start = subtitle_time(row['start_time'], separator)
        end = subtitle_time(row['end_time'], separator)
        lines.extend([f'{start} --> {end}', content, ''])
    return PlainTextResponse(
        '\n'.join(lines) + '\n',
        media_type='text/vtt' if fmt == 'vtt' else 'application/x-subrip',
        headers={'Content-Disposition': f'attachment; filename="subtitles.{fmt}"'},
    )
