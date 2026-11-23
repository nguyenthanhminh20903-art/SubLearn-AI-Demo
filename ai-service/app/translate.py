import json
import subprocess
import sys
from pathlib import Path
from threading import Lock
from functools import lru_cache

from .media import MODEL_DIRECTORY


PAIRS = {('vi', 'en'): '541dfd7', ('en', 'vi'): '30bcd46'}
translation_lock = Lock()


@lru_cache(maxsize=256)
def _translate_cached(texts, source, target, use_beam, glossary_items):
    command = [sys.executable, '-X', 'faulthandler', '-m', 'app.translate', source, target]
    if use_beam:
        command.append('--beam')
    with translation_lock:
        result = subprocess.run(
            command,
            input=json.dumps(list(texts)), capture_output=True, encoding='utf-8', timeout=600,
            cwd=MODEL_DIRECTORY.parent.parent, check=False,
        )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f'Translation worker exited with {result.returncode}')
    translations = json.loads(result.stdout)
    if len(translations) != len(texts):
        raise ValueError('Incomplete translation')

    glossary = dict(glossary_items)
    corrected = []
    for source_text, translated_text in zip(texts, translations, strict=True):
        value = translated_text.strip()
        source_casefold = source_text.casefold()
        for source_term, target_term in sorted(glossary.items(), key=lambda item: len(item[0]), reverse=True):
            if source_term.casefold() in source_casefold:
                value = value.replace(source_term, target_term)
                value = value.replace(source_term.casefold(), target_term)
        corrected.append(value)
    return tuple(corrected)


def translate_batch(texts, source, target, *, use_beam=False, glossary=None):
    glossary_items = tuple(sorted((str(key).strip(), str(value).strip())
                                  for key, value in (glossary or {}).items()
                                  if str(key).strip() and str(value).strip()))
    result = _translate_cached(tuple(texts), source, target, use_beam, glossary_items)
    return list(result)


def token_blocks(pieces, limit=96):
    start = 0
    while len(pieces) - start > limit:
        boundary = start + limit
        for position in range(boundary, boundary - 16, -1):
            if pieces[position].startswith('▁'):
                boundary = position
                break
        yield pieces[start:boundary]
        start = boundary
    if start < len(pieces):
        yield pieces[start:]


def run(texts, source, target, *, use_beam=False):
    from huggingface_hub import hf_hub_download
    from huggingface_hub.errors import LocalEntryNotFoundError
    from sentencepiece import SentencePieceProcessor

    revision = PAIRS[source, target]
    repository = f'Xenova/opus-mt-{source}-{target}'

    def model_file(name):
        options = {'revision': revision, 'cache_dir': MODEL_DIRECTORY / 'translation'}
        try:
            return hf_hub_download(repository, name, local_files_only=True, **options)
        except LocalEntryNotFoundError:
            return hf_hub_download(repository, name, **options)

    config = json.loads(Path(model_file('config.json')).read_text(encoding='utf-8'))
    vocabulary = json.loads(Path(model_file('vocab.json')).read_text(encoding='utf-8'))
    pieces_by_id = {token_id: piece for piece, token_id in vocabulary.items()}
    read_tokens = SentencePieceProcessor(model_file=model_file('source.spm'))
    write_tokens = SentencePieceProcessor(model_file=model_file('target.spm'))
    pad, eos = config['pad_token_id'], config['eos_token_id']
    unknown = vocabulary['<unk>']
    prefix = [vocabulary['>>vie<<']] if target == 'vi' else []
    inputs, cue_indexes = [], []
    for index, text in enumerate(texts):
        for pieces in token_blocks(read_tokens.encode(text.strip(), out_type=str)):
            if pieces[:1] == ['>>vie<<'] and prefix:
                pieces = pieces[1:]
            inputs.append(prefix + [vocabulary.get(piece, unknown) for piece in pieces] + [eos])
            cue_indexes.append(index)
    if not inputs:
        return [''] * len(texts)

    files = {kind: model_file(f'onnx/{kind}_model_quantized.onnx') for kind in ('encoder', 'decoder')}
    worker = MODEL_DIRECTORY.parents[2] / 'frontend' / 'translation-worker.mjs'
    result = subprocess.run(
        ['node', '--no-wasm-tier-up', str(worker)], input=json.dumps({
            'files': files, 'inputs': inputs, 'config': config,
            'beams': config['num_beams'] if use_beam else 1,
        }),
        capture_output=True, encoding='utf-8', timeout=540,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or 'Translation runtime failed')
    translations = [[] for _ in texts]
    for index, tokens in zip(cue_indexes, json.loads(result.stdout), strict=True):
        pieces = [pieces_by_id.get(token, '<unk>') for token in tokens if token not in (eos, pad)]
        text = write_tokens.decode(pieces).strip()
        if text:
            translations[index].append(text)
    return [' '.join(parts) for parts in translations]


if __name__ == '__main__':
    print(json.dumps(run(json.load(sys.stdin), *sys.argv[1:3], use_beam='--beam' in sys.argv[3:])))
