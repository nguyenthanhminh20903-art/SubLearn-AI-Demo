import math


VAD = {'min_silence_duration_ms': 400, 'speech_pad_ms': 180}
SAMPLE_RATE = 16000
LOW_CONFIDENCE = 0.55


def decode(model, audio, language, *, offset_ms=0, beam=3):
    stream, info = model.transcribe(
        audio, language=language, vad_filter=True, vad_parameters=VAD,
        word_timestamps=True, condition_on_previous_text=False,
        beam_size=beam, temperature=0, repetition_penalty=1.1,
        hallucination_silence_threshold=1.2,
    )
    rows = []
    for segment in stream:
        text = segment.text.strip()
        start, end = offset_ms + round(segment.start * 1000), offset_ms + round(segment.end * 1000)
        if not text or end <= start:
            continue
        words = []
        for word in segment.words or []:
            left, right = offset_ms + round(word.start * 1000), offset_ms + round(word.end * 1000)
            if word.word.strip() and right > left:
                words.append({'start_ms': left, 'end_ms': right, 'content': word.word.strip(),
                              'confidence': min(1.0, max(0.0, float(word.probability)))})
        confidence = (sum(word['confidence'] for word in words) / len(words) if words
                      else math.exp(min(0, segment.avg_logprob)))
        rows.append({'start_ms': start, 'end_ms': end, 'content': text, 'confidence': confidence,
                     'words': words, 'compression': segment.compression_ratio})
    return rows, info.language or language or 'und'


def retry_windows(rows):
    index = 0
    while index < len(rows):
        row = rows[index]
        if row['confidence'] >= LOW_CONFIDENCE and row['compression'] <= 2.4:
            index += 1
            continue
        start = index
        index += 1
        while index < len(rows):
            row = rows[index]
            if (row['confidence'] >= LOW_CONFIDENCE and row['compression'] <= 2.4
                    or row['start_ms'] - rows[index - 1]['end_ms'] > 1000
                    or row['end_ms'] - rows[start]['start_ms'] > 16000):
                break
            index += 1
        yield start, index


def score(rows):
    return sum(row['confidence'] - 0.12 * max(0, row['compression'] - 2.4) for row in rows) / len(rows)


def recognize_whisper(model, audio, language=None, *, retry=True, first_beam=3):
    first, language = decode(model, audio, language, beam=first_beam)
    result = []
    last = attempted = replaced = 0
    duration = round(len(audio) / SAMPLE_RATE * 1000)
    for start, end in retry_windows(first) if retry else ():
        original = first[start:end]
        left, right = original[0]['start_ms'], original[-1]['end_ms']
        window_start = max(0, left - 700)
        window_end = min(duration, right + 700)
        replacement, _ = decode(model, audio[window_start * 16:window_end * 16], language,
                                offset_ms=window_start, beam=max(8, first_beam + 2))
        attempted += 1
        # Padding gives the decoder context. Only keep hypotheses centered in the uncertain interval.
        replacement = [row for row in replacement if 2 * left <= row['start_ms'] + row['end_ms'] <= 2 * right
                       and row['start_ms'] >= (first[start - 1]['end_ms'] if start else 0)
                       and row['end_ms'] <= (first[end]['start_ms'] if end < len(first) else duration)]
        source_length = sum(len(row['content']) for row in original)
        better = (replacement and sum(len(row['content']) for row in replacement) >= source_length * 0.65
                  and score(replacement) >= score(original) + 0.04)
        result.extend(first[last:start])
        result.extend(replacement if better else original)
        replaced += bool(better)
        last = end
    result.extend(first[last:])
    segments, words = [], []
    for index, row in enumerate(result):
        chunk = str(index)
        segments.append({key: row[key] for key in ('start_ms', 'end_ms', 'content', 'confidence')} | {'chunk': chunk})
        words.extend(word | {'chunk': chunk} for word in row['words'])
    return {'segments': segments, 'words': words, 'language': language,
            'retry_windows': attempted, 'replaced_windows': replaced}
