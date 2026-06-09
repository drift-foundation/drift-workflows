-- Captures every significant state change for a singular work item.
CREATE TABLE `tb_singular_work_item_history` (
	`service_group` varchar(64) NOT NULL,
	`idempotency_key` varbinary(32) NOT NULL,
	`event_ts` datetime(6) NOT NULL,
	-- All persisted JSON is a DOCUMENT: non-NULL, valid, and a JSON OBJECT (the empty
	-- document is `{}`, never SQL NULL / JSON null / array / scalar). A non-lease event
	-- (CLAIMED/EXTENDED) carries `{}` for event_payload; an item with no checkpoint carries
	-- `{}` for checkpoint_payload. Enforced by CHECK here + validated at the gateway boundary.
	`item_meta` mediumtext NOT NULL CHECK (json_valid(`item_meta`) AND json_type(`item_meta`) = 'OBJECT'),
	-- Descriptive owner, but every event has one and it is exposed (inspect/history). NOT NULL +
	-- exactly 16 bytes (the SPs take varbinary(16) and SIGNAL on a non-16-byte owner, so a short
	-- value is rejected rather than silently zero-padded) — a NULL owner could never become JSON null.
	`lease_owner` binary(16) NOT NULL,
	`lease_meta` mediumtext NOT NULL CHECK (json_valid(`lease_meta`) AND json_type(`lease_meta`) = 'OBJECT'),
	-- A live (WORKING, status=1) lease MUST have a bounded expiry — an unbounded lease would
	-- block crash recovery / reclaim. Terminal events (DONE/FAILED) carry no live lease, so
	-- NULL is allowed there. (Not every event is a lease, so the column stays nullable with a
	-- status-conditional CHECK rather than a blanket NOT NULL.)
	`lease_expires_at` datetime(6) NULL CHECK (`status` <> 1 OR `lease_expires_at` IS NOT NULL),
	-- event_type and status are not independent: each event implies exactly one lifecycle status.
	-- The CHECK makes a mismatched pair (e.g. COMPLETED+WORKING) unrepresentable; the gateway also
	-- cross-checks the transported pair on decode (defense against a non-conforming backend).
	--   CLAIMED(10)/EXTENDED(11) -> WORKING(1) · COMPLETED(20) -> DONE(2) · FAILED(40) -> FAILED(3)
	`event_type` tinyint unsigned NOT NULL,
	`status` tinyint unsigned NOT NULL,
	CONSTRAINT `ck_singular_history_event_status` CHECK (
		(`event_type` = 10 AND `status` = 1)
		OR (`event_type` = 11 AND `status` = 1)
		OR (`event_type` = 20 AND `status` = 2)
		OR (`event_type` = 40 AND `status` = 3)
	),
	`event_payload` mediumtext NOT NULL CHECK (json_valid(`event_payload`) AND json_type(`event_payload`) = 'OBJECT'),
	`lease_token` varbinary(16) NULL,
	`checkpoint_payload` mediumtext NOT NULL CHECK (json_valid(`checkpoint_payload`) AND json_type(`checkpoint_payload`) = 'OBJECT'),
	PRIMARY KEY (`service_group`,`idempotency_key`,`event_ts`),
	KEY `idx_singular_history_event_ts` (`event_ts`)
) ENGINE=InnoDB;
