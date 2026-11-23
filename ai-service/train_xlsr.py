"""Fine-tune XLS-R on a labeled Vietnamese dialect manifest in a GPU notebook."""

import argparse
import json
from collections import Counter
from pathlib import Path


LABELS = ('NORTHERN', 'CENTRAL', 'SOUTHERN')
SPLITS = ('train', 'dev', 'test')
MODEL = 'facebook/wav2vec2-xls-r-300m'


def split_rows(rows):
    groups = {split: [] for split in SPLITS}
    speakers = {}
    identifiers = set()
    for row in rows:
        split, label, speaker = row['split'], row['label'], row['speaker']
        if (split not in SPLITS or label not in LABELS or not speaker or row['id'] in identifiers
                or (speaker in speakers and speakers[speaker] != split)):
            raise ValueError('Manifest has an invalid label, duplicate ID, or speaker shared across splits.')
        speakers[speaker] = split
        identifiers.add(row['id'])
        groups[split].append(row)
    if any({row['label'] for row in groups[split]} != set(LABELS) for split in SPLITS):
        raise ValueError('Each split must contain all three dialect labels.')
    return groups


def classification_metrics(truth, predicted):
    matrix = [[0] * len(LABELS) for _ in LABELS]
    for expected, actual in zip(truth, predicted, strict=True):
        matrix[expected][actual] += 1
    scores = {}
    for i, label in enumerate(LABELS):
        tp = matrix[i][i]
        total = sum(matrix[i]) + sum(row[i] for row in matrix)
        scores[label] = 2 * tp / total if total else 0.0
    return {'macro_f1': sum(scores.values()) / len(LABELS), 'per_label_f1': scores,
            'label_order': LABELS, 'confusion_matrix': matrix}


def train(manifest_path, output, revision, epochs, batch_size, seed):
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    groups = split_rows(manifest['samples'])
    if output.exists():
        raise ValueError('Use a new output directory so prior runs are preserved.')
    import torch
    from faster_whisper.audio import decode_audio
    from torch.utils.data import DataLoader
    from transformers import AutoFeatureExtractor, Wav2Vec2ForSequenceClassification, set_seed

    if not torch.cuda.is_available():
        raise RuntimeError('Run fine-tuning in a GPU-enabled research notebook.')
    set_seed(seed)
    processor = AutoFeatureExtractor.from_pretrained(MODEL, revision=revision)
    labels = {label: index for index, label in enumerate(LABELS)}
    model = Wav2Vec2ForSequenceClassification.from_pretrained(
        MODEL, revision=revision, num_labels=len(labels), label2id=labels,
        id2label={number: label for label, number in labels.items()}, apply_spec_augment=False,
    ).to('cuda')
    model.wav2vec2.requires_grad_(False)
    for layer in model.wav2vec2.encoder.layers[-2:]:
        layer.requires_grad_(True)
    optimizer = torch.optim.AdamW((param for param in model.parameters() if param.requires_grad), lr=3e-5)

    def collate(batch):
        clips = [decode_audio(str(manifest_path.parent / item['audio']))[:8 * 16000] for item in batch]
        if any(clip.size < 1600 for clip in clips):
            raise ValueError('Every audio clip must contain at least 100 ms of decoded samples.')
        fields = processor(clips, sampling_rate=16000, padding=True, return_attention_mask=True,
                           return_tensors='pt')
        return fields, torch.tensor([labels[item['label']] for item in batch])

    def evaluate(rows):
        model.eval()
        truth, predictions = [], []
        with torch.inference_mode():
            for fields, target in DataLoader(rows, batch_size=batch_size, collate_fn=collate):
                scores = model(**fields.to('cuda')).logits
                truth.extend(target.tolist())
                predictions.extend(scores.argmax(dim=1).tolist())
        return classification_metrics(truth, predictions)

    history = []
    output.mkdir(parents=True)
    best = -1.0
    for epoch in range(epochs):
        model.train()
        for fields, target in DataLoader(groups['train'], batch_size=batch_size, shuffle=True, collate_fn=collate):
            optimizer.zero_grad(set_to_none=True)
            loss = model(**fields.to('cuda'), labels=target.to('cuda')).loss
            loss.backward()
            optimizer.step()
        result = evaluate(groups['dev'])
        history.append({'epoch': epoch + 1, **result})
        if result['macro_f1'] > best:
            best = result['macro_f1']
            model.save_pretrained(output / 'checkpoint')
            processor.save_pretrained(output / 'checkpoint')
    del optimizer, model
    torch.cuda.empty_cache()
    model = Wav2Vec2ForSequenceClassification.from_pretrained(output / 'checkpoint').to('cuda')
    truth = [labels[row['label']] for row in groups['test']]
    majority = Counter(row['label'] for row in groups['train']).most_common(1)[0][0]
    test = evaluate(groups['test'])
    report = {
        'dataset': manifest['dataset'], 'dataset_version': manifest['version'], 'model': MODEL,
        'model_revision': revision, 'gpu': torch.cuda.get_device_name(), 'seed': seed,
        'batch_size': batch_size, 'max_clip_seconds': 8, 'dev_history': history,
        'majority_baseline': classification_metrics(truth, [labels[majority]] * len(truth)),
        'test': test,
        'metrics': {'macro_f1': test['macro_f1'],
                    **{f'f1_{label.lower()}': value for label, value in test['per_label_f1'].items()}},
    }
    (output / 'metrics.json').write_text(json.dumps(report, indent=2), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--epochs', type=int, default=5)
    parser.add_argument('--batch-size', type=int, default=2)
    parser.add_argument('--seed', type=int, default=42)
    args = parser.parse_args()
    if args.epochs < 1 or args.batch_size < 1:
        parser.error('epochs and batch-size must be positive')
    train(args.manifest, args.output, args.revision, args.epochs, args.batch_size, args.seed)
