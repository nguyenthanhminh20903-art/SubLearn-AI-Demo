from bisect import bisect_right
from collections import defaultdict
from math import ceil, isfinite
from unicodedata import normalize


REGIONS = ('NORTHERN', 'CENTRAL', 'SOUTHERN')
UNKNOWN = 'UNKNOWN'
MIN_WINDOW_MS = 1400
MAX_WINDOW_MS = 5000
MIN_CONFIDENCE = 0.72
MIN_MARGIN = 0.18


def region_label(value):
    label = normalize('NFC', value.strip()).upper()
    return {
        'NORTH': 'NORTHERN', 'NORTHERN': 'NORTHERN', 'BẮC': 'NORTHERN',
        'CENTRAL': 'CENTRAL', 'TRUNG': 'CENTRAL',
        'SOUTH': 'SOUTHERN', 'SOUTHERN': 'SOUTHERN', 'NAM': 'SOUTHERN',
    }.get(label)


def vietnamese(language):
    return bool(language and language.lower().split('-')[0] == 'vi')


def dialect_windows(spans, *, keep_short=False):
    """Build bounded windows within detected speech or existing speaker turns."""
    runs = []
    for span in sorted(spans, key=lambda item: (item['start_ms'], item['end_ms'])):
        start, end = span['start_ms'], span['end_ms']
        speaker = span.get('speaker')
        if end <= start:
            continue
        if runs:
            previous = runs[-1]
            if previous['speaker'] == speaker and start <= previous['end_ms'] + 360:
                previous['end_ms'] = max(previous['end_ms'], end)
                continue
            start = max(start, previous['end_ms'])
        if end > start:
            runs.append({'start_ms': start, 'end_ms': end, 'speaker': speaker})
    windows = []
    for run in runs:
        duration = run['end_ms'] - run['start_ms']
        if duration < MIN_WINDOW_MS and not keep_short:
            continue
        parts = ceil(duration / MAX_WINDOW_MS)
        start = run['start_ms']
        for index in range(parts):
            left, right = start + duration * index // parts, start + duration * (index + 1) // parts
            windows.append({**run, 'start_ms': left, 'end_ms': right})
    return windows[:20000]


def confident_region(values):
    if set(values) != set(REGIONS) or any(not isfinite(value) or not 0 <= value <= 1 for value in values.values()):
        return UNKNOWN, None
    (label, score), (_, second), _ = sorted(values.items(), key=lambda item: -item[1])
    return (label if score >= MIN_CONFIDENCE and score - second >= MIN_MARGIN else UNKNOWN), score


def agree_on_region(primary, parts):
    label, score = primary
    if any(candidate != label or confidence is None for candidate, confidence in parts):
        return UNKNOWN, score
    return label, min([score, *(confidence for _, confidence in parts)])


def region_for_range(spans, ends, start, end):
    if end <= start:
        return UNKNOWN, None
    votes = defaultdict(int)
    weighted = defaultdict(float)
    covered = 0
    for index in range(bisect_right(ends, start), len(spans)):
        span = spans[index]
        if span['start_ms'] >= end:
            break
        overlap = min(end, span['end_ms']) - max(start, span['start_ms'])
        if overlap <= 0:
            continue
        covered += overlap
        label = span['dialect_label']
        if label in REGIONS and span['dialect_confidence'] is not None:
            votes[label] += overlap
            weighted[label] += overlap * span['dialect_confidence']
    if not votes:
        return UNKNOWN, None
    label = max(votes, key=votes.get)
    if votes[label] < max((end - start) * 0.5, covered * 0.7):
        return UNKNOWN, None
    return label, weighted[label] / votes[label]
