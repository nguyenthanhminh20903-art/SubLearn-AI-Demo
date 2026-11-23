import os
import json
import logging
import shutil
import subprocess
import sys
import wave
from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory

from .social_video import (INVALID_VIDEO_URL, VideoDownloadError, download_network_error,
                           parse_video_url, resolve_short_link)
from .dialects import vietnamese


MAX_MEDIA_BYTES = 500 * 1024 * 1024
MODEL_DIRECTORY = Path(__file__).resolve().parent.parent / 'data' / 'models'
LOCAL_WHISPER_MODELS = {'tiny', 'base', 'small', 'medium', 'large-v3', 'large-v3-turbo'}
VIETNAMESE_ASR_NAME = 'PhoWhisper-small'
VIETNAMESE_ASR_FILE = 'ggml-phowhisper-small-q5_0.bin'
logger = logging.getLogger(__name__)
CDN_ERRORS = ('timed out', 'connection reset', 'failed to connect', 'http error 403',
              'http error 404', 'http error 500', 'http error 502', 'http error 503', 'http error 504')


def local_model_path(filename):
    root = MODEL_DIRECTORY.resolve()
    try:
        path = (root / filename).resolve()
        return path if path.is_relative_to(root) and path.exists() else None
    except (ValueError, OSError, RuntimeError):
        return None


def local_whisper_model(name, filename=None):
    if os.getenv('SUBTITLE_MODEL_DEVICE', 'cpu') != 'cpu':
        return None
    if filename:
        path = local_model_path(filename)
        return str(path) if path and path.is_file() and path.suffix.lower() == '.bin' else None
    return name if name in LOCAL_WHISPER_MODELS else None


def local_vietnamese_asr():
    filename = os.getenv('SUBTITLE_VI_ASR_CHECKPOINT', VIETNAMESE_ASR_FILE).strip()
    return local_whisper_model('', filename) if filename else None


def cpu_thread_count():
    configured = os.getenv('SUBTITLE_WHISPER_THREADS')
    if configured:
        try:
            return max(1, min(int(configured), os.cpu_count() or 1))
        except ValueError:
            pass
    return max(2, min((os.cpu_count() or 2) - 1, 6))


def download_video(url, directory):
    source = parse_video_url(url)
    if not source:
        raise VideoDownloadError(INVALID_VIDEO_URL)
    if source.short_link:
        source = resolve_short_link(source)
    formats = ['best[ext=mp4][height<=480]', 'best[height<=480]']
    ffmpeg = shutil.which(os.getenv('SUBTITLE_FFMPEG') or 'ffmpeg')
    if not ffmpeg:
        try:
            from imageio_ffmpeg import get_ffmpeg_exe

            ffmpeg = get_ffmpeg_exe()
        except (ImportError, RuntimeError):
            pass
    ffmpeg_args = []
    if ffmpeg and (resolved_ffmpeg := shutil.which(ffmpeg)):
        ffmpeg_args = ['--ffmpeg-location', resolved_ffmpeg]
        formats.append('bestvideo[height<=720]+bestaudio')
    formats.extend(('best', 'bestaudio'))
    command = [
        sys.executable, '-m', 'yt_dlp', '--ignore-config', '--no-playlist',
        '--playlist-items', '1', '--use-extractors', source.extractors,
        '--quiet', '--no-progress', '--js-runtimes', 'node', '--max-filesize', '500M',
        *ffmpeg_args, '-f', '/'.join(formats),
    ]
    output = directory

    def download(arguments, timeout):
        return subprocess.run(command + arguments + ['-o', str(output / 'video.%(ext)s'), '--', source.url],
                              capture_output=True, encoding='utf-8', errors='replace', timeout=timeout)

    try:
        result = download(['--socket-timeout', '15', '--retries', '2', '--extractor-retries', '2'], 600)
        detail = (result.stderr or '').lower()
        media_request = '[download]' in detail or 'unable to download video data' in detail
        if result.returncode and media_request and any(marker in detail for marker in CDN_ERRORS):
            output = directory / 'fallback'
            output.mkdir()
            result = download(['--check-formats', '--socket-timeout', '8', '--retries', '0',
                               '--extractor-retries', '0'], 120)
            if result.returncode and 'requested format is not available' in (result.stderr or '').lower():
                raise VideoDownloadError(f'Các luồng video {source.platform} cung cấp đang không tải được. '
                                         'Thử lại sau hoặc tải tệp vào dự án.')
    except subprocess.TimeoutExpired:
        raise VideoDownloadError(f'Tải video từ {source.platform} quá thời gian. Hãy thử tải tệp lên.') from None
    if result.returncode:
        detail = (result.stderr or '').lower()
        network_error = download_network_error(source.platform, detail)
        if network_error:
            raise VideoDownloadError(network_error)
        if any(marker in detail for marker in ('http error 429', 'too many requests', 'rate limit')):
            raise VideoDownloadError(f'{source.platform} đang giới hạn yêu cầu. Thử lại sau hoặc tải tệp vào dự án này.')
        if 'fresh cookies' in detail:
            raise VideoDownloadError(f'{source.platform} yêu cầu xác minh phiên truy cập để tải video. '
                                     'Hãy tải tệp vào dự án này để tiếp tục nhận dạng.')
        if any(marker in detail for marker in ('private video', 'login required', 'requires login',
                                               'login is required', 'sign in', 'not available in your region')):
            raise VideoDownloadError(f'{source.platform} yêu cầu quyền truy cập cho video này. '
                                     'Bạn có thể tải tệp được phép sử dụng vào dự án.')
        if any(marker in detail for marker in ('unexpected response from webpage', 'unable to extract', 'cannot parse data',
                                               'http error 403')):
            raise VideoDownloadError(f'Máy chủ chưa lấy được tệp video từ {source.platform}. '
                                     'Hãy tải tệp vào dự án này để tiếp tục nhận dạng.')
        raise VideoDownloadError(
            f'Không tải được video từ {source.platform}. Kiểm tra video có công khai, còn khả dụng và được phép tải, '
            'hoặc tải tệp lên.'
        )
    files = [path for path in output.glob('video.*') if path.suffix not in ('.part', '.ytdl')]
    if len(files) != 1 or not 0 < files[0].stat().st_size <= MAX_MEDIA_BYTES:
        raise VideoDownloadError(f'{source.platform} không trả về tệp video hợp lệ trong giới hạn 500 MB.')
    try:
        probe_media(files[0])
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        raise VideoDownloadError(f'Không tìm thấy luồng âm thanh đọc được trong video từ {source.platform}.') from error
    return files[0]


