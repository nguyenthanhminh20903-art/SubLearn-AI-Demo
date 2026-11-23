import json
import unittest
from types import SimpleNamespace

from app.context_translation import translate_with_context
from app.subtitle_algorithms import TimedText, optimize_transcript
from app.subtitle_import import read_subtitles
from app.whisper_decode import recognize_whisper


class SubtitlePipelineTests(unittest.TestCase):
    def test_retry_replaces_only_uncertain_audio_and_preserves_offset(self):
        calls = []

        def segment(text, start, end, confidence):
            return SimpleNamespace(text=text, start=start, end=end, compression_ratio=1.0, avg_logprob=-1,
                                   words=[SimpleNamespace(word=text, start=start, end=end, probability=confidence)])

        def transcribe(audio, **options):
            calls.append((len(audio), options['beam_size']))
            if options['beam_size'] == 3:
                rows = [segment('Hello', 0, .4, .95), segment('maybe', 1.0, 2.5, .3), segment('Bye', 3, 4, .98)]
            else:
                rows = [segment('better', .8, 2.1, .9)]
            return rows, SimpleNamespace(language='en')

        result = recognize_whisper(SimpleNamespace(transcribe=transcribe), bytes(80000))
        self.assertEqual(len(calls), 2)
        self.assertLess(calls[1][0], calls[0][0])
        self.assertEqual([row['content'] for row in result['segments']], ['Hello', 'better', 'Bye'])
        self.assertEqual(result['words'][1]['start_ms'], 1100)
        self.assertEqual(result['replaced_windows'], 1)

    def test_optimization_keeps_unaligned_content_and_non_overlapping_cues(self):
        raw = [TimedText(0, 900, 'Xin chào', '1', .4), TimedText(850, 2100, 'Sau đây', '2')]
        words = [TimedText(0, 440, 'Xin', '1', .4), TimedText(440, 900, 'chào', '1', .6)]
        cues = optimize_transcript(raw, words, 'vi')
        self.assertEqual([cue.text for cue, _ in cues], ['Xin chào', 'Sau đây'])
        self.assertEqual(cues[0][0].confidence, .5)
        self.assertEqual(len(cues[0][1]), 2)
        self.assertGreaterEqual(cues[1][0].start_ms, cues[0][0].end_ms)

    def test_import_preserves_timing_and_strips_vtt_markup(self):
        vtt = b'WEBVTT\n\nNOTE sample\n\nsubtitle-1\n00:01.100 --> 00:02.340 line:90%\n<i>Hello</i> &amp; bye\n'
        self.assertEqual(read_subtitles(vtt, '.vtt'), [(1100, 2340, 'Hello & bye')])
        with self.assertRaises(ValueError):
            read_subtitles(b'1\n00:00:02,000 --> 00:00:01,000\nHello', '.srt')

    def test_incomplete_word_timings_keep_valid_words_without_losing_text(self):
        cue = TimedText(100, 2100, 'We save this video.', '1', .7)
        words = [TimedText(820, 1210, 'save', '1', .9), TimedText(1800, 2200, 'video.', '1', .8)]
        [(caption, aligned)] = optimize_transcript([cue], words, 'en')
        self.assertEqual(caption.text, cue.text)
        self.assertEqual([(word.text, word.end_ms) for word in aligned], [('save', 1210), ('video.', 2100)])

    def test_context_sends_neighbors_and_retries_missing_glossary(self):
        rows = [{'id': str(index), 'text': text} for index, text in enumerate(('Previous', 'Term', 'Following'))]
        attempts = []

        def output(prompt):
            self.assertEqual(prompt['translate_ids'], ['1'])
            self.assertEqual([row['id'] for row in prompt['segments']], ['0', '1', '2'])
            attempts.append(prompt.copy())
            text = 'Exact phrase' if len(attempts) == 2 else 'Approximate phrase'
            return json.dumps({'translations': [{'id': '1', 'text': text}]})

        translated = translate_with_context(rows, 'vi', {'Term': 'Exact phrase'}, output, selected={'1'})
        self.assertEqual(translated, ['', 'Exact phrase', ''])
        self.assertEqual(len(attempts), 2)
