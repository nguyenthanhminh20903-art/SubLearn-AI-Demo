CREATE TABLE IF NOT EXISTS LEGACY_IMPORT_MAP (
    owner_id BIGINT UNSIGNED NOT NULL,
    legacy_project_id VARCHAR(100) NOT NULL,
    project_id BIGINT UNSIGNED NOT NULL,
    imported_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (owner_id, legacy_project_id),
    UNIQUE KEY uq_legacy_project (project_id)
) ENGINE=InnoDB