def probe_media(path):
    ffprobe = os.getenv('SUBTITLE_FFPROBE') or shutil.which('ffprobe')
    if ffprobe:
        result = subprocess.run([
            ffprobe, '-v', 'error', '-protocol_whitelist', 'file',
            '-show_entries', 'stream=codec_type:format=duration', '-of', 'json', str(path),
        ], capture_output=True, encoding='utf-8', timeout=20, check=True)
        details = json.loads(result.stdout)
        if not any(stream['codec_type'] == 'audio' for stream in details.get('streams', [])):
            raise ValueError('Media has no audio stream')
        value = details.get('format', {}).get('duration')
        return round(float(value) * 1000) if value not in (None, 'N/A') else None

    import av

    with av.open(str(path), options={'protocol_whitelist': 'file'}) as container:
        if not container.streams.audio:
            raise ValueError('Media has no audio stream')
        return round(container.duration / 1000) if container.duration is not None else None


def extract_raw_audio(source, destination):
    import av

    try:
        with source.open('rb') as handle, av.open(handle, options={'protocol_whitelist': 'file'}) as media:
            if not media.streams.audio:
                raise ValueError('Media has no audio stream')
            audio = media.streams.audio[0]
            with av.open(str(destination), mode='w', format='matroska') as output:
                stream = output.add_stream_from_template(audio)
                for packet in media.demux(audio):
                    if packet.dts is not None:
                        packet.stream = stream
                        output.mux(packet)
            return audio.codec_context.sample_rate, audio.codec_context.channels
    except av.FFmpegError as error:
        raise ValueError('Original audio cannot be saved as Matroska') from error


def denoise_audio(binary, normalized, destination):
    import numpy as np
    from faster_whisper.audio import decode_audio

    with TemporaryDirectory(dir=destination.parent) as directory:
        work = Path(directory)
        source = work / 'input.wav'
        output = work / 'out'
        audio = decode_audio(str(normalized), sampling_rate=48000)
        with wave.open(str(source), 'wb') as writer:
            writer.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
            writer.writeframes((np.clip(audio, -1, 1) * 32767).astype('<i2').tobytes())
        subprocess.run([binary, '--compensate-delay', '--out-dir', str(output), str(source)],
                       check=True, capture_output=True, timeout=300)
        files = list(output.glob('*.wav'))
        if len(files) != 1:
            raise ValueError('DeepFilterNet did not return one WAV file')
        return prepare_audio(files[0], destination)[0]


