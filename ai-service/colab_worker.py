import argparse
import json
import os
import time
import threading
from pathlib import Path
from tempfile import TemporaryDirectory

import requests
from faster_whisper import WhisperModel
from faster_whisper.audio import decode_audio
from app.whisper_decode import recognize_whisper
from app.worker_audio import speech_intervals


WORKER_URL = os.getenv("SUBLEARN_WORKER_URL", "").rstrip("/")
WORKER_TOKEN = os.getenv("SUBLEARN_WORKER_TOKEN", "")
ASR_MODEL = os.getenv("SUBLEARN_ASR_MODEL", "large-v3-turbo")
COMPUTE_TYPE = os.getenv("SUBLEARN_COMPUTE_TYPE", "float16")
POLL_SECONDS = float(os.getenv("SUBLEARN_POLL_SECONDS", "3"))

HEADERS = {"Authorization": f"Bearer {WORKER_TOKEN}"}


def claim_job():
    response = requests.post(
        f"{WORKER_URL}/api/v1/worker/jobs/claim",
        headers=HEADERS,
        json={},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def lease_headers(lease):
    return {
        "Authorization": f"Bearer {WORKER_TOKEN}",
        "X-Worker-Lease": lease,
    }


def heartbeat(job_id, lease, stop_event):
    headers = lease_headers(lease)
    while not stop_event.wait(20):
        try:
            response = requests.post(
                f"{WORKER_URL}/api/v1/worker/jobs/{job_id}/heartbeat",
                headers=headers,
                timeout=30,
            )
            if response.status_code in (401, 404, 409):
                return
        except requests.RequestException:
            continue


def download_media(job_id, lease, destination):
    response = requests.get(
        f"{WORKER_URL}/api/v1/worker/jobs/{job_id}/media",
        headers=lease_headers(lease),
        timeout=300,
    )
    response.raise_for_status()
    destination.write_bytes(response.content)


def send_error(job_id, lease, message):
    try:
        requests.post(
            f"{WORKER_URL}/api/v1/worker/jobs/{job_id}/error",
            headers=lease_headers(lease),
            json={"message": message[:2000]},
            timeout=30,
        )
    except requests.RequestException:
        pass


def transcribe_audio(model, audio_path, language, dialect_checkpoint=None):
    result = recognize_whisper(model, decode_audio(str(audio_path)), language) | {"model_name": ASR_MODEL}
    if dialect_checkpoint and result['segments']:
        result.update(speech_intervals(audio_path, 'cuda', result['language'], dialect_checkpoint=dialect_checkpoint))
    return result


def main():
    parser = argparse.ArgumentParser()
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument('--audio', type=Path, help='Process one research sample and write JSON.')
    choice.add_argument('--worker', action='store_true', help='Poll the queue on an authorized GPU host.')
    parser.add_argument('--output', type=Path, default=Path('asr-results.json'))
    parser.add_argument('--language')
    parser.add_argument('--dialect-checkpoint', type=Path)
    options = parser.parse_args()
    if options.worker and (not WORKER_URL.startswith('https://') or not WORKER_TOKEN):
        parser.error('Set an HTTPS SUBLEARN_WORKER_URL and SUBLEARN_WORKER_TOKEN.')
    model = WhisperModel(ASR_MODEL, device="cuda", compute_type=COMPUTE_TYPE)
    if options.audio:
        result = transcribe_audio(model, options.audio, options.language, options.dialect_checkpoint)
        options.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        return
    print(f"Colab worker ready: {ASR_MODEL}")

    while True:
        try:
            job = claim_job()
        except requests.RequestException as error:
            print(f"Claim failed: {error}")
            time.sleep(POLL_SECONDS)
            continue

        job_id = job.get("job_id")
        if not job_id:
            time.sleep(POLL_SECONDS)
            continue

        lease = job["lease"]
        language = job.get("language")
        stop_event = threading.Event()
        heartbeat_thread = threading.Thread(
            target=heartbeat,
            args=(job_id, lease, stop_event),
            daemon=True,
        )
        heartbeat_thread.start()

        try:
            print(f"Processing job {job_id}")
            with TemporaryDirectory() as directory:
                audio_path = Path(directory) / 'normalized.wav'
                download_media(job_id, lease, audio_path)
                result = transcribe_audio(model, audio_path, language, options.dialect_checkpoint)
            requests.post(f'{WORKER_URL}/api/v1/worker/jobs/{job_id}/heartbeat',
                          headers=lease_headers(lease), timeout=15).raise_for_status()
            response = requests.post(
                f"{WORKER_URL}/api/v1/worker/jobs/{job_id}/result",
                headers=lease_headers(lease),
                json=result,
                timeout=300,
            )
            response.raise_for_status()
            print(f"Completed job {job_id}: {response.json()}")
        except Exception as error:
            print(f"Job {job_id} failed: {error}")
            send_error(job_id, lease, str(error))
        finally:
            stop_event.set()
            heartbeat_thread.join(timeout=2)


if __name__ == "__main__":
    main()
