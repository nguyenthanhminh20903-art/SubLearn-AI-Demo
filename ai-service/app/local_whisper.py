import math
import zlib
from types import SimpleNamespace

import _pywhispercpp as native
from pywhispercpp.model import Model
from .media import speech_energy, voiced_ranges


class LocalWhisper:
    """Use native Whisper token timings on CPU and keep audio offsets through VAD."""

    def __init__(self, name, directory, threads, *, vietnamese_profile=False):
        self.vietnamese_profile = vietnamese_profile
        self.model = Model(name, models_dir=str(directory), n_threads=threads, params_sampling_strategy=1,
                           context_params={'use_gpu': False}, print_progress=False, print_realtime=False)

    def likely_vietnamese(self, audio):
        spans, _ = voiced_ranges(audio, padding_ms=120)
        spans = [(left, right) for left, right in spans if right - left >= 16000]
        weighted_confidence = sampled = 0
        if not spans:
            return False
        for index in sorted({0, len(spans) // 2, len(spans) - 1}):
            start, end = spans[index]
            clip = audio[start:min(end, start + 160000)]
            (language, probability), _ = self.model.auto_detect_language(clip)
            if language != 'vi' or probability < 0.55:
                return False
            weighted_confidence += probability * len(clip)
            sampled += len(clip)
        return bool(weighted_confidence >= 0.75 * sampled)

    def transcribe(self, audio, *, language, beam_size, vad_filter, vad_parameters, **_options):
        padding = vad_parameters['speech_pad_ms']
        if vad_filter and self.vietnamese_profile:
            ranges, _ = voiced_ranges(audio, padding_ms=max(250, padding))
        elif vad_filter:
            ranges = [(max(0, (start - padding) * 16), min(len(audio), (end + padding) * 16))
                      for start, end in speech_energy(audio)[1]]
        else:
            ranges = [(0, len(audio))]
        if not ranges:
            return [], SimpleNamespace(language=language or 'und')
        blocks = []
        for left, right in ranges:
            if blocks and (left < blocks[-1][1] or (left - blocks[-1][1] <= 9600 and right - blocks[-1][0] <= 400000)):
                blocks[-1] = (blocks[-1][0], right)
            else:
                blocks.append((left, right))
        if not language:
            start, end = max(blocks, key=lambda bounds: bounds[1] - bounds[0])
            if end - start >= 16000:
                (language, _), _ = self.model.auto_detect_language(audio[start:end])
        rows = []
        context = self.model._ctx
        for start, end in blocks:
            offset = start / 16000
            segments = self.model.transcribe(audio[start:end], language=language if language not in (None, 'und') else 'auto',
                                             no_context=True, no_timestamps=self.vietnamese_profile,
                                             token_timestamps=True, extract_probability=True,
                                             max_len=0 if self.vietnamese_profile else 84,
                                             split_on_word=True, suppress_non_speech_tokens=True,
                                             beam_search={'beam_size': beam_size, 'patience': 1.0})
            for index, segment in enumerate(segments):
                text = segment.text.strip()
                if not text:
                    continue
                raw = text.encode()
                rows.append(SimpleNamespace(
                    text=text, start=offset + segment.t0 / 100, end=min(end / 16000, offset + segment.t1 / 100),
                    words=token_words(context, index, offset, end / 16000),
                    compression_ratio=len(raw) / len(zlib.compress(raw)),
                    avg_logprob=math.log(max(1e-6, segment.probability)),
                ))
        return rows, SimpleNamespace(language=language or 'und')


def token_words(context, segment_index, offset, limit):
    words = []
    group = []
    stop_token = native.whisper_token_eot(context)
    for index in range(native.whisper_full_n_tokens(context, segment_index)):
        token = native.whisper_full_get_token_data(context, segment_index, index)
        if token.id >= stop_token:
            continue
        text = native.whisper_token_to_bytes(context, token.id)
        if text[:1].isspace() and group:
            append_word(words, group, offset, limit)
            group = []
        group.append((text, token))
    if group:
        append_word(words, group, offset, limit)
    return words


def append_word(words, group, offset, limit):
    word = b''.join(text for text, _ in group).decode('utf-8', errors='replace').strip()
    timed = [token for _, token in group if 0 <= token.t0 < token.t1]
    if word and timed:
        words.append(SimpleNamespace(word=word, start=offset + min(item.t0 for item in timed) / 100,
                                     end=min(limit, offset + max(item.t1 for item in timed) / 100),
                                     probability=sum(item.p for _, item in group) / len(group)))
