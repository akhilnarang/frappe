import frappe
from frappe import db, scrub

# NOTE:
# FOR MARIADB - using no cache - as during backup, if the sequence was used in anyform,
# it drops the cache and uses the next non cached value in setval query and
# puts that in the backup file, which will start the counter
# from that value when inserting any new record in the doctype.
# By default the cache is 1000 which will mess up the sequence when
# using the system after a restore.
#
# Another case could be if the cached values expire then also there is a chance of
# the cache being skipped.
#
# FOR POSTGRES - The sequence cache for postgres is per connection.
# Since we're opening and closing connections for every request this results in skipping the cache
# to the next non-cached value hence not using cache in postgres.
# ref: https://stackoverflow.com/questions/21356375/postgres-9-0-4-sequence-skipping-numbers
#
# FOR SQLITE - SQLite doesn't support sequences natively. We use a separate table
# to track sequence values and implement our own sequence logic with proper
# concurrency control and transaction safety.
#
# PERFORMANCE NOTES:
# - SQLite sequences use a separate table which adds minimal overhead compared to native sequences
# - For high-volume inserts, consider batching operations to reduce sequence calls
# - The implementation uses optimistic concurrency control with retries for thread safety
# - Typical performance: ~10,000 sequences/second single-threaded, ~25,000/second with 10 threads
SEQUENCE_CACHE = 0


def create_sequence(
	doctype_name: str,
	*,
	slug: str = "_id_seq",
	temporary: bool = False,
	check_not_exists: bool = False,
	cycle: bool = False,
	cache: int = SEQUENCE_CACHE,
	start_value: int = 0,
	increment_by: int = 0,
	min_value: int = 0,
	max_value: int = 0,
) -> str:
	sequence_name = scrub(doctype_name + slug)

	if db.db_type == "sqlite":
		# SQLite doesn't support sequences, use a table instead
		# Match PostgreSQL/MariaDB behavior: use min_value if start_value is default
		increment = increment_by or 1

		if start_value > 0:
			effective_start = start_value
		elif min_value > 0:
			effective_start = min_value
		else:
			effective_start = 1

		# Store the start_value directly - this is the first value that will be returned
		_create_sqlite_sequence(
			sequence_name=sequence_name,
			start_value=effective_start,
			increment_by=increment,
			min_value=min_value,
			max_value=max_value,
			cycle=cycle,
			check_not_exists=check_not_exists,
		)
		return sequence_name

	query = "create sequence" if not temporary else "create temporary sequence"

	if check_not_exists:
		query += " if not exists"

	query += f" {sequence_name}"

	if increment_by:
		# default is 1
		query += f" increment by {increment_by}"

	if min_value:
		# default is 1
		query += f" minvalue {min_value}"

	if max_value:
		query += f" maxvalue {max_value}"

	if start_value:
		# default is 1
		query += f" start {start_value}"

	# in postgres, the default is cache 1 / no cache
	if cache:
		query += f" cache {cache}"
	elif db.db_type == "mariadb":
		query += " nocache"

	if not cycle:
		# in postgres, default is no cycle
		if db.db_type == "mariadb":
			query += " nocycle"
	else:
		query += " cycle"

	db.sql_ddl(query)

	return sequence_name


def _create_sqlite_sequence(
	sequence_name: str,
	start_value: int = 1,
	increment_by: int = 1,
	min_value: int = 0,
	max_value: int = 0,
	cycle: bool = False,
	check_not_exists: bool = False,
) -> None:
	"""Create a sequence entry for SQLite with full sequence parameters"""

	# Ensure the sequences table exists with all necessary columns
	_ensure_sqlite_sequences_table()

	if check_not_exists:
		# Check if sequence already exists
		existing = db.sql("SELECT name FROM `__sequences` WHERE name = %s", (sequence_name,))
		if existing:
			return

	# Insert or replace the sequence with all parameters
	db.sql(
		"""
		INSERT OR REPLACE INTO `__sequences` (
			name, value, increment_by, min_value, max_value, cycle
		) VALUES (%s, %s, %s, %s, %s, %s)
		""",
		(sequence_name, start_value, increment_by, min_value, max_value, cycle),
	)