def prepare_audio(source, destination):
    import numpy as np
    from faster_whisper.audio import decode_audio

    sample_rate = 16000
    with source.open('rb') as input_file:
        audio = decode_audio(input_file, sampling_rate=sample_rate)
    duration_ms = round(audio.size * 1000 / sample_rate)
    if duration_ms <= 0:
        raise ValueError('Media has no decodable sound')
    with wave.open(str(destination), 'wb') as output:
        output.setparams((1, 2, sample_rate, 0, 'NONE', 'not compressed'))
        output.writeframes((np.clip(audio, -1, 1) * 32767).astype('<i2').tobytes())
    snr, intervals = speech_energy(audio)
    return duration_ms, snr, intervals


def speech_energy(audio):
    import numpy as np

    window = 320
    count = audio.size // window
    if count == 0:
        return None, []
    frames = audio[:count * window].reshape(-1, window)
    levels = 10 * np.log10(np.mean(frames * frames, axis=1) + 1e-10)
    noise, peak = np.percentile(levels, [20, 90])
    threshold = min(-26, max(noise + 10, -50))
    intervals = []
    for index in np.flatnonzero(levels > threshold):
        start, end = int(index) * 20, (int(index) + 1) * 20
        if intervals and start - intervals[-1][1] <= 200:
            intervals[-1] = (intervals[-1][0], end)
        else:
            intervals.append((start, end))
    return float(peak - noise), [(start, end) for start, end in intervals if end - start >= 100]


def voiced_ranges(audio, padding_ms):
    from faster_whisper.vad import get_speech_timestamps

    try:
        speech = get_speech_timestamps(audio, threshold=0.4, min_speech_duration_ms=150,
                                       min_silence_duration_ms=500, speech_pad_ms=padding_ms,
                                       max_speech_duration_s=23)
        return [(item['start'], item['end']) for item in speech], 'silero'
    except (ImportError, OSError, RuntimeError) as error:
        logger.info('Silero unavailable (%s); using speech energy.', type(error).__name__)
        return [(max(0, (start - padding_ms) * 16), min(audio.size, (end + padding_ms) * 16))
                for start, end in speech_energy(audio)[1]], 'energy'


@lru_cache(maxsize=1)
def speech_model(name):
    device = os.getenv('SUBTITLE_MODEL_DEVICE', 'cpu')
    if device == 'cpu':
        from pywhispercpp.model import Model

        return Model(name, models_dir=str(MODEL_DIRECTORY), n_threads=cpu_thread_count(), language='auto', print_progress=False)

    from faster_whisper import WhisperModel

    return WhisperModel(
        name,
        device=device,
        cpu_threads=cpu_thread_count(),
        compute_type='float16',
        download_root=str(MODEL_DIRECTORY),
    )


def split_cue(start, end, text, *, max_cpl=42, max_duration=7.0, max_cps=20):
    words = text.split()
    duration = end - start
    if not words or duration <= 0:
        return []
    char_count = len(text.replace(' ', ''))
    if duration <= max_duration and (char_count / duration if duration else 0) <= max_cps:
        return [(start, end, text)]

    groups = []
    current = []
    for word in words:
        candidate = ' '.join(current + [word])
        if current and len(candidate) > max_cpl:
            groups.append(current)
            current = [word]
        else:
            current.append(word)
    if current:
        groups.append(current)

    if len(groups) == 1:
        return [(start, end, text)]

    total_chars = sum(len(' '.join(group).replace(' ', '')) for group in groups) or 1
    cursor = start
    split_cues = []
    for index, group in enumerate(groups):
        if index == len(groups) - 1:
            cue_end = end
        else:
            cue_chars = len(' '.join(group).replace(' ', ''))
            cue_end = min(end, cursor + max(0.9, duration * cue_chars / total_chars))
            cue_end = min(cue_end, cursor + max_duration)
        cue_text = ' '.join(group).strip()
        if cue_text and cue_end > cursor:
            split_cues.append((cursor, cue_end, cue_text))
        cursor = cue_end
    return split_cues


