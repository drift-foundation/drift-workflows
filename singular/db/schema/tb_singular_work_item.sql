-- Tracks the latest state of a singular work item for a downstream service group.
CREATE TABLE `tb_singular_work_item` (
	`service_group` varchar(64) NOT NULL,
	`idempotency_key` varbinary(32) NOT NULL,
	`current_event_ts` datetime(6) NOT NULL,
	-- All persisted JSON is a DOCUMENT: non-NULL, valid, and a JSON OBJECT (the empty
	-- document is `{}`, never SQL NULL / JSON null / array / scalar). Enforced by CHECK
	-- here and validated again at the gateway boundary (before SQL and on decode).
	`checkpoint_payload` mediumtext NOT NULL CHECK (json_valid(`checkpoint_payload`) AND json_type(`checkpoint_payload`) = 'OBJECT'),
	-- Capability tokens (PR1): the live lease's token, and the token that wrote the
	-- terminal state. Authority lives here, not in lease_owner (which is descriptive).
	`current_lease_token` varbinary(16) NULL,
	`terminal_lease_token` varbinary(16) NULL,
	`created_at` datetime(6) NOT NULL,
	`updated_at` datetime(6) NOT NULL,
	PRIMARY KEY (`service_group`,`idempotency_key`),
	KEY `idx_singular_current_event_ts` (`current_event_ts`)
) ENGINE=InnoDB;