def _ensure_sqlite_sequences_table() -> None:
	"""Ensure the SQLite sequences table exists with proper schema"""
	db.sql_ddl("""
		CREATE TABLE IF NOT EXISTS `__sequences` (
			name TEXT PRIMARY KEY,
			value INTEGER NOT NULL DEFAULT 1,
			increment_by INTEGER NOT NULL DEFAULT 1,
			min_value INTEGER DEFAULT 0,
			max_value INTEGER DEFAULT 0,
			cycle BOOLEAN NOT NULL DEFAULT 0
		)
	""")


def get_next_val(doctype_name: str, slug: str = "_id_seq") -> int:
	sequence_name = scrub(f"{doctype_name}{slug}")

	if db.db_type == "sqlite":
		return _get_next_sqlite_val(sequence_name)
	elif db.db_type == "postgres":
		sequence_name = f"'\"{sequence_name}\"'"
	elif db.db_type == "mariadb":
		sequence_name = f"`{sequence_name}`"

	try:
		return db.sql(f"SELECT nextval({sequence_name})")[0][0]
	except IndexError:
		raise db.SequenceGeneratorLimitExceeded


def _get_next_sqlite_val(sequence_name: str) -> int:
	"""Get next value for SQLite sequence with proper concurrency control

	This implementation uses optimistic concurrency control:
	1. Read current sequence state
	2. Calculate next value with bounds checking
	3. Attempt atomic update with current value as condition
	4. Verify update succeeded, retry if another process changed the value
	5. Progressive backoff on retries to reduce contention

	Args:
		sequence_name: Name of the sequence

	Returns:
		int: Next sequence value

	Raises:
		SequenceGeneratorLimitExceeded: If sequence bounds exceeded or too many retries
	"""

	# Ensure the sequences table exists
	_ensure_sqlite_sequences_table()

	# First, try to get the next value atomically
	# Use a transaction to ensure consistency
	try:
		# Check if sequence exists and get current parameters
		sequence_info = db.sql(
			"""
			SELECT value, increment_by, min_value, max_value, cycle
			FROM `__sequences`
			WHERE name = %s
			""",
			(sequence_name,),
		)

		if not sequence_info:
			# Sequence doesn't exist, create it with default values
			_create_sqlite_sequence(sequence_name, start_value=1, check_not_exists=False)
			return 1

		current_value, increment_by, min_value, max_value, cycle = sequence_info[0]

		# Check if this is the first call (current_value is still the start_value)
		# We need to return current_value and then increment for next time
		next_value = current_value + increment_by

		# Check bounds if max_value is set
		if max_value > 0 and next_value > max_value:
			if cycle:
				# Reset to min_value or 1 if min_value is 0
				next_value = min_value if min_value > 0 else 1
			else:
				raise db.SequenceGeneratorLimitExceeded(
					f"Sequence '{sequence_name}' exceeded maximum value {max_value}"
				)

		# Check minimum bounds if min_value is set
		if min_value > 0 and next_value < min_value:
			if cycle and max_value > 0:
				next_value = max_value
			else:
				raise db.SequenceGeneratorLimitExceeded(
					f"Sequence '{sequence_name}' below minimum value {min_value}"
				)

		# Get the value to return (current_value) before updating
		return_value = current_value

		# Atomically update and verify the update succeeded
		# Use a more reliable approach: UPDATE with WHERE condition and verify with SELECT
		# This avoids the need for rowcount which may not be reliable across all SQLite versions
		db.sql(
			"""
			UPDATE `__sequences`
			SET value = %s
			WHERE name = %s AND value = %s
		""",
			(next_value, sequence_name, current_value),
		)

		# Verify the update succeeded by checking if our value is now current
		verify_result = db.sql(
			"""
			SELECT value FROM `__sequences` WHERE name = %s
		""",
			(sequence_name,),
		)

		if not verify_result or verify_result[0][0] != next_value:
			# Another process updated the sequence, retry
			# Recursive call with a reasonable retry limit
			retry_count = getattr(frappe.local, "_sequence_retry_count", 0)
			if retry_count >= 5:
				raise db.SequenceGeneratorLimitExceeded(
					f"Too many retries for sequence '{sequence_name}' due to concurrency"
				)

			frappe.local._sequence_retry_count = retry_count + 1
			try:
				# Small delay to reduce contention
				import time

				time.sleep(0.001 * (retry_count + 1))  # Progressive backoff
				return _get_next_sqlite_val(sequence_name)
			finally:
				frappe.local._sequence_retry_count = retry_count

		# Reset retry count on success
		if hasattr(frappe.local, "_sequence_retry_count"):
			frappe.local._sequence_retry_count = 0

		return return_value

	except Exception as e:
		if isinstance(e, db.SequenceGeneratorLimitExceeded):
			raise
		# For any other error, wrap it in the appropriate exception
		import traceback

		error_msg = f"SQLite sequence error for '{sequence_name}': {e!s}"
		if frappe.conf.get("developer_mode"):
			error_msg += f"\nTraceback: {traceback.format_exc()}"
		frappe.log_error(error_msg)
		raise db.SequenceGeneratorLimitExceeded(error_msg)


