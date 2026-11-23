import re
import unicodedata
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class TimedText:
    start_ms: int
    end_ms: int
    text: str
    chunk: str | None = None
    confidence: float | None = None


def normalize(text):
    return ' '.join(re.findall(r'\w+', unicodedata.normalize('NFC', text).casefold()))


def levenshtein(left, right):
    if len(right) > len(left):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for index, character in enumerate(left, 1):
        row = [index]
        for column, other in enumerate(right, 1):
            row.append(min(row[-1] + 1, previous[column] + 1, previous[column - 1] + (character != other)))
        previous = row
    return previous[-1]


def temporal_iou(left, right):
    overlap = max(0, min(left.end_ms, right.end_ms) - max(left.start_ms, right.start_ms))
    span = max(left.end_ms, right.end_ms) - min(left.start_ms, right.start_ms)
    return overlap / span if span else 0.0


def deduplicate(cues, *, similarity=0.85, overlap=0.5):
    retained = []
    active = []
    for cue in sorted(cues, key=lambda item: (item.start_ms, item.end_ms)):
        text = normalize(cue.text)
        if cue.start_ms >= cue.end_ms or not text:
            continue
        match = None
        active = [index for index in active if retained[index].end_ms > cue.start_ms]
        for index in reversed(active):
            previous = retained[index]
            if temporal_iou(cue, previous) < overlap:
                continue
            other = normalize(previous.text)
            if text != other and (not cue.chunk or not previous.chunk or cue.chunk == previous.chunk):
                continue
            score = 1 - levenshtein(text, other) / max(len(text), len(other))
            if score >= similarity:
                match = index
                break
        if match is None:
            active.append(len(retained))
            retained.append(cue)
        elif (cue.confidence if cue.confidence is not None else -1) > (
            retained[match].confidence if retained[match].confidence is not None else -1
        ):
            retained[match] = cue
    return sorted(retained, key=lambda item: (item.start_ms, item.end_ms))


@dataclass(frozen=True)
class SegmentationCosts:
    max_cps: int = 20
    max_cpl: int = 42
    max_duration_ms: int = 7000
    min_duration_ms: int = 1000
    long_pause_ms: int = 650


def caption_costs(language):
    return SegmentationCosts(max_cps=17 if (language or '').split('-')[0] == 'vi' else 20)


def segment_words(words, costs=SegmentationCosts(), *, with_words=False):
    """Find the lowest-cost partition of ordered word timestamps."""
    words = list(words)
    if any(word.start_ms < 0 or word.end_ms <= word.start_ms or not word.text.strip() for word in words):
        raise ValueError('Invalid word timestamp or text')
    if any(left.start_ms > right.start_ms or left.end_ms > right.end_ms for left, right in zip(words, words[1:])):
        raise ValueError('Word timestamps must be ordered')
    best = [0.0] + [float('inf')] * len(words)
    predecessor = [0] * (len(words) + 1)
    for end in range(1, len(words) + 1):
        length = 0
        pause_cost = 0.0
        for start in range(end - 1, max(-1, end - 80), -1):
            duration = words[end - 1].end_ms - words[start].start_ms
            if start < end - 1 and (duration > costs.max_duration_ms
                                    or words[start + 1].start_ms - words[start].end_ms > costs.long_pause_ms):
                break
            length += len(words[start].text.strip()) + (start < end - 1)
            if start < end - 1:
                gap = words[start + 1].start_ms - words[start].end_ms
                pause_cost += max(0, gap - costs.long_pause_ms) / 200
            cps = length * 1000 / duration
            candidate = (
                best[start] + 1 + pause_cost + max(0, cps - costs.max_cps) / 3
                + max(0, length - costs.max_cpl) / 8
                + max(0, duration - costs.max_duration_ms) / 500
                + max(0, costs.min_duration_ms - duration) / 500
            )
            if end < len(words):
                if not words[end - 1].text.endswith(('.', '?', '!', '…', ',', ';', ':')):
                    candidate += 0.9
                if normalize(words[end - 1].text) in {'và', 'của', 'một', 'the', 'a', 'an', 'of', 'to', 'and'}:
                    candidate += 1.4
            if candidate < best[end]:
                best[end] = candidate
                predecessor[end] = start
    cues = []
    end = len(words)
    while end:
        start = predecessor[end]
        group = words[start:end]
        confidence = [word.confidence for word in group if word.confidence is not None]
        text = ' '.join(word.text.strip() for word in group)
        text = re.sub(r'\s+([,.;:?!…])', r'\1', text)
        cue = TimedText(group[0].start_ms, group[-1].end_ms, text,
                        confidence=sum(confidence) / len(confidence) if confidence else None)
        cues.append((cue, group) if with_words else cue)
        end = start
    return list(reversed(cues))


