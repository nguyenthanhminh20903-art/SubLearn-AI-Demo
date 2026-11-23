import html
import re


TIMESTAMP = re.compile(r'(?:(\d{2,}):)?([0-5]\d):([0-5]\d)[,.](\d{3})')
CUE_TIMING = re.compile(r'(\S+)\s+-->\s+(\S+)(?:\s+.*)?')
TAGS = re.compile(r'<(?:/?(?:b|i|u|c|v|lang|ruby|rt)\b[^>]*|[0-9][^>]*)>')
MAX_IMPORT_BYTES = 2 * 1024 * 1024


def timestamp(value):
    match = TIMESTAMP.fullmatch(value)
    if not match:
        raise ValueError('Mốc thời gian cần theo định dạng SRT hoặc WebVTT.')
    hours, minutes, seconds, ms = (int(part or 0) for part in match.groups())
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + ms


def read_subtitles(data, extension):
    try:
        text = data.decode('utf-16' if data.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig')
    except UnicodeError as error:
        raise ValueError('Lưu tệp phụ đề bằng UTF-8 hoặc UTF-16 rồi thử lại.') from error
    lines = text.splitlines()
    if extension == '.vtt':
        if not lines or not re.fullmatch(r'WEBVTT(?:[ \t].*)?', lines[0]):
            raise ValueError('Tệp VTT phải có dòng đầu WEBVTT.')
        boundary = next((index for index, line in enumerate(lines) if not line.strip()), len(lines))
        lines = lines[boundary:]
    rows = []
    for block in re.split(r'\n[ \t]*\n+', '\n'.join(lines).strip()):
        if not block.strip():
            continue
        parts = block.split('\n')
        first = parts[0].strip()
        if extension == '.vtt' and (first in {'STYLE', 'REGION', 'NOTE'} or first.startswith(('NOTE ', 'NOTE\t'))):
            continue
        position = 0 if '-->' in first else 1
        match = CUE_TIMING.fullmatch(parts[position].strip()) if len(parts) > position else None
        if not match:
            raise ValueError(f'Đoạn {len(rows) + 1} thiếu mốc thời gian hợp lệ.')
        start, end = map(timestamp, match.groups())
        content = html.unescape(TAGS.sub('', '\n'.join(parts[position + 1:]))).strip()
        if (end <= start or end > 10**12 or not content or '\x00' in content
                or len(content) > 16000 or len(rows) == 20000):
            raise ValueError(f'Đoạn {len(rows) + 1} có nội dung hoặc mốc thời gian không hợp lệ.')
        rows.append((start, end, content))
    if not rows:
        raise ValueError('Tệp không có đoạn phụ đề.')
    return sorted(rows, key=lambda row: row[:2])
