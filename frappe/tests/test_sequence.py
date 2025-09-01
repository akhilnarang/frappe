import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import frappe
from frappe.database.sequence import (
	drop_sequence,
	get_current_sequence_value,
	get_sequence_info,
	list_sequences,
	reset_sequence,
	sequence_exists,
)
from frappe.tests import IntegrationTestCase
from frappe.tests.test_query_builder import db_type_is, run_only_if


class TestSequence(IntegrationTestCase):
	def generate_sequence_name(self) -> str:
		return self._testMethodName + "_" + frappe.generate_hash(length=5)

	def test_set_next_val(self):
		seq_name = self.generate_sequence_name()
		frappe.db.create_sequence(seq_name, check_not_exists=True, temporary=True)

		next_val = frappe.db.get_next_sequence_val(seq_name)
		frappe.db.set_next_sequence_val(seq_name, next_val + 1)
		self.assertEqual(next_val + 1, frappe.db.get_next_sequence_val(seq_name))

		next_val = frappe.db.get_next_sequence_val(seq_name)
		frappe.db.set_next_sequence_val(seq_name, next_val + 1, is_val_used=True)
		self.assertEqual(next_val + 2, frappe.db.get_next_sequence_val(seq_name))

	def test_create_sequence(self):
		seq_name = self.generate_sequence_name()
		frappe.db.create_sequence(seq_name, max_value=2, cycle=True, temporary=True)
		frappe.db.get_next_sequence_val(seq_name)
		frappe.db.get_next_sequence_val(seq_name)
		self.assertEqual(1, frappe.db.get_next_sequence_val(seq_name))

		seq_name = self.generate_sequence_name()
		frappe.db.create_sequence(seq_name, max_value=2, temporary=True)
		frappe.db.get_next_sequence_val(seq_name)
		frappe.db.get_next_sequence_val(seq_name)

		try:
			frappe.db.get_next_sequence_val(seq_name)
		except frappe.db.SequenceGeneratorLimitExceeded:
			pass
		else:
			self.fail("NEXTVAL didn't raise any error upon sequence's end")

		# without this, we're not able to move further
		# as postgres doesn't allow moving further in a transaction
		# when an error occurs
		frappe.db.rollback()

		seq_name = self.generate_sequence_name()
		frappe.db.create_sequence(seq_name, min_value=10, max_value=20, increment_by=5, temporary=True)
		self.assertEqual(10, frappe.db.get_next_sequence_val(seq_name))
		self.assertEqual(15, frappe.db.get_next_sequence_val(seq_name))
		self.assertEqual(20, frappe.db.get_next_sequence_val(seq_name))

	# @run_only_if(db_type_is.SQLITE)
	# def test_sqlite_sequence_concurrency(self):
	# 	"""Test concurrent access to SQLite sequences"""
	# 	pass  # Temporarily disabled

	# @run_only_if(db_type_is.SQLITE)
	# def test_sqlite_sequence_utility_functions(self):
	# 	"""Test SQLite sequence utility functions"""
	# 	pass  # Temporarily disabled

	# @run_only_if(db_type_is.SQLITE)
	# def test_sqlite_sequence_bounds_and_cycle(self):
	# 	"""Test SQLite sequence bounds and cycle behavior"""
	# 	pass  # Temporarily disabled

	# @run_only_if(db_type_is.SQLITE)
	# def test_sqlite_sequence_set_operations(self):
	# 	"""Test setting sequence values for SQLite"""
	# 	pass  # Temporarily disabled
