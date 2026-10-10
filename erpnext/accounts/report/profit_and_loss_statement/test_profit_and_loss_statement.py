# Copyright (c) 2023, Frappe Technologies Pvt. Ltd. and Contributors
# MIT License. See license.txt

from unittest.mock import Mock, patch

import duckdb
import frappe
from frappe.desk.query_report import export_query
from frappe.utils import add_days, getdate, today

from erpnext.accounts.doctype.sales_invoice.test_sales_invoice import create_sales_invoice
from erpnext.accounts.report import utils as report_utils
from erpnext.accounts.report.financial_statements import (
	build_period_list,
	calculate_values,
	get_period_keys_for_total,
	get_period_list,
	is_dimension_grouped,
)
from erpnext.accounts.report.profit_and_loss_statement import profit_and_loss_statement as report
from erpnext.accounts.report.profit_and_loss_statement.profit_and_loss_statement import execute
from erpnext.accounts.test.accounts_mixin import AccountsTestMixin
from erpnext.tests.utils import ERPNextTestSuite


def _create_snapshot_gl_table(conn):
	conn.execute(
		'CREATE TABLE "tabGL Entry" (company VARCHAR, is_cancelled INTEGER, account VARCHAR, '
		"voucher_type VARCHAR, posting_date DATE, debit DOUBLE, credit DOUBLE, "
		"debit_in_account_currency DOUBLE, credit_in_account_currency DOUBLE, "
		"account_currency VARCHAR, cost_center VARCHAR, project VARCHAR, finance_book VARCHAR)"
	)


def _insert_snapshot_currency_rows(conn, rows):
	for day, account, credit, account_credit, account_currency, dimension in rows:
		conn.execute(
			'INSERT INTO "tabGL Entry" VALUES (?, 0, ?, ?, ?, 0, ?, 0, ?, ?, ?, ?, NULL)',
			[
				"Snapshot Test",
				account,
				"Journal Entry",
				day,
				credit,
				account_credit,
				account_currency,
				dimension,
				dimension,
			],
		)


def _snapshot_currency_accounts(separate_roots):
	accounts = [
		frappe._dict(name="Income Root", is_group=True, parent_account=None, lft=1, rgt=6),
		frappe._dict(name="Foreign Income", is_group=False, parent_account="Income Root", lft=2, rgt=3),
		frappe._dict(name="Local Income", is_group=False, parent_account="Income Root", lft=4, rgt=5),
	]
	if separate_roots:
		accounts[0].rgt = 4
		accounts[2].update(parent_account="Other Root", lft=6, rgt=7)
		accounts.append(frappe._dict(name="Other Root", is_group=True, parent_account=None, lft=5, rgt=8))
	accounts.sort(key=lambda account: account.lft)
	return accounts


def _depth_income_accounts(hidden_outside_root):
	accounts = [
		frappe._dict(name="Income Root", is_group=True, parent_account=None, lft=1, rgt=48),
		frappe._dict(name="Foreign Income", is_group=False, parent_account="Income Root", lft=2, rgt=3),
	]
	parent = "Income Root"
	for level in range(1, 22):
		name = f"Depth {level}"
		accounts.append(
			frappe._dict(name=name, is_group=True, parent_account=parent, lft=3 + level, rgt=48 - level)
		)
		parent = name
	accounts.append(
		frappe._dict(
			name="Hidden Income",
			is_group=False,
			parent_account=parent,
			lft=100 if hidden_outside_root else 25,
			rgt=101 if hidden_outside_root else 26,
		)
	)
	for account in accounts:
		account.account_name = account.name
		account.root_type = "Income"
		account.report_type = "Profit and Loss"
	return accounts