def clean_word_times(words, max_duration_ms):
    result = []
    for word in deduplicate(words):
        start = max(word.start_ms, result[-1].end_ms if result else 0)
        if word.end_ms > start:
            result.append(replace(word, start_ms=start, end_ms=min(word.end_ms, start + max_duration_ms)))
    return result


def keep_aligned_words(cue, words):
    source = iter(normalize(cue.text).split())
    matches = []
    for word in words:
        start, end = max(cue.start_ms, word.start_ms), min(cue.end_ms, word.end_ms)
        if start >= end:
            continue
        for token in normalize(word.text).split():
            if token not in source:
                return []
        matches.append(replace(word, start_ms=start, end_ms=end))
    return matches


def split_untimed_cue(cue, costs):
    tokens = cue.text.split()
    duration = cue.end_ms - cue.start_ms
    total = sum(map(len, tokens))
    estimated, used, start = [], 0, cue.start_ms
    for token in tokens:
        used += len(token)
        end = cue.start_ms + duration * used // total
        if end <= start:
            return [cue]
        estimated.append(TimedText(start, end, token, confidence=cue.confidence))
        start = end
    return segment_words(estimated, costs)


def optimize_transcript(segments, words, language, duration=None, *, split_untimed=False):
    costs = caption_costs(language)
    segments = deduplicate(segments)
    by_chunk = {}
    for word in words:
        by_chunk.setdefault(word.chunk, []).append(word)

    def clipped(items):
        return (replace(item, end_ms=min(item.end_ms, duration)) if duration else item for item in items
                if duration is None or item.start_ms < duration)

    def tokenize(items):
        return normalize(' '.join(item.text for item in items))

    if words and (set(by_chunk) == {None} or not segments or all(item.chunk is None for item in segments)):
        aligned = clean_word_times(clipped(words), costs.max_duration_ms)
        if aligned and (not segments or tokenize(aligned) == tokenize(segments)):
            return segment_words(aligned, costs, with_words=True)

    groups, pending = [], []
    for segment in clipped(segments):
        aligned = clean_word_times(clipped(by_chunk.get(segment.chunk, [])), costs.max_duration_ms)
        if aligned and tokenize(aligned) == normalize(segment.text):
            pending.extend(aligned)
            continue
        groups.extend(segment_words(clean_word_times(pending, costs.max_duration_ms), costs, with_words=True))
        pending.clear()
        if split_untimed and (segment.end_ms - segment.start_ms > costs.max_duration_ms
                              or len(segment.text) > costs.max_cpl * 2):
            # An unaligned decoder span can be subdivided for display without storing estimated word times.
            groups.extend((cue, []) for cue in split_untimed_cue(segment, costs))
        else:
            groups.append((segment, keep_aligned_words(segment, aligned)))
    groups.extend(segment_words(clean_word_times(pending, costs.max_duration_ms), costs, with_words=True))
    result = []
    for cue, group in sorted(groups, key=lambda item: item[0].start_ms):
        start = max(cue.start_ms, result[-1][0].end_ms if result else 0)
        if cue.end_ms <= start:
            continue
        if start != cue.start_ms:
            group = [replace(word, start_ms=max(word.start_ms, start)) for word in group if word.end_ms > start]
        result.append((replace(cue, start_ms=start), group))
    return result
