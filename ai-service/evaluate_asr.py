"""Measure WER, CER and RTF for a versioned ASR test manifest."""

import argparse
import json
import os
import platform
from pathlib import Path
from time import perf_counter

from app.subtitle_algorithms import levenshtein, normalize
from app.dialects import region_label


def evaluate(manifest, predictions):
    samples = []
    word_errors = word_count = char_errors = char_count = 0
    duration = inference = 0.0
    regional = {}
    for item in manifest:
        identifier = item['id']
        estimate = predictions[identifier]
        reference = normalize(item['reference'])
        predicted = normalize(estimate['text'])
        words, actual_words = reference.split(), predicted.split()
        characters, actual_characters = reference.replace(' ', ''), predicted.replace(' ', '')
        seconds = float(item['duration_seconds'])
        elapsed = float(estimate['seconds'])
        if seconds <= 0 or elapsed < 0 or not words:
            raise ValueError(f'Invalid reference, duration or runtime for {identifier}')
        we = levenshtein(words, actual_words)
        ce = levenshtein(characters, actual_characters)
        word_errors += we
        word_count += len(words)
        char_errors += ce
        char_count += len(characters)
        duration += seconds
        inference += elapsed
        dialect = region_label(item['dialect_label']) if item.get('dialect_label') else None
        if dialect:
            group = regional.setdefault(dialect, {'count': 0, 'word_errors': 0, 'words': 0,
                                                   'char_errors': 0, 'characters': 0})
            group['count'] += 1
            group['word_errors'] += we
            group['words'] += len(words)
            group['char_errors'] += ce
            group['characters'] += len(characters)
        samples.append({'id': identifier, 'wer': we / len(words), 'cer': ce / len(characters),
                        'rtf': elapsed / seconds, 'duration_seconds': seconds, 'seconds': elapsed})
    metrics = {'count': len(samples), 'wer': word_errors / word_count, 'cer': char_errors / char_count,
               'rtf': inference / duration, 'samples': samples}
    if regional:
        metrics['by_dialect'] = {}
        for label, group in regional.items():
            scores = {'count': group['count'], 'wer': group['word_errors'] / group['words'],
                      'cer': group['char_errors'] / group['characters']}
            metrics['by_dialect'][label] = scores
            metrics[f'wer_{label.lower()}'] = scores['wer']
            metrics[f'cer_{label.lower()}'] = scores['cer']
    return metrics


def transcribe_manifest(samples, root, profile):
    from app.media import (VIETNAMESE_ASR_NAME, aligned_speech_model, local_vietnamese_asr, speech_model,
                           transcribe, transcribe_aligned)
    from app.subtitle_algorithms import TimedText, optimize_transcript

    model = 'small' if profile == 'accurate' else os.getenv('SUBTITLE_WHISPER_MODEL', 'base')
    vietnamese_asr = profile == 'vietnamese'
    aligned = vietnamese_asr or profile in ('single-pass', 'two-pass')
    started = perf_counter()
    if vietnamese_asr:
        checkpoint = local_vietnamese_asr()
        if not checkpoint:
            raise RuntimeError('PhoWhisper CPU checkpoint is missing.')
        aligned_speech_model(checkpoint, 'cpu', True, vietnamese_profile=True)
        model = VIETNAMESE_ASR_NAME
    elif aligned:
        aligned_speech_model(model, os.getenv('SUBTITLE_MODEL_DEVICE', 'cpu'), False)
    else:
        speech_model(model)
    load_seconds = perf_counter() - started
    predictions = {}
    for example in samples:
        start = perf_counter()
        path = root / example['audio']
        if aligned:
            result = transcribe_aligned(path, example.get('language'), model_name=None if vietnamese_asr else model,
                                        accurate=vietnamese_asr,
                                        retry=profile != 'single-pass')
            timing = [[TimedText(row['start_ms'], row['end_ms'], row['content'], row.get('chunk'),
                                 row.get('confidence')) for row in result.get(key, [])] for key in ('segments', 'words')]
            cues = optimize_transcript(*timing, result['language'],
                                       split_untimed=result['model_name'] == VIETNAMESE_ASR_NAME)
            estimate = {'text': ' '.join(cue.text for cue, _ in cues),
                        'model': result['model_name'], 'cue_count': len(cues),
                        'aligned_words': sum(len(words) for _, words in cues),
                        'retry_windows': result.get('retry_windows', 0), 'replaced_windows': result.get('replaced_windows', 0)}
        else:
            cues, _ = transcribe(path, example.get('language'), profile == 'accurate')
            estimate = {'text': ' '.join(cue[2] for cue in cues)}
        predictions[example['id']] = estimate | {'seconds': perf_counter() - start}
    return predictions, {'profile': profile, 'model': model, 'load_seconds': load_seconds,
                         'platform': platform.platform(), 'processor': platform.processor()}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--profile', choices=('baseline', 'accurate', 'single-pass', 'two-pass', 'vietnamese'))
    source.add_argument('--predictions', type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
    samples = manifest['samples']
    identifiers = {sample['id'] for sample in samples}
    if not samples or len(identifiers) != len(samples):
        parser.error('Manifest must contain samples with unique IDs.')
    if args.predictions:
        run = json.loads(args.predictions.read_text(encoding='utf-8'))
        predictions, configuration = run['predictions'], run['configuration']
    else:
        predictions, configuration = transcribe_manifest(samples, args.manifest.parent, args.profile)
    if set(predictions) != identifiers:
        parser.error('Prediction IDs must exactly match the test manifest.')
    print(json.dumps({'dataset': manifest['dataset'], 'dataset_version': manifest['version'],
                      'configuration': configuration, 'metrics': evaluate(samples, predictions),
                      'predictions': predictions}, ensure_ascii=True, indent=2))
