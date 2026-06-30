-- 004_fix_furnished_check.sql
-- Repair existing Sprint 2 databases whose `furnished` check constraint
-- predates the `unknown` enum value or contains mixed-case values from
-- LLM JSON booleans.

ALTER TABLE apartments
    DROP CONSTRAINT IF EXISTS apartments_furnished_check;

UPDATE apartments
SET furnished = CASE
    WHEN furnished IS NULL THEN NULL
    WHEN lower(trim(furnished)) IN ('true', 'yes', 'y', 'si', 'furnished')
        THEN 'true'
    WHEN lower(trim(furnished)) IN ('false', 'no', 'n', 'unfurnished', 'sin amueblar')
        THEN 'false'
    WHEN lower(trim(furnished)) = 'unknown'
        THEN 'unknown'
    ELSE 'unknown'
END;

ALTER TABLE apartments
    ADD CONSTRAINT apartments_furnished_check
    CHECK (furnished IS NULL OR furnished IN ('true', 'false', 'unknown'));