def _seed_snapshot_gl_rows(conn):
	rows = [
		("2026-01-15", 60, "A"),
		("2026-01-16", 40, "A"),
		("2026-02-15", 200, "A"),
		("2026-01-15", 50, "B's Center"),
		("2026-02-15", 75, "B's Center"),
	]
	_insert_snapshot_currency_rows(
		conn, [(day, "Income", amount, amount, "AUD", value) for day, amount, value in rows]
	)
	for company, cancelled, account, voucher, day in (
		("Other Company", 0, "Income", "Journal Entry", "2026-01-15"),
		("Snapshot Test", 1, "Income", "Journal Entry", "2026-01-15"),
		("Snapshot Test", 0, "Unrelated", "Journal Entry", "2026-01-15"),
		("Snapshot Test", 0, "Income", "Period Closing Voucher", "2026-01-15"),
		("Snapshot Test", 0, "Income", "Journal Entry", "2025-12-31"),
		("Snapshot Test", 0, "Income", "Journal Entry", "2026-03-01"),
	):
		conn.execute(
			'INSERT INTO "tabGL Entry" VALUES (?, ?, ?, ?, ?, 0, 1000, 0, 1000, ?, ?, ?, NULL)',
			[company, cancelled, account, voucher, day, "AUD", "A", "A"],
		)


