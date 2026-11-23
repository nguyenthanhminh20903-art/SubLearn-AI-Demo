"""Run a local Safetensors speech classifier for normalized Vietnamese audio."""

import argparse
import json
import sys
import wave
from pathlib import Path

from .dialects import MIN_WINDOW_MS, REGIONS, UNKNOWN, agree_on_region, confident_region, region_label


def classify_windows(audio, windows, checkpoint, device, threads=3):
    import numpy as np
    import torch
    from transformers import AutoFeatureExtractor, Wav2Vec2ForSequenceClassification

    if device == 'cpu':
        torch.set_num_threads(threads)
    processor = AutoFeatureExtractor.from_pretrained(checkpoint, local_files_only=True)
    model = Wav2Vec2ForSequenceClassification.from_pretrained(
        checkpoint, local_files_only=True, use_safetensors=True,
    ).to(device).eval()
    labels = [region_label(model.config.id2label[index]) for index in range(model.config.num_labels)]
    if len(labels) != len(REGIONS) or set(labels) != set(REGIONS):
        raise ValueError('Checkpoint must have exactly the three regional accent labels.')

    def predict(clips):
        fields = processor(clips, sampling_rate=16000, padding=True, return_tensors='pt')
        probabilities = model(**fields.to(device)).logits.softmax(dim=-1).tolist()
        return [confident_region(dict(zip(labels, row, strict=True))) for row in probabilities]

    results = []
    with torch.inference_mode():
        for window in windows:
            start, end = window['start_ms'] * 16, window['end_ms'] * 16
            clip = audio[start:end]
            label, score = UNKNOWN, None
            if len(clip) >= MIN_WINDOW_MS * 16 and float(np.sqrt(np.mean(clip * clip))) >= 0.003:
                label, score = predict([clip])[0]
                if label != UNKNOWN and len(clip) >= MIN_WINDOW_MS * 32:
                    halves = np.array_split(clip, 2)
                    if all(float(np.sqrt(np.mean(part * part))) >= 0.003 for part in halves):
                        label, score = agree_on_region((label, score), predict(halves))
                    else:
                        label = UNKNOWN
            results.append({**window, 'dialect': label, 'dialect_confidence': score})
    return results


def read_pcm(path):
    import numpy as np

    with wave.open(str(path), 'rb') as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 16000):
            raise ValueError('Dialect inference requires normalized mono 16 kHz PCM audio.')
        return np.frombuffer(audio.readframes(audio.getnframes()), dtype='<i2').astype(np.float32) / 32768


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('audio', type=Path)
    parser.add_argument('checkpoint', type=Path)
    parser.add_argument('--threads', type=int, default=3)
    options = parser.parse_args()
    windows = json.load(sys.stdin)
    audio = read_pcm(options.audio)
    print(json.dumps(classify_windows(audio, windows, options.checkpoint, 'cpu', options.threads), allow_nan=False))


if __name__ == '__main__':
    main()
