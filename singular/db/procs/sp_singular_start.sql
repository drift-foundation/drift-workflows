DELIMITER $$
-- Begin BRAND-NEW work. The PK (service_group, idempotency_key) is the serializer:
-- exactly one concurrent caller wins the INSERT and is GRANTED a Fresh lease; every
-- other caller gets EXISTS and must call sp_singular_resume. This SP NEVER reads an
-- existing row (no select-then-insert).
--
-- Duplicate detection is an EXPLICIT handler for ER_DUP_ENTRY (1062), scoped to just
-- the projection INSERT -- NOT `INSERT IGNORE`. INSERT IGNORE downgrades truncation,
-- type-conversion, and other data errors to warnings, which would then be misread as
-- "row already exists" (EXISTS). With a plain INSERT, only a real PK conflict yields
-- EXISTS; every other error propagates as a genuine failure.
CREATE PROCEDURE `sp_singular_start`(
	IN arg_service_group varchar(64),
	IN arg_idempotency_key varbinary(32),
	IN arg_item_meta mediumtext,
	IN arg_lease_owner varbinary(16),       -- descriptive (SingularIdentity); varbinary so short values are NOT padded
	IN arg_lease_meta mediumtext,
	IN arg_lease_timeout_seconds int,
	IN arg_lease_token varbinary(16)        -- app-minted capability token; the authority
)
proc:BEGIN
	DECLARE v_now datetime(6);
	DECLARE v_lease_expires datetime(6);
	DECLARE v_exists tinyint(1) DEFAULT 0;

	DECLARE CONST_STATUS_WORKING tinyint unsigned DEFAULT 1;

	DECLARE CONST_EVENT_CLAIMED tinyint unsigned DEFAULT 10;

	-- Token validation: required, exactly 16 bytes. Invalid input -> SIGNAL.
	IF arg_lease_token IS NULL OR LENGTH(arg_lease_token) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'SingularLeaseTokenInvalid';
	END IF;

	-- Owner is descriptive but PERSISTED non-null: require exactly 16 bytes (varbinary, so a short
	-- value is rejected here rather than silently zero-padded by a binary(16) parameter).
	IF arg_lease_owner IS NULL OR LENGTH(arg_lease_owner) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'SingularLeaseOwnerInvalid';
	END IF;

	-- Persisted JSON is a DOCUMENT: non-NULL, valid, JSON OBJECT (the gateway also validates
	-- this object contract before SQL; these SIGNALs are the backend-side guarantee). Absent
	-- metadata is the empty document `{}` (the gateway substitutes it), never SQL NULL.
	IF arg_item_meta IS NULL OR JSON_VALID(arg_item_meta) = 0 OR JSON_TYPE(arg_item_meta) <> 'OBJECT' THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'SingularItemMetaInvalid';
	END IF;

	IF arg_lease_meta IS NULL OR JSON_VALID(arg_lease_meta) = 0 OR JSON_TYPE(arg_lease_meta) <> 'OBJECT' THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'SingularLeaseMetaInvalid';
	END IF;

	-- A live lease MUST be bounded: a NULL/non-positive timeout would create an
	-- unbounded WORKING lease that can never be reclaimed. Reject it (the gateway also
	-- validates client-side via InvalidLeaseTimeout; this is the backend-side guarantee).
	IF arg_lease_timeout_seconds IS NULL OR arg_lease_timeout_seconds <= 0 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'SingularLeaseTimeoutInvalid';
	END IF;

	SET v_now = UTC_TIMESTAMP(6);
	SET v_lease_expires = DATE_ADD(v_now, INTERVAL arg_lease_timeout_seconds SECOND);

	-- The PK serializes concurrent first-submits: exactly one INSERT succeeds. A PK
	-- conflict (ER_DUP_ENTRY = 1062) flips v_exists and continues; any OTHER error
	-- escapes the handler and aborts the proc. The handler is scoped to this nested
	-- block so it cannot mask a 1062 from the (by-construction unique) history insert.
	BEGIN
		DECLARE CONTINUE HANDLER FOR 1062 SET v_exists = 1;
		INSERT INTO `tb_singular_work_item` (
			`service_group`,
			`idempotency_key`,
			`current_event_ts`,
			`checkpoint_payload`,
			`current_lease_token`,
			`terminal_lease_token`,
			`created_at`,
			`updated_at`
		) VALUES (
			arg_service_group,
			arg_idempotency_key,
			v_now,
			'{}',                    -- checkpoint_payload: empty document (never SQL NULL)
			arg_lease_token,
			NULL,
			v_now,
			v_now
		);
	END;

	IF v_exists = 1 THEN
		-- Row already exists. Do NOT read it; the caller resume()s.
		SELECT JSON_OBJECT('outcome', 'exists') AS result;
		LEAVE proc;
	END IF;

	INSERT INTO `tb_singular_work_item_history` (
		`service_group`,
		`idempotency_key`,
		`event_ts`,
		`item_meta`,
		`lease_owner`,
		`lease_meta`,
		`lease_expires_at`,
		`event_type`,
		`status`,
		`event_payload`,
		`lease_token`,
		`checkpoint_payload`
	) VALUES (
		arg_service_group,
		arg_idempotency_key,
		v_now,
		arg_item_meta,
		arg_lease_owner,
		arg_lease_meta,
		v_lease_expires,
		CONST_EVENT_CLAIMED,
		CONST_STATUS_WORKING,
		'{}',                    -- event_payload: CLAIMED is a non-terminal event -> empty document
		arg_lease_token,
		'{}'                     -- checkpoint_payload: empty document (never SQL NULL)
	);

	-- GRANTED: one discriminated JSON result document. The lease token is the caller's own
	-- (echoed input), so it is not re-returned here; the gateway threads the input token.
	SELECT JSON_OBJECT(
		'outcome', 'granted',
		'lease_expires_at', v_lease_expires
	) AS result;
END $$
DELIMITER ;