class TestProfitAndLossStatement(ERPNextTestSuite, AccountsTestMixin):
	def test_project_selection_is_normalised_before_period_construction(self):
		cases = (
			(None, None),
			("", ""),
			([], []),
			("[]", []),
			(["A"], ["A"]),
			('["A"]', ["A"]),
			(["A", "B's Project"], ["A", "B's Project"]),
			('["A", "B\'s Project"]', ["A", "B's Project"]),
		)
		for entry_point in (execute, report.execute_snapshot_report):
			for selection, expected in cases:
				with self.subTest(entry_point=entry_point.__name__, selection=selection):
					filters = frappe._dict(project=selection)
					conn = Mock()
					with (
						patch("frappe.database.duckdb.database.get_latest_sync", return_value=conn),
						patch.object(
							report, "build_period_list", side_effect=RuntimeError("period boundary")
						) as periods,
						self.assertRaisesRegex(RuntimeError, "period boundary"),
					):
						entry_point(filters)
					self.assertEqual(filters.project, expected)
					periods.assert_called_once_with(filters)
					if entry_point is report.execute_snapshot_report:
						conn.close.assert_called_once_with()

	def test_invalid_project_selections_cannot_reach_financial_computation(self):
		invalid_selections = (
			"[",
			"null",
			"42",
			"true",
			'"A"',
			"{}",
			'["A", null]',
			'["A", 42]',
			'["A", {}]',
			'[["A"]]',
			'[" "]',
			[None],
			[42],
			[{}],
			[["A"]],
			[""],
			[" "],
			42,
			False,
			{},
		)
		for entry_point in (execute, report.execute_snapshot_report):
			for selection in invalid_selections:
				with self.subTest(entry_point=entry_point.__name__, selection=selection):
					filters = frappe._dict(project=selection)
					conn = Mock()
					with (
						patch("frappe.database.duckdb.database.get_latest_sync", return_value=conn),
						patch.object(report, "build_period_list") as periods,
						self.assertRaises(frappe.ValidationError),
					):
						entry_point(filters)
					periods.assert_not_called()
					self.assertEqual(filters.project, selection)
					if entry_point is report.execute_snapshot_report:
						conn.close.assert_called_once_with()

	def test_snapshot_closes_connection_after_successful_report(self):
		conn = Mock()
		filters = frappe._dict(company="Snapshot Test", presentation_currency="AUD")
		with (
			patch("frappe.database.duckdb.database.get_latest_sync", return_value=conn),
			patch.object(report, "build_period_list", return_value=[frappe._dict(key="jan")]),
			patch.object(report, "_get_data_duckdb", return_value=None),
			patch.object(report, "get_net_profit_loss", return_value=None),
			patch.object(report, "get_columns", return_value=[]),
			patch.object(report, "get_chart_data", return_value={}),
			patch.object(report, "get_report_summary", return_value=(None, None)),
		):
			self.assertEqual(report.execute_snapshot_report(filters), ([], [], None, {}, None, None))
		conn.close.assert_called_once_with()

	def test_snapshot_empty_periods_match_ordinary_report(self):
		self.assertTrue(report.get_accounts(self.company, "Income"))
		conn = Mock()
		filters = frappe._dict(company=self.company, accumulated_values=False)
		with (
			patch("frappe.database.duckdb.database.get_latest_sync", return_value=conn),
			patch.object(report, "build_period_list", return_value=[]),
		):
			self.assertIsNone(execute(filters))
			self.assertIsNone(report.execute_snapshot_report(filters))
		conn.close.assert_called_once_with()

	def test_snapshot_missing_sync_precedes_project_validation(self):
		with (
			patch("frappe.database.duckdb.database.get_latest_sync", return_value=None),
			patch.object(report, "_normalise_project_filter") as normalise,
			patch.object(report, "build_period_list") as periods,
			self.assertRaisesRegex(frappe.ValidationError, "requires .* to be synced to DuckDB"),
		):
			report.execute_snapshot_report(frappe._dict(project="["))
		normalise.assert_not_called()
		periods.assert_not_called()

	def _snapshot_currency_values(
		self, rows, grouped=False, separate_roots=False, feb_only=False, project=None, currency="USD"
	):
		periods = [
			frappe._dict(key="jan", from_date=getdate("2026-01-01"), to_date=getdate("2026-01-31")),
			frappe._dict(key="feb", from_date=getdate("2026-02-01"), to_date=getdate("2026-02-28")),
		]
		for period in periods:
			period.year_start_date = getdate("2026-01-01")
		if feb_only:
			periods = periods[1:]
		if grouped:
			periods = [
				frappe._dict(
					period, key=f"{value}_{period.key}", dimension_field="cost_center", dimension_value=value
				)
				for value in ("A", "B")
				for period in periods
			]
		filters = frappe._dict(
			company="Snapshot Test",
			presentation_currency=currency,
			period_end_date="2026-02-28",
			group_by_dimension="Cost Center" if grouped else None,
			project=project,
		)
		accounts = _snapshot_currency_accounts(separate_roots)
		with (
			duckdb.connect(":memory:") as conn,
			patch.object(report_utils, "get_company_currency", return_value="AUD"),
			patch.object(report_utils, "get_rate_as_at", return_value=2) as rate,
		):
			_create_snapshot_gl_table(conn)
			_insert_snapshot_currency_rows(conn, rows)
			entries = {}
			report._load_gl_entries_duckdb(conn, filters, periods, accounts, entries)
			if currency:
				for call in rate.call_args_list:
					self.assertEqual(call.args, ("2026-02-28", "USD", "AUD"))
			else:
				rate.assert_not_called()
		return {name: sum(entry.credit for entry in values) for name, values in entries.items()}

	def test_snapshot_currency_uses_root_report_population(self):
		usd = ("2026-01-15", "Foreign Income", 100, 90, "USD", "A")
		aud = ("2026-02-15", "Local Income", 200, 200, "AUD", "B")
		cases = (
			([usd, aud], {}, {"Foreign Income": 50, "Local Income": 100}),
			([usd, aud], {"grouped": True}, {"Foreign Income": 50, "Local Income": 100}),
			([usd], {"grouped": True}, {"Foreign Income": 90}),
			(
				[usd, aud],
				{"grouped": True, "separate_roots": True},
				{"Foreign Income": 90, "Local Income": 100},
			),
			([usd, (*aud[:5], None)], {"grouped": True}, {"Foreign Income": 50}),
			(
				[("2026-02-15", *usd[1:]), ("2026-01-15", *aud[1:])],
				{"feb_only": True},
				{"Foreign Income": 50},
			),
			([usd, aud], {"grouped": True, "project": ["A"]}, {"Foreign Income": 90}),
			([usd, aud], {"grouped": True, "currency": None}, {"Foreign Income": 100, "Local Income": 200}),
		)
		for rows, options, expected in cases:
			with self.subTest(options=options, rows=rows):
				self.assertEqual(self._snapshot_currency_values(rows, **options), expected)

	def test_snapshot_currency_rejects_ambiguous_roots(self):
		period = frappe._dict(
			from_date=getdate("2026-01-01"),
			to_date=getdate("2026-01-31"),
			year_start_date=getdate("2026-01-01"),
		)
		filters = frappe._dict(company="Snapshot Test", presentation_currency="USD")
		for roots in (
			[],
			[
				frappe._dict(name="Root", parent_account=None, is_group=True, lft=1, rgt=4),
				frappe._dict(name="Overlap", parent_account=None, is_group=True, lft=1, rgt=4),
			],
		):
			with self.subTest(roots=roots), duckdb.connect(":memory:") as conn:
				conn.execute(
					'CREATE TABLE "tabGL Entry" (company VARCHAR, is_cancelled INTEGER, account VARCHAR, '
					"voucher_type VARCHAR, posting_date DATE, debit DOUBLE, credit DOUBLE, "
					"debit_in_account_currency DOUBLE, credit_in_account_currency DOUBLE, "
					"account_currency VARCHAR, finance_book VARCHAR)"
				)
				accounts = [
					*roots,
					frappe._dict(name="Income", parent_account="Root", is_group=False, lft=2, rgt=3),
				]
				with self.assertRaises(frappe.ValidationError):
					report._load_gl_entries_duckdb(conn, filters, [period], accounts, {})

	def test_snapshot_currency_includes_depth_hidden_accounts(self):
		for hidden_outside_root, currency, expected in (
			(False, "USD", 50),
			(True, "USD", 90),
			(False, None, 100),
		):
			with self.subTest(hidden_outside_root=hidden_outside_root, currency=currency):
				accounts = _depth_income_accounts(hidden_outside_root)
				period = frappe._dict(
					key="jan",
					from_date=getdate("2026-01-01"),
					to_date=getdate("2026-01-31"),
					year_start_date=getdate("2026-01-01"),
					year_end_date=getdate("2026-12-31"),
				)
				filters = frappe._dict(
					company="Snapshot Test",
					presentation_currency=currency,
					period_end_date="2026-01-31",
					accumulated_values=False,
					show_zero_values=False,
				)
				with (
					duckdb.connect(":memory:") as conn,
					patch.object(report, "get_accounts", return_value=accounts),
					patch.object(report, "get_appropriate_currency", return_value=currency or "AUD"),
					patch.object(report_utils, "get_company_currency", return_value="AUD"),
					patch.object(report_utils, "get_rate_as_at", return_value=2),
					patch.object(report, "calculate_values", wraps=report.calculate_values) as calculate,
				):
					_create_snapshot_gl_table(conn)
					_insert_snapshot_currency_rows(
						conn,
						[
							("2026-01-15", "Foreign Income", 100, 90, "USD", None),
							("2026-01-15", "Hidden Income", 200, 200, "AUD", None),
						],
					)
					result = report._get_data_duckdb(conn, filters, "Income", "Credit", [period])
				self.assertEqual({"Foreign Income"}, set(calculate.call_args.args[1]))
				self.assertNotIn("Hidden Income", {row.get("account") for row in result})
				foreign = next(row for row in result if row.get("account") == "Foreign Income")
				self.assertEqual(foreign["jan"], expected)
				self.assertEqual(foreign["total"], expected)
				self.assertEqual(result[-2]["total"], expected)

	def _snapshot_gl_values(self, dimension=None, accumulated=False, selected_project=None):
		periods = [
			frappe._dict(key="jan", from_date=getdate("2026-01-01"), to_date=getdate("2026-01-31")),
			frappe._dict(key="feb", from_date=getdate("2026-02-01"), to_date=getdate("2026-02-28")),
		]
		for period in periods:
			period.year_start_date = getdate("2026-01-01")
		if dimension:
			periods = [
				frappe._dict(
					period, key=f"{value}_{period.key}", dimension_field=dimension, dimension_value=value
				)
				for value in ("A", "B's Center")
				for period in periods
			]
		filters = frappe._dict(
			company="Snapshot Test",
			accumulated_values=accumulated,
			group_by_dimension={"cost_center": "Cost Center", "project": "Project"}.get(dimension),
			project=selected_project,
		)
		with duckdb.connect(":memory:") as conn:
			_create_snapshot_gl_table(conn)
			_seed_snapshot_gl_rows(conn)
			entries = {}
			report._load_gl_entries_duckdb(
				conn, filters, periods, [frappe._dict(name="Income", is_group=False)], entries
			)
		account = frappe._dict(name="Income")
		calculate_values({"Income": account}, entries, periods, accumulated, False)
		values = [-account.get(period.key, 0) for period in periods]
		total = -sum(account.get(key, 0) for key in get_period_keys_for_total(periods, accumulated))
		return values, total

	def test_snapshot_gl_dimension_totals(self):
		for dimension in (None, "cost_center", "project"):
			for accumulated in (False, True):
				with self.subTest(dimension=dimension, accumulated=accumulated):
					values, total = self._snapshot_gl_values(dimension, accumulated)
					expected = [150, 425 if accumulated else 275]
					if dimension:
						expected = [100, 300 if accumulated else 200, 50, 125 if accumulated else 75]
					self.assertEqual(values, expected)
					self.assertEqual(total, 425)

	def test_snapshot_gl_dimension_project_filter(self):
		for accumulated in (False, True):
			with self.subTest(accumulated=accumulated):
				values, total = self._snapshot_gl_values("project", accumulated, ["A"])
				self.assertEqual(values, [100, 300 if accumulated else 200, 0, 0])
				self.assertEqual(total, 300)

	def test_snapshot_gl_invalid_dimension(self):
		period = frappe._dict(
			from_date=getdate("2026-01-01"),
			to_date=getdate("2026-01-31"),
			dimension_field='cost_center" OR 1=1 --',
			dimension_value="A",
		)
		filters = frappe._dict(company="Snapshot Test", group_by_dimension="Cost Center")
		with duckdb.connect(":memory:") as conn, self.assertRaises(frappe.ValidationError):
			report._load_gl_entries_duckdb(
				conn, filters, [period], [frappe._dict(name="Income", is_group=False)], {}
			)

	def test_snapshot_gl_missing_dimension_column_raises(self):
		period = frappe._dict(
			from_date=getdate("2026-01-01"),
			to_date=getdate("2026-01-31"),
			dimension_field="cost_center",
			dimension_value="A",
		)
		filters = frappe._dict(company="Snapshot Test", group_by_dimension="Cost Center")
		with duckdb.connect(":memory:") as conn:
			conn.execute(
				'CREATE TABLE "tabGL Entry" (company VARCHAR, is_cancelled INTEGER, account VARCHAR, '
				"voucher_type VARCHAR, posting_date DATE, debit DOUBLE, credit DOUBLE, "
				"debit_in_account_currency DOUBLE, credit_in_account_currency DOUBLE, "
				"account_currency VARCHAR, finance_book VARCHAR)"
			)
			with self.assertRaises(duckdb.BinderException):
				report._load_gl_entries_duckdb(
					conn, filters, [period], [frappe._dict(name="Income", is_group=False)], {}
				)

	def test_snapshot_margin_uses_period_values(self):
		periods = [frappe._dict(key="p1"), frappe._dict(key="p2")]
		for accumulated in (False, True):
			with self.subTest(accumulated_values=accumulated):
				income = {"account_name": frappe._("Income"), "p1": 100, "p2": 300 if accumulated else 200}
				expense = {"account_name": "Expense", "p1": 25, "p2": 125 if accumulated else 100}
				profit = {"account_name": "Net Profit", "p1": 75, "p2": 175 if accumulated else 100}
				filters = frappe._dict(
					company="Snapshot Test",
					periodicity="Monthly",
					presentation_currency="AUD",
					selected_view="Margin",
					accumulated_values=accumulated,
				)
				columns, chart, summary = [], {}, []
				with (
					patch("frappe.database.duckdb.database.get_latest_sync", return_value=Mock()),
					patch.object(report, "build_period_list", return_value=periods),
					patch.object(report, "_get_data_duckdb", side_effect=[[income], [expense]]),
					patch.object(report, "get_net_profit_loss", return_value=profit),
					patch.object(report, "get_columns", return_value=columns),
					patch.object(report, "get_chart_data", return_value=chart),
					patch.object(report, "get_report_summary", return_value=(summary, 0)),
					patch.object(
						report, "compute_margin_view_data", wraps=report.compute_margin_view_data
					) as margin,
				):
					result = report.execute_snapshot_report(filters)
					margin.assert_called_once_with(result[1], periods)
				self.assertEqual(result, (columns, [income, expense, profit], None, chart, summary, 0))
				self.assertIs(result[1][0], income)
				self.assertEqual([income["p1"], income["p2"]], [100, 100])
				self.assertEqual([expense["p1"], expense["p2"]], [25, 41.67 if accumulated else 50])
				self.assertEqual([profit["p1"], profit["p2"]], [75, 58.33 if accumulated else 50])

	def test_margin_missing_and_zero_income(self):
		periods = [frappe._dict(key="p1"), frappe._dict(key="p2")]
		data = [
			{"account_name": frappe._("Income"), "p1": 0, "p2": None},
			{"account_name": "Expense", "p1": 0, "p2": 2},
			{"account_name": "Net Profit", "p1": 7, "p2": None},
		]
		self.assertIsNone(report.compute_margin_view_data(data, periods))
		self.assertEqual([[row["p1"], row["p2"]] for row in data], [[0, None], [0, None], [None, None]])
		missing_income = [{"account_name": "Expense", "p1": 25}]
		self.assertIsNone(report.compute_margin_view_data(missing_income, periods))
		self.assertEqual(missing_income, [{"account_name": "Expense", "p1": 25}])
		empty_periods = [{"account_name": frappe._("Income"), "p1": 100}]
		self.assertIsNone(report.compute_margin_view_data(empty_periods, []))
		self.assertEqual(empty_periods, [{"account_name": frappe._("Income"), "p1": 100}])

	def test_snapshot_net_profit_total_uses_accumulated_period_keys(self):
		cases = (
			(False, False, [100, 200], [25, 100], 175),
			(False, True, [100, 300], [25, 125], 175),
			(True, False, [100, 200, 200, 400], [25, 100, 50, 150], 575),
			(True, True, [100, 300, 200, 600], [25, 125, 50, 300], 475),
		)
		for dimension, accumulated, income_values, expense_values, expected_total in cases:
			with self.subTest(dimension=dimension, accumulated_values=accumulated):
				periods = [frappe._dict(key=f"p{i}", label=f"P{i}") for i in range(len(income_values))]
				if dimension:
					for i, period in enumerate(periods):
						period.update(dimension_field="cost_center", dimension_value="A" if i < 2 else "B")
				income = {period.key: value for period, value in zip(periods, income_values, strict=True)}
				expense = {period.key: value for period, value in zip(periods, expense_values, strict=True)}
				income_rows = [{"account_name": frappe._("Income"), **income}, income.copy(), {}]
				expense_rows = [{"account_name": "Expense", **expense}, expense.copy(), {}]
				filters = frappe._dict(
					company="Snapshot Test",
					periodicity="Monthly",
					presentation_currency="AUD",
					selected_view="Report",
					accumulated_values=accumulated,
				)
				with (
					patch("frappe.database.duckdb.database.get_latest_sync", return_value=Mock()),
					patch.object(report, "build_period_list", return_value=periods),
					patch.object(report, "_get_data_duckdb", side_effect=[income_rows, expense_rows]),
					patch.object(report, "get_chart_data", return_value={}),
					patch.object(report, "get_report_summary", return_value=([], 0)),
				):
					result = report.execute_snapshot_report(filters)
				self.assertEqual(result[1][-1]["total"], expected_total)

	def test_snapshot_special_views_omit_total_column(self):
		for dimension, accumulated in ((False, False), (True, True)):
			for view in ("Report", "Margin", "Growth"):
				with self.subTest(dimension=dimension, selected_view=view):
					income_values = [100, 300, 200, 600] if dimension else [100, 200]
					expense_values = [25, 125, 50, 300] if dimension else [25, 100]
					periods = [frappe._dict(key=f"p{i}", label=f"P{i}") for i in range(len(income_values))]
					if dimension:
						for i, period in enumerate(periods):
							period.update(
								dimension_field="cost_center", dimension_value="A" if i < 2 else "B"
							)
					income = {period.key: value for period, value in zip(periods, income_values, strict=True)}
					expense = {
						period.key: value for period, value in zip(periods, expense_values, strict=True)
					}
					income_rows = [{"account_name": frappe._("Income"), **income}, income.copy(), {}]
					expense_rows = [{"account_name": "Expense", **expense}, expense.copy(), {}]
					filters = frappe._dict(
						company="Snapshot Test",
						periodicity="Monthly",
						presentation_currency="AUD",
						selected_view=view,
						accumulated_values=accumulated,
					)
					with (
						patch("frappe.database.duckdb.database.get_latest_sync", return_value=Mock()),
						patch.object(report, "build_period_list", return_value=periods),
						patch.object(report, "_get_data_duckdb", side_effect=[income_rows, expense_rows]),
						patch.object(report, "get_chart_data", return_value={}),
						patch.object(report, "get_report_summary", return_value=([], 0)),
						patch.object(report, "compute_growth_view_data"),
					):
						result = report.execute_snapshot_report(filters)
					self.assertEqual(result[0][0]["fieldname"], "account")
					self.assertEqual(
						"total" in {column["fieldname"] for column in result[0]}, view == "Report"
					)
					self.assertEqual(result[1][-1]["total"], 475 if dimension else 175)
					if view == "Margin":
						self.assertEqual(result[1][0]["p0"], 100)
						self.assertEqual(result[1][-1]["p0"], 75)

	def setUp(self):
		self.company = "_Test Company"
		self.customer = "_Test Customer"
		self.item = "_Test Item"
		self.debit_to = "Debtors - _TC"
		self.cost_center = "Main - _TC"

	def create_sales_invoice(self, qty=1, rate=150, no_payment_schedule=False, do_not_submit=False):
		frappe.set_user("Administrator")
		si = create_sales_invoice(
			item=self.item,
			company=self.company,
			customer=self.customer,
			debit_to=self.debit_to,
			posting_date=today(),
			parent_cost_center=self.cost_center,
			cost_center=self.cost_center,
			rate=rate,
			price_list_rate=rate,
			qty=qty,
			do_not_save=1,
		)
		si = si.save()
		if not do_not_submit:
			si = si.submit()
		return si

	def get_fiscal_year(self):
		active_fy = frappe.db.get_all(
			"Fiscal Year",
			filters={"disabled": 0, "year_start_date": ("<=", today()), "year_end_date": (">=", today())},
		)[0]
		return frappe.get_doc("Fiscal Year", active_fy.name)

	def get_report_filters(self):
		fy = self.get_fiscal_year()
		return frappe._dict(
			company=self.company,
			from_fiscal_year=fy.name,
			to_fiscal_year=fy.name,
			period_start_date=fy.year_start_date,
			period_end_date=fy.year_end_date,
			filter_based_on="Fiscal Year",
			periodicity="Monthly",
			accumulated_values=False,
		)

	def _create_cost_center(self, name):
		parent = frappe.db.get_value("Cost Center", self.cost_center, "parent_cost_center")
		cc = frappe.new_doc("Cost Center")
		cc.cost_center_name = name
		cc.parent_cost_center = parent
		cc.company = self.company
		cc.insert()
		return cc.name

	def test_group_by_dimension(self):
		second_cc = self._create_cost_center("P&L Test CC 2")

		# 100 to default cost center, 200 to second cost center
		self.create_sales_invoice(rate=100)
		si2 = create_sales_invoice(
			item=self.item,
			company=self.company,
			customer=self.customer,
			debit_to=self.debit_to,
			posting_date=today(),
			parent_cost_center=second_cc,
			cost_center=second_cc,
			rate=200,
			price_list_rate=200,
			qty=1,
		)
		si2.submit()

		filters = self.get_report_filters()
		filters.group_by_dimension = "Cost Center"

		period_list = build_period_list(filters)
		self.assertTrue(is_dimension_grouped(period_list))

		posting_date = getdate()

		def key_for(cost_center):
			return next(
				p.key
				for p in period_list
				if p.dimension_value == cost_center and p.from_date <= posting_date <= p.to_date
			)

		columns, data, *_ = execute(filters)
		self.assertLessEqual({self.cost_center, second_cc}, {c.get("dimension_value") for c in columns})

		income_account = frappe.db.get_value("Company", self.company, "default_income_account")
		income_row = next((r for r in data if r.get("account") == income_account), None)
		self.assertIsNotNone(income_row)

		cc1_key, cc2_key = key_for(self.cost_center), key_for(second_cc)
		self.assertEqual(income_row[cc1_key], 100)
		self.assertEqual(income_row[cc2_key], 200)

		# no leakage into other dimension or period columns
		for period in period_list:
			if period.key not in (cc1_key, cc2_key):
				self.assertEqual(income_row[period.key], 0)

		# non-accumulated: total = sum of all dimension-period values
		self.assertEqual(income_row["total"], 300.0)

		# accumulated: total must take each dimension's last running balance once,
		# not sum every accumulated column
		filters.accumulated_values = True
		data = execute(filters)[1]
		income_row = next(r for r in data if r.get("account") == income_account)
		self.assertEqual(income_row["total"], 300.0)

	def test_profit_and_loss_output_and_summary(self):
		self.create_sales_invoice(qty=1, rate=150)

		filters = self.get_report_filters()
		period_list = get_period_list(
			filters.from_fiscal_year,
			filters.to_fiscal_year,
			filters.period_start_date,
			filters.period_end_date,
			filters.filter_based_on,
			filters.periodicity,
			company=filters.company,
		)

		result = execute(filters)[1]
		current_period = next(x for x in period_list if x.from_date <= getdate() and x.to_date >= getdate())
		current_period_key = current_period.key
		without_current_period = [x for x in period_list if x.key != current_period.key]
		# all period except current period(whence invoice was posted), should be '0'
		for acc in result:
			if acc:
				with self.subTest(acc=acc):
					for period in without_current_period:
						self.assertEqual(acc[period.key], 0)

		for acc in result:
			if acc:
				with self.subTest(current_period_key=current_period_key):
					self.assertEqual(acc[current_period_key], 150)
					self.assertEqual(acc["total"], 150)

	def test_p_and_l_export(self):
		self.create_sales_invoice(qty=1, rate=150)

		filters = self.get_report_filters()
		frappe.local.form_dict = frappe._dict(
			{
				"report_name": "Profit and Loss Statement",
				"file_format_type": "CSV",
				"filters": filters,
				"visible_idx": [0, 1, 2, 3, 4, 5, 6],
			}
		)
		export_query()
		contents = frappe.response["filecontent"].decode()
		sales_account = frappe.db.get_value("Company", self.company, "default_income_account")

		self.assertIn(sales_account, contents)

	def test_accumulate_filter(self):
		# ensure 2 fiscal years
		cur_fy = self.get_fiscal_year()
		find_for = add_days(cur_fy.year_start_date, -1)
		_x = frappe.db.get_all(
			"Fiscal Year",
			filters={"disabled": 0, "year_start_date": ("<=", find_for), "year_end_date": (">=", find_for)},
		)[0]
		prev_fy = frappe.get_doc("Fiscal Year", _x.name)
		prev_fy.append("companies", {"company": self.company})
		prev_fy.save()

		# make SI on both of them
		prev_fy_si = self.create_sales_invoice(qty=1, rate=450, do_not_submit=True)
		prev_fy_si.posting_date = add_days(prev_fy.year_end_date, -1)
		prev_fy_si.save().submit()
		income_acc = prev_fy_si.items[0].income_account

		self.create_sales_invoice(qty=1, rate=120)

		# Unaccumualted
		filters = frappe._dict(
			company=self.company,
			from_fiscal_year=prev_fy.name,
			to_fiscal_year=cur_fy.name,
			period_start_date=prev_fy.year_start_date,
			period_end_date=cur_fy.year_end_date,
			filter_based_on="Date Range",
			periodicity="Yearly",
			accumulated_values=False,
		)
		result = execute(filters)
		columns = [result[0][4], result[0][5]]
		expected = {
			"account": income_acc,
			columns[0].get("fieldname"): 450.0,
			columns[1].get("fieldname"): 120.0,
		}
		actual = [x for x in result[1] if x.get("account") == income_acc]
		self.assertEqual(len(actual), 1)
		actual = actual[0]
		for key in expected.keys():
			with self.subTest(key=key):
				self.assertEqual(expected.get(key), actual.get(key))

		# accumualted
		filters.update({"accumulated_values": True})
		expected = {
			"account": income_acc,
			columns[0].get("fieldname"): 450.0,
			columns[1].get("fieldname"): 570.0,
		}
		result = execute(filters)
		columns = [result[0][4], result[0][5]]
		actual = [x for x in result[1] if x.get("account") == income_acc]
		self.assertEqual(len(actual), 1)
		actual = actual[0]
		for key in expected.keys():
			with self.subTest(key=key):
				self.assertEqual(expected.get(key), actual.get(key))
