-- JobSeeker initial schema
-- Runs automatically on first postgres container start

CREATE TABLE IF NOT EXISTS applications (
    id              UUID PRIMARY KEY,
    person_id       VARCHAR(20)  NOT NULL,        -- 'sai' or 'gf'
    job_id          UUID         NOT NULL,
    source_id       VARCHAR(255) NOT NULL,         -- native ATS job ID
    company         VARCHAR(255) NOT NULL,
    role            VARCHAR(255) NOT NULL,
    job_url         TEXT         NOT NULL,
    location        VARCHAR(255) DEFAULT '',
    resume_variant  VARCHAR(100) DEFAULT '',
    resume_url      TEXT         DEFAULT '',
    cover_letter_url TEXT        DEFAULT '',
    match_score     FLOAT        DEFAULT 0.0,
    status          VARCHAR(50)  DEFAULT 'pending_review',
    applied_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ  DEFAULT NOW(),
    notes           TEXT         DEFAULT '',

    -- Prevent same job being tracked twice for the same person
    CONSTRAINT uq_source_person UNIQUE (source_id, person_id)
);

CREATE INDEX IF NOT EXISTS idx_applications_person    ON applications (person_id);
CREATE INDEX IF NOT EXISTS idx_applications_status    ON applications (status);
CREATE INDEX IF NOT EXISTS idx_applications_company   ON applications (company);
CREATE INDEX IF NOT EXISTS idx_applications_created   ON applications (created_at DESC);

-- Summary view useful for quick reporting
CREATE OR REPLACE VIEW application_summary AS
SELECT
    person_id,
    COUNT(*)                                         AS total,
    COUNT(*) FILTER (WHERE status = 'applied')       AS applied,
    COUNT(*) FILTER (WHERE status = 'screening')     AS screening,
    COUNT(*) FILTER (WHERE status = 'interview')     AS interview,
    COUNT(*) FILTER (WHERE status = 'offer')         AS offer,
    COUNT(*) FILTER (WHERE status = 'rejected')      AS rejected,
    ROUND(AVG(match_score)::NUMERIC, 2)              AS avg_match_score
FROM applications
GROUP BY person_id;
