USE subtitle_ai_db;

SET @column_exists = (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = 'subtitle_ai_db'
      AND TABLE_NAME = 'SUBTITLE_SEGMENT'
      AND COLUMN_NAME = 'is_locked'
);
SET @sql = IF(@column_exists = 0,
    'ALTER TABLE SUBTITLE_SEGMENT ADD COLUMN is_locked BOOLEAN NOT NULL DEFAULT FALSE',
    'SELECT 1');
PREPARE add_column FROM @sql;
EXECUTE add_column;
DEALLOCATE PREPARE add_column;

SET @index_exists = (
    SELECT COUNT(*) FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = 'subtitle_ai_db'
      AND TABLE_NAME = 'SUBTITLE_SEGMENT'
      AND INDEX_NAME = 'idx_subtitle_segment_locked'
);
SET @sql = IF(@index_exists = 0,
    'CREATE INDEX idx_subtitle_segment_locked ON SUBTITLE_SEGMENT (track_id, is_locked)',
    'SELECT 1');
PREPARE add_index FROM @sql;
EXECUTE add_index;
DEALLOCATE PREPARE add_index;
