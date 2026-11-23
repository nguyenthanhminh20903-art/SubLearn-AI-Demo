"""Claim and process jobs from an authorized Remote GPU environment."""

import argparse
import gc
import re
import shutil
import time
from getpass import getpass
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from urllib.parse import urlsplit

import requests
from faster_whisper import WhisperModel
from faster_whisper.audio import decode_audio
from app.worker_audio import speech_intervals
from app.whisper_decode import recognize_whisper


class RecognitionEngine:
    def __init__(self, multilingual_model, device, vram_mib, forced_alignment=False,
                 speaker_checkpoint=None, dialect_checkpoint=None):
        self.multilingual_model = multilingual_model
        self.device = device
        self.vram_mib = vram_mib
        self.forced_alignment = forced_alignment
        self.speaker_checkpoint = speaker_checkpoint
        self.dialect_checkpoint = dialect_checkpoint
        self.model = None
        self.name = None

    def load(self, name):
        if self.name == name:
            return self.model
        previous = self.name
        self.model = self.name = None
        gc.collect()
        if previous == 'vinai/PhoWhisper-large':
            import torch

            torch.cuda.empty_cache()
        if name == 'vinai/PhoWhisper-large':
            import torch
            from transformers import pipeline

            self.model = pipeline('automatic-speech-recognition', model=name, device=0, dtype=torch.float16,
                                  chunk_length_s=25)
        else:
            compute_type = 'float16' if self.device == 'cuda' else 'int8'
            self.model = WhisperModel(name, device=self.device, compute_type=compute_type)
        self.name = name
        return self.model

    def recognize(self, media, language, snr):
        result = self.transcribe(media, language, snr)
        if self.forced_alignment and result['segments']:
            try:
                aligned = self.align(media, result)
                if aligned:
                    result['words'] = aligned
                    result['alignment'] = 'whisperx_ctc'
            except (ImportError, RuntimeError, ValueError, OSError):
                print('Forced alignment unavailable; preserving ASR timestamps.')
        if result['segments']:
            if self.device == 'cuda' and (self.speaker_checkpoint or self.dialect_checkpoint):
                import torch

                self.model = self.name = None
                gc.collect()
                torch.cuda.empty_cache()
            result.update(speech_intervals(
                media, self.device, result['language'], self.speaker_checkpoint, self.dialect_checkpoint,
            ))
        return result

    def align(self, media, result):
        import torch
        import whisperx

        self.model = self.name = None
        gc.collect()
        if self.device == 'cuda':
            torch.cuda.empty_cache()
        alignment, metadata = whisperx.load_align_model(language_code=result['language'], device=self.device)
        segments = [
            {'start': row['start_ms'] / 1000, 'end': row['end_ms'] / 1000, 'text': row['content']}
            for row in result['segments']
        ]
        audio = decode_audio(str(media))
        response = whisperx.align(segments, alignment, metadata, audio, self.device,
                                  return_char_alignments=False)
        words = []
        for item in response.get('word_segments', []):
            if item.get('start') is not None and item.get('end') is not None and item['word'].strip():
                start, end = round(item['start'] * 1000), round(item['end'] * 1000)
                if end > start:
                    words.append({'start_ms': start, 'end_ms': end, 'content': item['word'].strip()})
        before = re.findall(r'\w+', ' '.join(row['text'] for row in segments).casefold())
        after = re.findall(r'\w+', ' '.join(row['content'] for row in words).casefold())
        if not before or before != after:
            return []
        original = result['words']
        if len(original) == len(words):
            for item, reference in zip(words, original):
                if item['content'].casefold() == reference['content'].casefold():
                    item['confidence'] = reference.get('confidence')
        return words

    def transcribe(self, media, language, snr):
        audio = decode_audio(str(media))
        noise = snr is not None and snr < 12
        if language is None and audio.size >= 16000:
            detected, probability, _ = self.load(self.multilingual_model).detect_language(
                audio=audio[:480000], vad_filter=True
            )
            language = detected if probability >= 0.8 else None
        if language == 'vi' and self.device == 'cuda' and self.vram_mib >= 8192:
            try:
                name = 'vinai/PhoWhisper-large'
                output = self.load(name)({'array': audio, 'sampling_rate': 16000}, return_timestamps='word')
                words = []
                for item in output.get('chunks', []):
                    start, end = item['timestamp']
                    if start is not None and end is not None and end > start and item['text'].strip():
                        words.append({'start_ms': round(start * 1000), 'end_ms': round(end * 1000),
                                      'content': item['text'].strip()})
                if output.get('text', '').strip() and words:
                    return {'language': 'vi', 'model_name': name, 'segments': words, 'words': words}
            except (ImportError, RuntimeError, ValueError):
                print('PhoWhisper unavailable; using multilingual Whisper.')
        return recognize_whisper(self.load(self.multilingual_model), audio, language,
                                 first_beam=4 if noise else 3) | {'model_name': self.multilingual_model}


