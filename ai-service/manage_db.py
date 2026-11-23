import argparse
import hashlib
import math
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import UUID, uuid4

from dotenv import load_dotenv
from fastapi import HTTPException

from app.mysql_db import mysql_transaction


ROOT = Path(__file__).resolve().parent
UPLOADS = ROOT / 'data' / 'uploads'


def migrate():
    # MySQL commits DDL implicitly. The migration statements are safe to resume.
    with mysql_transaction() as cursor:
        cursor.execute("SELECT GET_LOCK('subtitle_ai_db:sublearn:migrate',10) AS acquired")
        if cursor.fetchone()['acquired'] != 1:
            raise ValueError('Một tiến trình khác đang áp dụng migration.')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS SCHEMA_MIGRATION (
                version VARCHAR(100) PRIMARY KEY,
                checksum CHAR(64) NOT NULL,
                applied_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
            ) ENGINE=InnoDB
        ''')
        for script in sorted((ROOT / 'migrations').glob('*.sql')):
            content = script.read_bytes()
            checksum = hashlib.sha256(content).hexdigest()
            cursor.execute('SELECT checksum FROM SCHEMA_MIGRATION WHERE version=%s', (script.stem,))
            recorded = cursor.fetchone()
            if recorded is not None:
                if recorded['checksum'] != checksum:
                    raise ValueError(f'Migration {script.name} đã thay đổi sau khi áp dụng.')
                continue
            cursor.execute(content.decode('utf-8').strip())
            cursor.execute('INSERT INTO SCHEMA_MIGRATION (version, checksum) VALUES (%s,%s)',
                           (script.stem, checksum))
            print(f'Đã áp dụng {script.name}')


def source_media(identifier, directory):
    try:
        name = str(UUID(identifier))
    except ValueError:
        return None
    return next((path for path in directory.glob(f'{name}.*')
                 if path.is_file() and not path.is_symlink()), None)


def write_track(cursor, video_id, language, values, source_track_id=None):
    from app.mysql_api import save_revision

    cursor.execute('''INSERT INTO SUBTITLE_TRACK (video_id,source_track_id,language,track_type)
                      VALUES (%s,%s,%s,%s)''',
                   (video_id, source_track_id, language, 'TRANSLATION' if source_track_id else 'ORIGINAL'))
    track_id = cursor.lastrowid
    segment_ids = {}
    for sequence, start, end, text in values:
        cursor.execute('''INSERT INTO SUBTITLE_SEGMENT (track_id,sequence_number,start_ms,end_ms,content)
                          VALUES (%s,%s,%s,%s,%s)''', (track_id, sequence, start, end, text))
        segment_ids[sequence] = cursor.lastrowid
    save_revision(cursor, track_id)
    return track_id, segment_ids


def import_project(project, owner_id, media, segments):
    copied = None
    try:
        with mysql_transaction() as cursor:
            cursor.execute('SELECT user_id FROM `USER` WHERE user_id=%s AND status=%s FOR UPDATE',
                           (owner_id, 'ACTIVE'))
            if cursor.fetchone() is None:
                raise ValueError('Tài khoản đích không tồn tại hoặc bị khóa.')
            cursor.execute('SELECT project_id FROM LEGACY_IMPORT_MAP WHERE owner_id=%s AND legacy_project_id=%s',
                           (owner_id, project['id']))
            if cursor.fetchone():
                return False
            if media:
                UPLOADS.mkdir(parents=True, exist_ok=True)
                copied = UPLOADS / f'{uuid4()}{media.suffix}'
                shutil.copyfile(media, copied)
            cursor.execute('INSERT INTO PROJECT (user_id,project_name) VALUES (%s,%s)',
                           (owner_id, project['name'][:255]))
            project_id = cursor.lastrowid
            if media or project['source_type'] == 'URL':
                cursor.execute('''
                    INSERT INTO VIDEO (project_id,title,source_type,source_url,file_path,file_size_bytes,status)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                ''', (project_id, project['name'][:255], project['source_type'],
                      project['source_url'] if project['source_type'] == 'URL' else None,
                      str(copied) if copied else None, copied.stat().st_size if copied else None,
                      'COMPLETED' if segments else 'PENDING'))
                video_id = cursor.lastrowid
                if segments:
                    source, original_ids = write_track(
                        cursor, video_id, project.get('source_language') or 'und',
                        [(sequence, start, end, original) for sequence, start, end, original, _ in segments],
                    )
                    translated = [(sequence, start, end, target)
                                  for sequence, start, end, _, target in segments if target and target.strip()]
                    if translated:
                        _, target_ids = write_track(cursor, video_id, project.get('target_language') or 'und',
                                                    translated, source)
                        cursor.executemany('INSERT INTO SEGMENT_LINK VALUES (%s,%s)',
                                           [(original_ids[key], value) for key, value in target_ids.items()])
            cursor.execute('INSERT INTO LEGACY_IMPORT_MAP (owner_id,legacy_project_id,project_id) VALUES (%s,%s,%s)',
                           (owner_id, project['id'], project_id))
        return True
    except Exception:
        if copied is not None:
            copied.unlink(missing_ok=True)
        raise


def import_sqlite(source, owner_id, apply):
    with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as database:
        database.row_factory = sqlite3.Row
        database.execute('BEGIN')
        for project in map(dict, database.execute('SELECT * FROM projects ORDER BY id')):
            legacy_id = project['id']
            source_type = project['source_type']
            if source_type not in ('UPLOAD', 'URL') or (source_type == 'URL' and not project['source_url']):
                print(f'{legacy_id}: bỏ qua dự án mẫu hoặc nguồn không hợp lệ')
                continue
            media = source_media(legacy_id, source.parent / 'uploads')
            if not media:
                print(f'{legacy_id}: thiếu tệp nguồn')
                if source_type == 'UPLOAD':
                    continue
            segments = []
            for row in database.execute('SELECT * FROM segments WHERE project_id=? ORDER BY sequence_number,id',
                                        (legacy_id,)):
                start, end = row['start_time'], row['end_time']
                if not math.isfinite(start) or not math.isfinite(end) or start < 0 or round(end * 1000) <= round(start * 1000):
                    print(f"{legacy_id}: bỏ qua đoạn sai mốc {row['id']}")
                    continue
                segments.append((len(segments) + 1, round(start * 1000), round(end * 1000),
                                 row['source_text'], row['translated_text']))
            message = 'sẵn sàng nhập'
            if apply:
                message = 'đã nhập' if import_project(project, owner_id, media, segments) else 'đã có ánh xạ'
            print(f'{legacy_id}: {message}, {len(segments)} đoạn')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    subcommands = parser.add_subparsers(dest='command', required=True)
    subcommands.add_parser('migrate')
    importer = subcommands.add_parser('import-sqlite')
    importer.add_argument('--source', type=Path, default=ROOT / 'data' / 'subtitle_demo.db')
    importer.add_argument('--owner-id', type=int, required=True)
    importer.add_argument('--apply', action='store_true')
    options = parser.parse_args()
    load_dotenv(ROOT / '.env', interpolate=False)
    try:
        if options.command == 'migrate':
            migrate()
        else:
            import_sqlite(options.source, options.owner_id, options.apply)
    except (HTTPException, OSError, ValueError, sqlite3.Error) as error:
        parser.exit(1, f'{getattr(error, "detail", error)}\n')