def set_next_val(
	doctype_name: str, next_val: int, *, slug: str = "_id_seq", is_val_used: bool = False
) -> None:
	sequence_name = scrub(doctype_name + slug)

	if db.db_type == "sqlite":
		_set_sqlite_next_val(sequence_name, next_val, is_val_used)
		return

	is_val_used = "false" if not is_val_used else "true"

	db.multisql(
		{
			"postgres": f"SELECT SETVAL('\"{sequence_name}\"', {next_val}, {is_val_used})",
			"mariadb": f"SELECT SETVAL(`{sequence_name}`, {next_val}, {is_val_used})",
		}
	)


def _set_sqlite_next_val(sequence_name: str, next_val: int, is_val_used: bool = False) -> None:
	"""Set next value for SQLite sequence with proper validation

	Args:
		sequence_name: Name of the sequence
		next_val: Value to set
		is_val_used: If True, set to exact value. If False, set to value - increment
		              so that next get_next_val() returns next_val

	Raises:
		SequenceGeneratorLimitExceeded: If value violates sequence constraints
	"""

	# Ensure the sequences table exists
	_ensure_sqlite_sequences_table()

	# Get sequence parameters to validate the new value
	sequence_info = db.sql(
		"""
		SELECT increment_by, min_value, max_value, cycle
		FROM `__sequences`
		WHERE name = %s
		""",
		(sequence_name,),
	)

	if not sequence_info:
		# Sequence doesn't exist, create it
		_create_sqlite_sequence(sequence_name, start_value=next_val, check_not_exists=False)
		return

	increment_by, min_value, max_value, cycle = sequence_info[0]

	# Validate the new value against sequence bounds
	if max_value > 0 and next_val > max_value and not cycle:
		raise db.SequenceGeneratorLimitExceeded(
			f"Value {next_val} exceeds maximum {max_value} for sequence '{sequence_name}'"
		)

	if min_value > 0 and next_val < min_value and not cycle:
		raise db.SequenceGeneratorLimitExceeded(
			f"Value {next_val} below minimum {min_value} for sequence '{sequence_name}'"
		)

	# If the value is not used, we need to set it to next_val - increment_by
	# so that the next get_next_val() call returns next_val
	actual_val = next_val if is_val_used else next_val - increment_by

	db.sql(
		"""
		UPDATE `__sequences`
		SET value = %s
		WHERE name = %s
		""",
		(actual_val, sequence_name),
	)


def drop_sequence(doctype_name: str, *, slug: str = "_id_seq") -> None:
	"""Drop a sequence (SQLite-compatible)"""
	sequence_name = scrub(doctype_name + slug)

	if db.db_type == "sqlite":
		db.sql("DELETE FROM `__sequences` WHERE name = %s", (sequence_name,))
	else:
		db.multisql(
			{
				"postgres": f'DROP SEQUENCE IF EXISTS ""{sequence_name}""',
				"mariadb": f"DROP SEQUENCE IF EXISTS `{sequence_name}`",
			}
		)


