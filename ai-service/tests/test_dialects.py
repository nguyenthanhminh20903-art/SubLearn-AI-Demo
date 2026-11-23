import unittest
from pathlib import Path
from unittest.mock import patch

from app.dialects import agree_on_region, confident_region, dialect_windows, region_for_range, region_label
from app.media import transcribe_aligned
from app.subtitle_algorithms import TimedText, normalize, optimize_transcript
from evaluate_asr import evaluate


class DialectTests(unittest.TestCase):
    def test_checkpoint_and_training_label_aliases(self):
        self.assertEqual([region_label(value) for value in ('north', 'CENTRAL', 'Nam')],
                         ['NORTHERN', 'CENTRAL', 'SOUTHERN'])
        self.assertIsNone(region_label('LABEL_0'))

    def test_keeps_three_labels_and_abstains_at_low_confidence(self):
        self.assertEqual(confident_region({'NORTHERN': .1, 'CENTRAL': .8, 'SOUTHERN': .1}), ('CENTRAL', .8))
        self.assertEqual(confident_region({'NORTHERN': .46, 'CENTRAL': .44, 'SOUTHERN': .1}), ('UNKNOWN', .46))

    def test_disagreed_high_scores_are_marked_unknown(self):
        self.assertEqual(agree_on_region(('NORTHERN', .99), [('NORTHERN', .95), ('CENTRAL', .99)]), ('UNKNOWN', .99))
        self.assertEqual(agree_on_region(('CENTRAL', .99), [('CENTRAL', .96), ('CENTRAL', .92)]), ('CENTRAL', .92))

    def test_windowing_respects_speaker_boundaries_and_minimum(self):
        spans = [{'start_ms': 0, 'end_ms': 10100, 'speaker': 'A'},
                 {'start_ms': 10100, 'end_ms': 11800, 'speaker': 'B'},
                 {'start_ms': 11800, 'end_ms': 12100, 'speaker': 'A'}]
        windows = dialect_windows(spans)
        self.assertEqual([window['speaker'] for window in windows], ['A', 'A', 'A', 'B'])
        self.assertTrue(all(1400 <= window['end_ms'] - window['start_ms'] <= 5000 for window in windows))
        self.assertEqual(windows[2]['end_ms'], windows[3]['start_ms'])
        self.assertEqual(dialect_windows(spans, keep_short=True)[-1]['end_ms'], 12100)

    def test_region_is_based_on_cue_time_even_without_a_speaker_label(self):
        spans = [{'start_ms': 0, 'end_ms': 2000, 'dialect_label': 'NORTHERN', 'dialect_confidence': .8},
                 {'start_ms': 2000, 'end_ms': 4000, 'dialect_label': 'CENTRAL', 'dialect_confidence': .9}]
        ends = [2000, 4000]
        self.assertEqual(region_for_range(spans, ends, 2020, 3500), ('CENTRAL', .9))
        self.assertEqual(region_for_range(spans, ends, 1000, 3000), ('UNKNOWN', None))
        self.assertEqual(region_for_range(spans, ends, 4100, 4800), ('UNKNOWN', None))

    def test_asr_evaluation_keeps_count_and_wer_by_region(self):
        manifest = [dict(id='a', dialect_label='north', reference='xin chào', duration_seconds=2),
                    dict(id='b', dialect_label='central', reference='xin chào', duration_seconds=3)]
        predictions = {'a': {'text': 'xin chào', 'seconds': 1}, 'b': {'text': 'xin', 'seconds': 1}}
        result = evaluate(manifest, predictions)
        self.assertEqual(result['wer'], .25)
        self.assertEqual(result['by_dialect']['CENTRAL']['count'], 1)
        self.assertEqual(result['wer_central'], .5)

    def test_prefers_specialized_checkpoint_only_for_vietnamese(self):
        import numpy as np

        decoded = {'segments': [{'start_ms': 0, 'end_ms': 1000, 'content': 'test'}]}
        with patch('app.media.local_vietnamese_asr', return_value='pho.bin'), \
             patch('app.media.aligned_speech_model') as model, \
             patch('faster_whisper.audio.decode_audio', return_value=np.zeros(16000)), \
             patch('app.whisper_decode.recognize_whisper', return_value=decoded) as run:
            model.return_value.likely_vietnamese.return_value = False
            self.assertEqual(transcribe_aligned(Path('clip.wav'), 'vi-VN')['model_name'], 'PhoWhisper-small')
            self.assertEqual(run.call_args.args[2], 'vi')
            self.assertNotEqual(transcribe_aligned(Path('clip.wav'), 'en')['model_name'], 'PhoWhisper-small')
            self.assertNotEqual(transcribe_aligned(Path('clip.wav'))['model_name'], 'PhoWhisper-small')
            model.return_value.likely_vietnamese.return_value = True
            self.assertEqual(transcribe_aligned(Path('clip.wav'))['model_name'], 'PhoWhisper-small')

    def test_preserves_phowhisper_text_when_native_alignment_is_incomplete(self):
        text = 'Câu đầu có đầy đủ mốc giờ. Câu sau vẫn được lưu khi thiếu một số mốc giờ.'
        segment = TimedText(500, 9500, text, '1', .8)
        words = [TimedText(500, 650, 'Câu', '1', .9)]
        cues = optimize_transcript([segment], words, 'vi', split_untimed=True)
        self.assertGreater(len(cues), 1)
        self.assertEqual(normalize(' '.join(cue.text for cue, _ in cues)), normalize(text))
        self.assertTrue(all(not group and cue.end_ms - cue.start_ms <= 7000 for cue, group in cues))
        self.assertEqual((cues[0][0].start_ms, cues[-1][0].end_ms), (500, 9500))

    def test_language_evidence_combines_speech_windows_and_rejects_conflicting_languages(self):
        import numpy as np
        from unittest.mock import Mock
        from app.local_whisper import LocalWhisper

        recognizer = LocalWhisper.__new__(LocalWhisper)
        recognizer.model = Mock()
        recognizer.model.auto_detect_language.side_effect = [
            (('vi', .73), {}), (('vi', .92), {}),
            (('vi', .95), {}), (('en', .96), {}),
        ]
        with patch('app.local_whisper.voiced_ranges', return_value=([(0, 88000), (92000, 127200)], 'silero')):
            audio = np.zeros(136000, dtype=np.float32)
            self.assertTrue(recognizer.likely_vietnamese(audio))
            self.assertFalse(recognizer.likely_vietnamese(audio))