def renew_lease(job_url, headers, stop):
    try:
        from app.resource_metrics import measure
    except ImportError:
        measure = None
    while not stop.wait(20):
        try:
            metrics = measure(Path.cwd(), gpu=True).model_dump() if measure else None
            response = requests.post(f'{job_url}/heartbeat', headers=headers, json=metrics, timeout=10)
            if response.status_code in (401, 404, 409):
                return
        except requests.RequestException:
            continue


def run_job(jobs_url, job_id, model, token, lease, language=None, snr=None):
    job_url = f'{jobs_url}/{job_id}'
    headers = {'Authorization': f'Bearer {token}', 'X-Worker-Lease': lease}
    stop = Event()
    heartbeat = Thread(target=renew_lease, args=(job_url, headers, stop), daemon=True)
    heartbeat.start()
    try:
        process_media(job_url, model, headers, language, snr)
    finally:
        stop.set()
        heartbeat.join(timeout=2)


def process_media(job_url, model, headers, language, snr):
    with requests.Session() as session, TemporaryDirectory() as directory:
        session.headers.update(headers)
        media = Path(directory) / 'input.wav'
        try:
            with session.get(f'{job_url}/media', stream=True, timeout=60) as response:
                response.raise_for_status()
                with media.open('wb') as output:
                    shutil.copyfileobj(response.raw, output)
            result = model.recognize(media, language, snr)
        except Exception as error:
            message = f'Remote GPU: {type(error).__name__}'
            session.post(f'{job_url}/error', json={'message': message}, timeout=30)
            raise
        session.post(f'{job_url}/heartbeat', timeout=15).raise_for_status()
        response = session.post(
            f'{job_url}/result', json=result, timeout=120,
        )
        response.raise_for_status()
        print(f'Completed {job_url.rsplit("/",1)[-1]}: {response.json()["status"]} ({len(result["segments"])} segments).')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('base_url')
    parser.add_argument('job_id', type=int, nargs='?', help='Omit to watch the queue.')
    parser.add_argument('--model', default='large-v3-turbo')
    parser.add_argument('--device', choices=('cpu', 'cuda'), default='cuda')
    parser.add_argument('--vram-mib', type=int, default=0, help='Available GPU memory for PhoWhisper routing.')
    parser.add_argument('--forced-alignment', action='store_true', help='Enable optional WhisperX CTC alignment.')
    parser.add_argument('--speaker-checkpoint', type=Path, help='Local pyannote community-1 checkpoint directory.')
    parser.add_argument('--dialect-checkpoint', type=Path, help='Local XLS-R dialect checkpoint directory.')
    options = parser.parse_args()
    url = urlsplit(options.base_url)
    if url.scheme != 'https' and url.hostname not in ('127.0.0.1', 'localhost'):
        parser.error('Use an HTTPS URL for the worker API.')
    for checkpoint in (options.speaker_checkpoint, options.dialect_checkpoint):
        if checkpoint is not None and not checkpoint.is_dir():
            parser.error(f'Checkpoint directory does not exist: {checkpoint}')
    token = getpass('Worker token: ')
    jobs_url = f'{options.base_url.rstrip("/")}/api/v1/worker/jobs'
    model = RecognitionEngine(options.model, options.device, options.vram_mib, options.forced_alignment,
                              options.speaker_checkpoint, options.dialect_checkpoint)
    print('Waiting for Remote GPU jobs. Press Ctrl+C to stop.')
    while True:
        response = requests.post(f'{jobs_url}/claim', params={'job_id': options.job_id},
                                 headers={'Authorization': f'Bearer {token}'}, timeout=30)
        response.raise_for_status()
        claim = response.json()
        if claim['job_id']:
            try:
                run_job(jobs_url, claim['job_id'], model, token, claim['lease'], claim.get('language'),
                        claim.get('snr_estimate_db'))
            except Exception as error:
                print(f"Job {claim['job_id']} failed: {error}")
                if options.job_id is not None:
                    raise
        if options.job_id is not None:
            break
        time.sleep(5)