def clean_cues(segments, duration=None):
    cues = []
    for start, end, text in segments:
        start = max(0, start)
        end = min(end, duration) if duration is not None else end
        words = text.split()
        normalized = [word.casefold().strip('.,!?…') for word in words]
        # A multiword phrase repeated three or more times in a single cue is a common decoding loop.
        for size in range(3, len(words) // 3 + 1):
            if len(words) % size == 0 and normalized == normalized[:size] * (len(words) // size):
                words = words[:size]
                break
        text = ' '.join(words)
        if not text or end <= start:
            continue
        for cue_start, cue_end, cue_text in split_cue(start, end, text):
            cue_words = cue_text.split()
            if cues:
                previous_start, previous_end, previous_text = cues[-1]
                is_echo = cue_start <= previous_end + 0.12 and len(cue_words) >= 4
                if (cue_start < previous_end or is_echo) and previous_text.casefold() == cue_text.casefold():
                    cues[-1] = (previous_start, max(previous_end, cue_end), previous_text)
                    continue
                cue_start = max(cue_start, previous_end)
            if cue_end > cue_start:
                cues.append((cue_start, cue_end, cue_text))
    return cues


def transcribe(path, language=None, accurate=False, *, model_name=None):
    name = model_name or ('small' if accurate else os.getenv('SUBTITLE_WHISPER_MODEL', 'base'))
    model = speech_model(name)
    if os.getenv('SUBTITLE_MODEL_DEVICE', 'cpu') == 'cpu':
        from faster_whisper.audio import decode_audio

        audio = decode_audio(str(path))
        if not audio.size:
            return [], language
        if language is None:
            if audio.size < 16000:
                language = 'auto'
            else:
                (language, _), _ = model.auto_detect_language(audio)
        segments = model.transcribe(
            audio, language=language, no_context=True, suppress_non_speech_tokens=True,
            token_timestamps=True, split_on_word=True, max_len=84,
        )
        return clean_cues(((s.t0 / 100, s.t1 / 100, s.text) for s in segments), audio.size / 16000), language

    segments, info = model.transcribe(str(path), language=language, vad_filter=True,
                                      condition_on_previous_text=False, repetition_penalty=1.12)
    return clean_cues(((s.start, s.end, s.text) for s in segments), info.duration), info.language


@lru_cache(maxsize=2)
def aligned_speech_model(name, device, cached_only, *, vietnamese_profile=False):
    if device == 'cpu':
        from .local_whisper import LocalWhisper

        return LocalWhisper(name, MODEL_DIRECTORY, cpu_thread_count(), vietnamese_profile=vietnamese_profile)
    from faster_whisper import WhisperModel

    return WhisperModel(name, device=device, cpu_threads=cpu_thread_count(),
                        compute_type='int8' if device == 'cpu' else 'float16',
                        download_root=str(MODEL_DIRECTORY), local_files_only=cached_only)


def transcribe_aligned(path, language=None, accurate=False, *, model_name=None, retry=True):
    from faster_whisper.audio import decode_audio
    from .whisper_decode import recognize_whisper

    baseline = os.getenv('SUBTITLE_WHISPER_MODEL', 'base')
    selected = model_name or ('small' if accurate else baseline)
    device = os.getenv('SUBTITLE_MODEL_DEVICE', 'cpu')
    checkpoint = local_vietnamese_asr() if not model_name and (not language or vietnamese(language)) else None
    audio = decode_audio(str(path)) if checkpoint else None
    if audio is not None and audio.size:
        try:
            if language or aligned_speech_model(baseline, device, False).likely_vietnamese(audio):
                model = aligned_speech_model(checkpoint, device, True, vietnamese_profile=True)
                result = recognize_whisper(model, audio, 'vi', first_beam=5 if accurate else 3, retry=retry)
                if result['segments']:
                    return result | {'model_name': VIETNAMESE_ASR_NAME}
        except (OSError, RuntimeError, ValueError) as error:
            logger.warning('Local Vietnamese ASR unavailable (%s); using multilingual Whisper.', type(error).__name__)
    if vietnamese(language):
        language = 'vi'
    if selected in LOCAL_WHISPER_MODELS or (model_name and device == 'cpu'):
        try:
            model = aligned_speech_model(selected, device, selected != baseline)
        except OSError:
            if model_name or selected == baseline:
                model = None
            else:
                selected = baseline
                model = aligned_speech_model(selected, device, False)
        if model is not None:
            result = recognize_whisper(model, audio if audio is not None else decode_audio(str(path)), language,
                                       first_beam=4 if accurate else 3, retry=retry)
            return result | {'model_name': selected}
    cues, detected = transcribe(path, language, accurate, model_name=selected)
    return {'language': detected or 'und', 'model_name': Path(selected).name,
            'segments': [{'start_ms': round(start * 1000), 'end_ms': round(end * 1000), 'content': text}
                         for start, end, text in cues if round(end * 1000) > round(start * 1000)]}