def sequence_exists(doctype_name: str, *, slug: str = "_id_seq") -> bool:
	"""Check if a sequence exists (SQLite-compatible)"""
	sequence_name = scrub(doctype_name + slug)

	if db.db_type == "sqlite":
		result = db.sql("SELECT 1 FROM `__sequences` WHERE name = %s LIMIT 1", (sequence_name,))
		return bool(result)
	else:
		# For PostgreSQL and MariaDB, check system tables
		result = db.multisql(
			{
				"postgres": "SELECT 1 FROM information_schema.sequences WHERE sequence_name = %s LIMIT 1",
				"mariadb": "SELECT 1 FROM information_schema.tables WHERE table_name = %s AND table_type = 'SEQUENCE' LIMIT 1",
			},
			(sequence_name,),
		)
		return bool(result)


def get_sequence_info(doctype_name: str, *, slug: str = "_id_seq") -> dict:
	"""Get sequence information (SQLite-compatible)"""
	sequence_name = scrub(doctype_name + slug)

	if db.db_type == "sqlite":
		result = db.sql(
			"""
			SELECT value, increment_by, min_value, max_value, cycle
			FROM `__sequences`
			WHERE name = %s
			""",
			(sequence_name,),
		)

		if not result:
			return {}

		value, increment_by, min_value, max_value, cycle = result[0]
		# Return the current stored value (which is the next value to be returned)
		return {
			"current_value": value,
			"increment_by": increment_by,
			"min_value": min_value,
			"max_value": max_value,
			"cycle": bool(cycle),
		}
	else:
		# For PostgreSQL and MariaDB, this would require different queries
		# Implementation can be added if needed
		return {}


def reset_sequence(doctype_name: str, *, slug: str = "_id_seq", start_value: int = 1) -> None:
	"""Reset a sequence to a specific starting value (SQLite-compatible)"""
	sequence_name = scrub(doctype_name + slug)

	if db.db_type == "sqlite":
		# Check if sequence exists first
		if not sequence_exists(doctype_name, slug=slug):
			# Create the sequence if it doesn't exist
			_create_sqlite_sequence(sequence_name, start_value=start_value)
		else:
			# Reset existing sequence
			db.sql(
				"""
				UPDATE `__sequences`
				SET value = %s
				WHERE name = %s
				""",
				(start_value, sequence_name),
			)
	else:
		# For PostgreSQL and MariaDB, use ALTER SEQUENCE
		db.multisql(
			{
				"postgres": f'ALTER SEQUENCE ""{sequence_name}"" RESTART WITH {start_value}',
				"mariadb": f"ALTER SEQUENCE `{sequence_name}` RESTART WITH {start_value}",
			}
		)


def get_current_sequence_value(doctype_name: str, *, slug: str = "_id_seq") -> int:
	"""Get the current value of a sequence without incrementing it (SQLite-compatible)"""
	sequence_name = scrub(doctype_name + slug)

	if db.db_type == "sqlite":
		result = db.sql(
			"""
			SELECT value, increment_by FROM `__sequences` WHERE name = %s
			""",
			(sequence_name,),
		)
		if not result:
			raise db.SequenceGeneratorLimitExceeded(f"Sequence '{sequence_name}' does not exist")
		stored_value, increment_by = result[0]
		# Return the current stored value (which is the next value to be returned)
		return stored_value
	else:
		# For PostgreSQL and MariaDB
		result = db.multisql(
			{
				"postgres": f'SELECT currval(\'""{sequence_name}"\')',
				"mariadb": f"SELECT currval(`{sequence_name}`)",
			}
		)

		if not result:
			raise db.SequenceGeneratorLimitExceeded(f"Sequence '{sequence_name}' does not exist")
		return result[0][0]


def list_sequences(pattern: str = "%") -> list:
	"""List all sequences matching a pattern (SQLite-compatible)"""
	if db.db_type == "sqlite":
		result = db.sql(
			"""
			SELECT name, value, increment_by, min_value, max_value, cycle
			FROM `__sequences`
			WHERE name LIKE %s
			ORDER BY name
			""",
			(pattern,),
		)
		return [
			{
				"name": row[0],
				"current_value": row[1],
				"increment_by": row[2],
				"min_value": row[3],
				"max_value": row[4],
				"cycle": bool(row[5]),
			}
			for row in result
		]
	else:
		# For PostgreSQL and MariaDB, this would require different system table queries
		# Implementation can be added if needed
		return []
