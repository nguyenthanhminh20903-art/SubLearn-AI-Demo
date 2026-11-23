from __future__ import annotations
import logging
import sqlite3
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from typing import Literal
from fastapi import BackgroundTasks, FastAPI, UploadFile, File, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, HttpUrl

from .context_translation import contextual_model, translate_subtitle_cues
from .media import MAX_MEDIA_BYTES, download_video, probe_media, transcribe
from .social_video import INVALID_VIDEO_URL, SUPPORTED_PLATFORMS, VideoDownloadError, parse_video_url
from .translate import PAIRS, translate_batch
from .video_export import VideoExportRequest, create_video_export, video_download

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
UPLOADS = DATA / "uploads"
DB = DATA / "subtitle_demo.db"
UPLOADS.mkdir(parents=True, exist_ok=True)
MEDIA_SUFFIXES = {'.mp4', '.mov', '.mkv', '.webm', '.mp3', '.wav', '.m4a', '.3gp', '.opus', '.aac', '.ogg'}
asr_lock = Lock()
logger = logging.getLogger(__name__)

app = FastAPI(title="SubLearn AI - Local Processing Service", version="0.2.0",
              description="Local subtitle processing with Whisper.")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:5174", "http://127.0.0.1:5174"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@contextmanager
def connect():
    with closing(sqlite3.connect(DB)) as conn:
        conn.row_factory = sqlite3.Row
        with conn:
            yield conn

