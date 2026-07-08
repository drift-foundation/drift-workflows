-- viz_ro — SELECT-only DB user for the microflows-viz `serve` backend (work/viz-consolidation).
--
-- The backend is read-only BY PERMISSION, not convention: its test suite connects as this user,
-- so any mutating statement fails at the DB (ER_TABLEACCESS_DENIED, 1142) instead of surviving
-- as an unnoticed code path. Applied by Mariachi like every grants/ file ({{SCHEMA}} is
-- substituted at apply time; both statements are idempotent under repeated applies, and users
-- are server-scoped, so a --destroy-database schema reload does not remove them).
--
-- The IDENTIFIED BY password below is the DEVELOPMENT fixture credential (same posture as the
-- fixture's committed root password). Production deployments MUST rotate it at provision time
-- (ALTER USER 'viz_ro'@'%' IDENTIFIED BY '<real secret>') or provision their own user carrying
-- this same single SELECT grant.
CREATE USER IF NOT EXISTS 'viz_ro'@'%' IDENTIFIED BY 'vizro_dev';

GRANT SELECT ON `{{SCHEMA}}`.* TO 'viz_ro'@'%';
