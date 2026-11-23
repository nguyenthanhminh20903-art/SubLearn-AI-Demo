from pathlib import Path

from fastapi import HTTPException

from .media_preview import preview_path


def delete_project_content(cursor, project_id, storage):
    cursor.execute('SELECT video_id,file_path FROM VIDEO WHERE project_id=%s FOR UPDATE', (project_id,))
    videos = cursor.fetchall()
    cursor.execute('''SELECT j.job_id FROM PROCESSING_JOB j JOIN VIDEO v ON v.video_id=j.video_id
                      WHERE v.project_id=%s AND j.status IN ('PENDING','RUNNING') LIMIT 1 FOR UPDATE''', (project_id,))
    if cursor.fetchone():
        raise HTTPException(409, 'Dự án đang có tác vụ xử lý.')
    cursor.execute('''SELECT e.result_id FROM EXPERIMENT_RESULT e JOIN PROCESSING_JOB j ON j.job_id=e.job_id
                      JOIN VIDEO v ON v.video_id=j.video_id WHERE v.project_id=%s LIMIT 1''', (project_id,))
    if cursor.fetchone():
        raise HTTPException(409, 'Dự án đang được dùng trong kết quả thực nghiệm.')
    paths = [('VIDEO', storage / 'uploads', row['file_path']) for row in videos]
    for table, root, joins in (
        ('AUDIO_ASSET', 'audio', 'JOIN VIDEO v ON v.video_id=x.video_id'),
        ('EXPORT_FILE', 'exports', '''JOIN SUBTITLE_REVISION r ON r.revision_id=x.revision_id
            JOIN SUBTITLE_TRACK t ON t.track_id=r.track_id JOIN VIDEO v ON v.video_id=t.video_id'''),
        ('LIVE_CHUNK', 'audio', '''JOIN LIVE_SESSION l ON l.session_id=x.session_id
            JOIN VIDEO v ON v.video_id=l.video_id'''),
    ):
        column = 'audio_path' if table == 'LIVE_CHUNK' else 'file_path'
        cursor.execute(f'SELECT x.{column} AS path FROM {table} x {joins} WHERE v.project_id=%s', (project_id,))
        paths.extend((table, storage / root, row['path']) for row in cursor.fetchall())

    revision = '''JOIN SUBTITLE_REVISION r ON r.revision_id=x.revision_id
                  JOIN SUBTITLE_TRACK t ON t.track_id=r.track_id JOIN VIDEO v ON v.video_id=t.video_id'''
    track = 'JOIN SUBTITLE_TRACK t ON t.track_id=x.track_id JOIN VIDEO v ON v.video_id=t.video_id'
    segment_track = 'JOIN SUBTITLE_TRACK t ON t.track_id=s.track_id JOIN VIDEO v ON v.video_id=t.video_id'
    job = 'JOIN PROCESSING_JOB j ON j.job_id=x.job_id JOIN VIDEO v ON v.video_id=j.video_id'
    video = 'JOIN VIDEO v ON v.video_id=x.video_id'
    steps = [
        ('EXPORT_FILE', revision),
        ('WORD_ALIGNMENT', f'JOIN SUBTITLE_SEGMENT s ON s.segment_id=x.segment_id {segment_track}'),
        ('SEGMENT_LINK', f'JOIN SUBTITLE_SEGMENT s ON s.segment_id=x.source_segment_id {segment_track}'),
        ('SEGMENT_LINK', f'JOIN SUBTITLE_SEGMENT s ON s.segment_id=x.target_segment_id {segment_track}'),
        ('SUBTITLE_ISSUE', f'JOIN SUBTITLE_SEGMENT s ON s.segment_id=x.segment_id {segment_track}'),
        ('SUBTITLE_ISSUE', f'JOIN SUBTITLE_SEGMENT s ON s.segment_id=x.related_segment_id {segment_track}'),
        ('SUBTITLE_REVISION', track),
        ('SUBTITLE_SEGMENT', track),
        ('LIVE_CHUNK', 'JOIN LIVE_SESSION l ON l.session_id=x.session_id JOIN VIDEO v ON v.video_id=l.video_id'),
        ('LIVE_SESSION', video),
        ('SPEECH_SEGMENT', job),
        ('SPEAKER', video),
        ('PROCESSING_STEP', job),
        ('JOB_MODEL', job),
        ('RESOURCE_METRIC', job),
    ]
    for table, joins in steps:
        cursor.execute(f'DELETE x FROM {table} x {joins} WHERE v.project_id=%s', (project_id,))
    for table, parent_column in (
        ('SUBTITLE_TRACK', 'source_track_id'), ('AUDIO_ASSET', 'parent_audio_id'),
        ('PROCESSING_JOB', 'retry_of_job_id'),
    ):
        cursor.execute(f'UPDATE {table} x {video} SET x.{parent_column}=NULL WHERE v.project_id=%s', (project_id,))
        cursor.execute(f'DELETE x FROM {table} x {video} WHERE v.project_id=%s', (project_id,))
    cursor.execute('DELETE FROM VIDEO WHERE project_id=%s', (project_id,))
    cursor.execute('DELETE FROM PROJECT WHERE project_id=%s', (project_id,))
    removable = []
    for table, root, filename in paths:
        if not filename:
            continue
        path = Path(filename)
        if path.is_symlink() or path.resolve().parent != root.resolve():
            continue
        column = 'audio_path' if table == 'LIVE_CHUNK' else 'file_path'
        cursor.execute(f'SELECT 1 AS present FROM {table} WHERE {column}=%s LIMIT 1', (filename,))
        if not cursor.fetchone():
            removable.append(path)
            if table == 'VIDEO':
                preview = preview_path(path.resolve())
                if preview.parent.resolve() == preview.parent:
                    removable.append(preview)
    return removable
