import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from time import time
from urllib.parse import urlencode
from uuid import UUID, uuid4

from fastapi import HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator


logger = logging.getLogger(__name__)
VIDEO_EXPORTS = Path(__file__).resolve().parent.parent / 'data' / 'video-exports'
DOWNLOAD_LIFETIME = 3600
RENDER_TIMEOUT = 30 * 60
invalid_filename_characters = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
render_lock = Lock()


class VideoCue(BaseModel):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0, le=10**12)
    text: str = Field(max_length=32000)

    @model_validator(mode='after')
    def check_timing(self):
        if self.end_ms <= self.start_ms:
            raise ValueError('Mốc kết thúc phải sau mốc bắt đầu.')
        return self


class VideoExportRequest(BaseModel):
    cues: list[VideoCue] = Field(min_length=1, max_length=20000)


ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 640
PlayResY: 360
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Caption,Arial,18,&H00FFFFFF,&H00FFFFFF,&H00000000,&H60000000,-1,0,0,0,100,100,0,0,1,1.7,0,2,24,24,22,1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""


def ass_time(centiseconds):
    seconds, hundredths = divmod(centiseconds, 100)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours}:{minutes:02}:{seconds:02}.{hundredths:02}'


def subtitle_script(cues, duration_ms):
    lines = []
    for cue in sorted(cues, key=lambda cue: cue.start_ms):
        # Treat edited captions as plain text, including literal ASS metacharacters.
        text = cue.text.replace('\x00', '').strip()
        if not text or (duration_ms is not None and cue.start_ms >= duration_ms):
            continue
        text = text.replace('\\', '\\\u2060').replace('{', r'\{').replace('}', r'\}')
        text = r'\N'.join(text.splitlines())
        start = ass_time(cue.start_ms // 10)
        end = ass_time((cue.end_ms + 9) // 10)
        lines.append(f'Dialogue: 0,{start},{end},Caption,,0,0,0,,{text}')
    if not lines:
        raise HTTPException(422, 'Chưa có phụ đề trong thời lượng video để tải xuống.')
    return ASS_HEADER + '\n'.join(lines) + '\n'


def video_duration(source):
    import av

    try:
        with source.open('rb') as handle, av.open(handle, options={'protocol_whitelist': 'file'}) as media:
            video = next((stream for stream in media.streams.video
                          if not stream.disposition & av.stream.Disposition.attached_pic), None)
            if video is None:
                raise HTTPException(422, 'Nguồn này chỉ có âm thanh. Hãy chọn video để tải MP4 có phụ đề.')
            return media.duration // 1000 if media.duration else None
    except (OSError, av.FFmpegError) as error:
        raise HTTPException(422, 'Máy chủ không đọc được video nguồn.') from error


def get_export_ffmpeg():
    configured = os.getenv('SUBTITLE_FFMPEG')
    if configured:
        return configured
    try:
        from imageio_ffmpeg import get_ffmpeg_exe

        return get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        binary = shutil.which('ffmpeg')
        if binary:
            return binary
        raise HTTPException(503, 'Máy chủ chưa cài FFmpeg để tạo video có phụ đề.') from None


def render_video(source, destination, script, work):
    captions = work / 'captions.ass'
    captions.write_text(script, encoding='utf-8')
    command = [
        get_export_ffmpeg(), '-nostdin', '-hide_banner', '-loglevel', 'error',
        '-protocol_whitelist', 'file', '-i', str(source),
        '-map', '0:V:0', '-map', '0:a:0?', '-map_metadata', '-1', '-map_chapters', '-1',
        '-filter_threads', '1', '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2,ass=captions.ass',
        '-c:v', 'libx264', '-threads:v', '2', '-preset', 'veryfast', '-crf', '21',
        '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '160k', '-movflags', '+faststart',
        str(destination),
    ]
    try:
        result = subprocess.run(command, cwd=work, timeout=RENDER_TIMEOUT, capture_output=True)
    except subprocess.TimeoutExpired as error:
        raise HTTPException(504, 'Tạo video quá thời gian. Hãy thử với video ngắn hơn.') from error
    except OSError as error:
        raise HTTPException(503, 'Máy chủ chưa chạy được FFmpeg để tạo video.') from error
    if result.returncode or not destination.is_file() or not destination.stat().st_size:
        detail = result.stderr.decode('utf-8', errors='replace')
        logger.warning('Video export failed: %s', detail[-1500:])
        if "No such filter: 'ass'" in detail or 'Unknown encoder' in detail:
            raise HTTPException(503, 'FFmpeg cần hỗ trợ libass và libx264 để tạo video có phụ đề.')
        raise HTTPException(422, 'Không tạo được MP4 từ video này.')


def export_directory(owner):
    return VIDEO_EXPORTS / ('demo' if owner is None else str(owner))


def export_filename(title):
    safe = invalid_filename_characters.sub('_', title).strip(' .')[:120]
    return f'{safe or "video"}-phu-de.mp4'


def expired(path):
    return time() - path.stat().st_mtime > DOWNLOAD_LIFETIME


def create_video_export(source, title, body, owner=None):
    if not render_lock.acquire(blocking=False):
        raise HTTPException(409, 'Máy chủ đang chuẩn bị một video khác. Hãy thử lại sau.')
    try:
        script = subtitle_script(body.cues, video_duration(source))
        folder = export_directory(owner)
        folder.mkdir(parents=True, exist_ok=True)
        for old in VIDEO_EXPORTS.glob('*/*.mp4'):
            try:
                if expired(old):
                    old.unlink(missing_ok=True)
            except OSError:
                continue
        identifier = uuid4()
        with TemporaryDirectory(dir=folder) as directory:
            work = Path(directory)
            output = work / 'render.mp4'
            render_video(source.resolve(), output, script, work)
            output.replace(folder / f'{identifier}.mp4')
        query = urlencode({'filename': export_filename(title)})
        return {'download_url': f'/video-exports/{identifier}?{query}'}
    finally:
        render_lock.release()


def video_download(export_id: UUID, filename, owner=None):
    path = export_directory(owner) / f'{export_id}.mp4'
    try:
        available = path.is_file() and not path.is_symlink() and not expired(path)
    except OSError:
        available = False
    if not available:
        raise HTTPException(404, 'Video tải xuống đã hết hạn. Hãy tạo lại video có phụ đề.')
    safe = invalid_filename_characters.sub('_', filename).strip(' .')
    if not safe.lower().endswith('.mp4'):
        safe = export_filename(safe)
    return FileResponse(path, media_type='video/mp4', filename=safe, headers={'Cache-Control': 'private, no-store'})