def init_db():
    with connect() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, name TEXT NOT NULL, source_url TEXT, source_type TEXT NOT NULL DEFAULT 'URL', status TEXT NOT NULL DEFAULT 'ready', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS segments(id TEXT PRIMARY KEY, project_id TEXT NOT NULL, sequence_number INTEGER NOT NULL, start_time REAL NOT NULL, end_time REAL NOT NULL, source_text TEXT NOT NULL, translated_text TEXT NOT NULL, confidence REAL DEFAULT 0.92, FOREIGN KEY(project_id) REFERENCES projects(id));
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, project_id TEXT NOT NULL, job_type TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY(project_id) REFERENCES projects(id));
        ''')
        count = c.execute('SELECT COUNT(*) n FROM projects').fetchone()['n']
        if count == 0:
            pid = 'demo-project-001'; now = datetime.now(timezone.utc).isoformat()
            c.execute('INSERT INTO projects (id,name,source_url,source_type,status,created_at) VALUES(?,?,?,?,?,?)',
                      (pid, 'English listening practice - Sample', 'https://example.com/demo-video', 'DEMO', 'completed', now))
            samples = [(1,0,3.2,'Welcome back to our English lesson.','Chào mừng bạn quay trở lại với bài học tiếng Anh.'),(2,3.2,6.8,'Today we are going to talk about artificial intelligence.','Hôm nay chúng ta sẽ nói về trí tuệ nhân tạo.'),(3,6.8,10.4,'It is changing the way people learn and work.','Nó đang thay đổi cách mọi người học tập và làm việc.'),(4,10.4,14.1,'Let us look at a few practical examples.','Hãy cùng xem một vài ví dụ thực tế.')]
            for n,s,e,src,tr in samples: c.execute('INSERT INTO segments VALUES(?,?,?,?,?,?,?,?)',(f'{pid}-{n}',pid,n,s,e,src,tr,0.96))
            c.execute('INSERT INTO jobs VALUES(?,?,?,?,?)',('demo-job-001',pid,'TRANSCRIBE_TRANSLATE','completed',now))
        columns = {row['name'] for row in c.execute('PRAGMA table_info(projects)')}
        for column in ('error_message', 'source_language', 'target_language'):
            if column not in columns:
                c.execute(f'ALTER TABLE projects ADD COLUMN {column} TEXT')
        c.execute("UPDATE projects SET status='failed',error_message='Tác vụ bị gián đoạn khi máy chủ khởi động lại.' WHERE status IN ('pending','running')")
        c.execute("UPDATE jobs SET status='failed' WHERE status IN ('pending','running')")
init_db()

class ProjectIn(BaseModel):
    name: str
    source_url: HttpUrl

class SegmentUpdate(BaseModel):
    source_text: str
    translated_text: str
    start_time: float
    end_time: float


class TranslationInput(BaseModel):
    source_language: Literal['vi', 'en']
    target_language: Literal['vi', 'en']
    mode: Literal['local', 'beam', 'context'] = 'local'

@app.get('/health')
def health(): return {'status':'ok','service':'sublearn-ai-service','version':'0.2.0','mode':'local'}

@app.get('/api/v1/capabilities')
def capabilities():
    return {'database':'sqlite','url_ingestion':'social','url_platforms':SUPPORTED_PLATFORMS,'file_upload':'enabled',
            'speech_to_text':'local','subtitle_export':['srt','vtt'],'video_export':['mp4'],
            'context_translation':contextual_model() is not None}

@app.get('/api/v1/projects')
def list_projects():
    with connect() as c:
        rows = c.execute('SELECT p.*, (SELECT COUNT(*) FROM segments s WHERE s.project_id=p.id) segment_count FROM projects p ORDER BY created_at DESC').fetchall()
        return [{**dict(r), 'media_ready': local_media(r['id']) is not None} for r in rows]

@app.post('/api/v1/projects')
def create_project(body: ProjectIn, tasks: BackgroundTasks):
    if not body.name.strip(): raise HTTPException(400,'Tên dự án không được để trống')
    pid = str(uuid.uuid4()); now = datetime.now(timezone.utc).isoformat()
    url = str(body.source_url)
    if not parse_video_url(url):
        raise HTTPException(422, INVALID_VIDEO_URL)
    project = create_queued_project(tasks, pid, body.name.strip(), url, 'URL', now)
    return project

@app.delete('/api/v1/projects/{project_id}')
def delete_project(project_id: str):
    with connect() as c:
        if not c.execute("DELETE FROM projects WHERE id=? AND status NOT IN ('pending','running')",(project_id,)).rowcount:
            if c.execute('SELECT 1 FROM projects WHERE id=?',(project_id,)).fetchone():
                raise HTTPException(409,'Video đang được xử lý.')
            raise HTTPException(404,'Không tìm thấy dự án')
        c.execute('DELETE FROM segments WHERE project_id=?',(project_id,))
        c.execute('DELETE FROM jobs WHERE project_id=?',(project_id,))
    path = local_media(project_id)
    if path:
        path.unlink(missing_ok=True)
    return {'deleted':True}

@app.post('/api/v1/upload')
def upload_video(tasks: BackgroundTasks, file: UploadFile = File(...)):
    filename = Path(file.filename or '').name
    suffix = Path(filename).suffix.lower()
    if suffix not in MEDIA_SUFFIXES:
        raise HTTPException(400,'Định dạng tệp chưa hỗ trợ.')
    pid = str(uuid.uuid4())
    target = UPLOADS / f'{pid}{suffix}'
    try:
        copy_upload(file, target)
        project = create_queued_project(tasks, pid, Path(filename).stem, filename, 'UPLOAD',
                                        datetime.now(timezone.utc).isoformat())
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return {**project, 'file_name': filename}


def copy_upload(file, target):
    size = 0
    with target.open('xb') as output:
        while chunk := file.file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_MEDIA_BYTES:
                raise HTTPException(413, 'Tệp vượt quá 500 MB.')
            output.write(chunk)
    if not size:
        raise HTTPException(400, 'Tệp tải lên đang trống.')


@app.post('/api/v1/projects/{project_id}/videos/upload', status_code=201)
def attach_failed_source(project_id: str, tasks: BackgroundTasks, file: UploadFile = File(...)):
    try:
        stem = str(uuid.UUID(project_id))
    except ValueError:
        raise HTTPException(404, 'Không tìm thấy dự án.') from None
    filename = Path(file.filename or '').name
    suffix = Path(filename).suffix.lower()
    if suffix not in MEDIA_SUFFIXES:
        raise HTTPException(415, 'Định dạng tệp chưa hỗ trợ.')
    target = UPLOADS / f'{stem}{suffix}'
    with TemporaryDirectory(dir=UPLOADS) as directory:
        staging = Path(directory) / f'upload{suffix}'
        copy_upload(file, staging)
        try:
            probe_media(staging)
        except Exception as error:
            raise HTTPException(415, 'Không đọc được tệp hoặc tệp không có luồng âm thanh.') from error
        attached = False
        try:
            with connect() as c:
                c.execute('BEGIN IMMEDIATE')
                row = c.execute('''SELECT p.*, (SELECT COUNT(*) FROM segments s WHERE s.project_id=p.id) AS segment_count
                                   FROM projects p WHERE p.id=?''', (project_id,)).fetchone()
                if row is None:
                    raise HTTPException(404, 'Không tìm thấy dự án.')
                if row['source_type'] != 'URL' or row['status'] != 'failed' or local_media(project_id):
                    raise HTTPException(409, 'Chỉ gắn tệp cho dự án URL đã tải thất bại và chưa có video.')
                staging.replace(target)
                attached = True
                c.execute("UPDATE projects SET status='pending',error_message=NULL WHERE id=?", (project_id,))
                job_id = insert_job(c, project_id)
        except Exception:
            if attached:
                target.unlink(missing_ok=True)
            raise
    tasks.add_task(run_transcription, project_id, job_id)
    return {**dict(row), 'status':'pending', 'error_message':None, 'media_ready':True, 'file_name':filename}


def create_queued_project(tasks, pid, name, source, source_type, created_at):
    with connect() as c:
        c.execute('''INSERT INTO projects (id,name,source_url,source_type,status,created_at)
                     VALUES (?,?,?,?,'pending',?)''', (pid,name,source,source_type,created_at))
        job_id = insert_job(c, pid)
    tasks.add_task(run_transcription, pid, job_id)
    return {'id':pid,'name':name,'source_url':source,'source_type':source_type,'status':'pending',
            'created_at':created_at,'segment_count':0,'media_ready':source_type=='UPLOAD'}


def insert_job(c, project_id):
    job_id = str(uuid.uuid4())
    c.execute("INSERT INTO jobs VALUES (?,?,'TRANSCRIBE','pending',?)",
              (job_id, project_id, datetime.now(timezone.utc).isoformat()))
    return job_id


def local_media(project_id):
    try:
        stem = str(uuid.UUID(project_id))
    except ValueError:
        return None
    return next((p for p in UPLOADS.glob(f'{stem}.*')
                 if p.suffix in MEDIA_SUFFIXES and p.is_file() and not p.is_symlink()), None)


@app.get('/api/v1/projects/{project_id}/media')
def project_media(project_id: str):
    with connect() as c:
        if not c.execute('SELECT 1 FROM projects WHERE id=?',(project_id,)).fetchone():
            raise HTTPException(404,'Không tìm thấy dự án.')
    path = local_media(project_id)
    if not path:
        raise HTTPException(404,'Video chưa sẵn sàng.')
    return FileResponse(path)


@app.post('/api/v1/projects/{project_id}/video-exports', status_code=201)
def export_project_video(project_id: str, body: VideoExportRequest):
    with connect() as cursor:
        project = cursor.execute('SELECT name FROM projects WHERE id=?', (project_id,)).fetchone()
        if project is None:
            raise HTTPException(404, 'Không tìm thấy dự án.')
    path = local_media(project_id)
    if path is None:
        raise HTTPException(409, 'Video nguồn chưa sẵn sàng.')
    return create_video_export(path, project['name'], body)


@app.get('/api/v1/video-exports/{export_id}')
def download_project_video(export_id: uuid.UUID, filename: str = Query(default='video-phu-de.mp4', max_length=200)):
    return video_download(export_id, filename)


def run_transcription(project_id, job_id, language=None, accurate=False):
    with asr_lock:
        with connect() as c:
            if not c.execute("UPDATE projects SET status='running' WHERE id=? AND status='pending'",(project_id,)).rowcount:
                return
            project = c.execute('SELECT * FROM projects WHERE id=?',(project_id,)).fetchone()
            c.execute("UPDATE jobs SET status='running' WHERE id=?",(job_id,))
        error_message = 'Không đọc được tệp video hoặc âm thanh.'
        try:
            path = local_media(project_id)
            if not path and project['source_type'] == 'URL':
                error_message = 'Không đọc được tệp từ đường dẫn. Hãy thử tải tệp video lên.'
                with TemporaryDirectory(dir=UPLOADS) as directory:
                    downloaded = download_video(project['source_url'], Path(directory))
                    if downloaded.suffix not in MEDIA_SUFFIXES:
                        raise ValueError('Unsupported video container')
                    path = downloaded.replace(UPLOADS / f'{project_id}{downloaded.suffix}')
            if not path:
                raise FileNotFoundError(project_id)
            error_message = 'Không thể nhận dạng âm thanh. Vui lòng thử lại sau.'
            segments, detected_language = transcribe(path, language, accurate)
            if not segments:
                error_message = 'Không phát hiện lời nói rõ trong video.'
                raise ValueError(error_message)
            with connect() as c:
                c.execute('DELETE FROM segments WHERE project_id=?',(project_id,))
                c.executemany('INSERT INTO segments VALUES (?,?,?,?,?,?,?,NULL)', [
                    (str(uuid.uuid4()),project_id,i,start,end,content,'')
                    for i,(start,end,content) in enumerate(segments,1)
                ])
                c.execute("""UPDATE projects SET status='completed',source_language=?,target_language=NULL
                             WHERE id=?""",(detected_language,project_id))
                c.execute("UPDATE jobs SET status='completed' WHERE id=?",(job_id,))
        except Exception as error:
            if isinstance(error, VideoDownloadError):
                error_message = str(error)
            logger.exception('Transcription failed for project %s', project_id)
            with connect() as c:
                c.execute("UPDATE projects SET status='failed',error_message=? WHERE id=?",(error_message,project_id))
                c.execute("UPDATE jobs SET status='failed' WHERE id=?",(job_id,))

@app.post('/api/v1/projects/{project_id}/process')
def process_project(project_id: str, tasks: BackgroundTasks,
                    source_language: Literal['auto', 'vi', 'en'] = 'auto', accurate: bool = False):
    with connect() as c:
        p=c.execute('SELECT * FROM projects WHERE id=?',(project_id,)).fetchone()
        if not p: raise HTTPException(404,'Không tìm thấy dự án')
        if p['source_type'] == 'DEMO':
            return {'status':'completed','pipeline':'LOCAL'}
        if p['source_type'] == 'URL' and not local_media(project_id) and not parse_video_url(p['source_url']):
            raise HTTPException(422, INVALID_VIDEO_URL)
        updated = c.execute("""UPDATE projects SET status='pending',error_message=NULL
                               WHERE id=? AND status NOT IN ('pending','running')""",(project_id,))
        if not updated.rowcount:
            status = c.execute('SELECT status FROM projects WHERE id=?',(project_id,)).fetchone()['status']
            return {'status':status,'pipeline':'LOCAL'}
        job_id = insert_job(c, project_id)
    tasks.add_task(run_transcription, project_id, job_id, None if source_language == 'auto' else source_language, accurate)
    return {'status':'pending','pipeline':'LOCAL'}

@app.get('/api/v1/projects/{project_id}/segments')
def get_segments(project_id: str):
    with connect() as c:
        if not c.execute('SELECT 1 FROM projects WHERE id=?',(project_id,)).fetchone(): raise HTTPException(404,'Không tìm thấy dự án')
        return [dict(r) for r in c.execute('SELECT * FROM segments WHERE project_id=? ORDER BY sequence_number',(project_id,)).fetchall()]


@app.post('/api/v1/projects/{project_id}/translate')
def translate_project(project_id: str, body: TranslationInput):
    pair = (body.source_language, body.target_language)
    if pair not in PAIRS:
        raise HTTPException(422, 'Chọn tiếng Việt và tiếng Anh theo hai chiều khác nhau.')
    model = contextual_model() if body.mode == 'context' else None
    if body.mode == 'context' and model is None:
        raise HTTPException(503, 'Máy chủ chưa cấu hình mô hình dịch theo ngữ cảnh.')
    with connect() as c:
        project = c.execute('SELECT status,target_language FROM projects WHERE id=?', (project_id,)).fetchone()
        if project is None:
            raise HTTPException(404, 'Không tìm thấy dự án.')
        if project['status'] in ('pending', 'running'):
            raise HTTPException(409, 'Đợi video xử lý xong rồi dịch phụ đề.')
        query = 'SELECT id,source_text,translated_text FROM segments WHERE project_id=? ORDER BY sequence_number'
        rows = [tuple(row) for row in c.execute(query, (project_id,))]
    if not rows or not any(row[1].strip() for row in rows):
        raise HTTPException(422, 'Dự án chưa có lời thoại để dịch.')
    try:
        if model:
            results = translate_subtitle_cues([(row[0], row[1]) for row in rows], body.target_language, {}, model)
        else:
            results = translate_batch([row[1] for row in rows], *pair, use_beam=body.mode == 'beam')
    except Exception as error:
        logger.exception('Translation failed for project %s', project_id)
        raise HTTPException(503, 'Không chạy được mô hình dịch. Vui lòng thử lại sau.') from error
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        current = c.execute('SELECT status,target_language FROM projects WHERE id=?', (project_id,)).fetchone()
        if current != project or [tuple(row) for row in c.execute(query, (project_id,))] != rows:
            raise HTTPException(409, 'Dự án đã thay đổi trong lúc dịch. Hãy dịch lại.')
        c.executemany('UPDATE segments SET translated_text=? WHERE id=?',
                      [(text, row[0]) for row, text in zip(rows, results, strict=True)])
        c.execute('UPDATE projects SET source_language=?,target_language=? WHERE id=?', (*pair, project_id))
    return {'target_language': body.target_language, 'segments': get_segments(project_id)}

@app.put('/api/v1/segments/{segment_id}')
def update_segment(segment_id: str, body: SegmentUpdate):
    if body.start_time<0 or body.end_time<=body.start_time: raise HTTPException(400,'Mốc thời gian không hợp lệ')
    with connect() as c:
        cur=c.execute('UPDATE segments SET source_text=?,translated_text=?,start_time=?,end_time=? WHERE id=?',(body.source_text,body.translated_text,body.start_time,body.end_time,segment_id))
        if not cur.rowcount: raise HTTPException(404,'Không tìm thấy đoạn phụ đề')
    return {'saved':True}

def fmt_srt_time(sec: float):
    ms=round(sec*1000); h=ms//3600000; m=(ms%3600000)//60000; s=(ms%60000)//1000; milli=ms%1000
    return f'{h:02}:{m:02}:{s:02},{milli:03}'

@app.get('/api/v1/projects/{project_id}/export/{fmt}')
def export_subtitles(project_id: str, fmt: str,
                     track: Literal['original', 'translation', 'bilingual'] = 'original'):
    if fmt not in ('srt','vtt'): raise HTTPException(400,'Chỉ hỗ trợ SRT và VTT')
    rows=get_segments(project_id); lines=[]
    if fmt=='vtt': lines.append('WEBVTT\n')
    index = 0
    for r in rows:
        start=fmt_srt_time(r['start_time']); end=fmt_srt_time(r['end_time'])
        if fmt=='vtt': start=start.replace(',','.'); end=end.replace(',','.')
        texts = [r['source_text']] if track != 'translation' else []
        if track != 'original':
            texts.append(r['translated_text'])
        content = '\n'.join(text for text in texts if text.strip())
        if content:
            index += 1
            lines += ([str(index)] if fmt=='srt' else []) + [f'{start} --> {end}',content,'']
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse('\n'.join(lines),media_type='text/plain',headers={'Content-Disposition':f'attachment; filename="subtitles.{fmt}"'})
