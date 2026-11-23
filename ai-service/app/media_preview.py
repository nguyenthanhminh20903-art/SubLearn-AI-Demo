import logging
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock

import av
from fastapi import HTTPException

from .video_export import get_export_ffmpeg


logger = logging.getLogger(__name__)
preview_lock = Lock()


def preview_path(source: Path) -> Path:
    return source.parent.parent / 'previews' / f'{source.name}.mp4'


def needs_mp4(source: Path) -> bool:
    with source.open('rb') as handle, av.open(handle, options={'protocol_whitelist': 'file'}) as media:
        video = next((stream for stream in media.streams.video
                      if not stream.disposition & av.stream.Disposition.attached_pic), None)
        if video is None:
            return False
        video_codec = video.codec_context.name
        audio_codec = media.streams.audio[0].codec_context.name if media.streams.audio else None
        if source.suffix.lower() == '.mp4':
            return video_codec != 'h264' or audio_codec not in (None, 'aac')
        if source.suffix.lower() == '.webm':
            return video_codec not in ('vp8', 'vp9') or audio_codec not in (None, 'opus', 'vorbis')
        return True


def browser_preview(source: Path) -> Path:
    destination = preview_path(source)
    try:
        source_modified = source.stat().st_mtime_ns

        def current_preview():
            return destination.is_file() and destination.stat().st_mtime_ns >= source_modified

        if destination.parent.resolve() != destination.parent or destination.is_symlink():
            return source
        if current_preview():
            return destination
        if not needs_mp4(source):
            return source
        with preview_lock:
            if current_preview():
                return destination
            destination.parent.mkdir(parents=True, exist_ok=True)
            with TemporaryDirectory(dir=destination.parent) as scratch:
                output = Path(scratch) / 'preview.mp4'
                subprocess.run([
                    get_export_ffmpeg(), '-nostdin', '-v', 'error',
                    '-protocol_whitelist', 'file', '-i', str(source),
                    '-map', '0:V:0', '-map', '0:a:0?', '-map_metadata', '-1', '-map_chapters', '-1',
                    '-filter_threads', '1', '-vf', "scale='min(720,iw)':-2,pad=ceil(iw/2)*2:ceil(ih/2)*2",
                    '-c:v', 'libx264', '-threads:v', '2', '-preset', 'veryfast',
                    '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-movflags', '+faststart', str(output),
                ], capture_output=True, check=True, timeout=180)
                output.replace(destination)
        return destination
    except (OSError, av.FFmpegError, subprocess.SubprocessError, HTTPException) as error:
        logger.warning('Could not prepare preview for %s (%s)', source.name, type(error).__name__)
        return source
