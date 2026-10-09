# Copyright (c) 2023, Frappe Technologies Pvt. Ltd. and Contributors
# MIT License. See license.txt

from unittest.mock import patch

import frappe
from frappe.desk.query_report import export_query
from frappe.utils import add_days, getdate, today

from erpnext.accounts.doctype.sales_invoice.test_sales_invoice import create_sales_invoice
from erpnext.accounts.report.financial_statements import (
	build_period_list,
	get_period_list,
	is_dimension_grouped,
)
from erpnext.accounts.report.profit_and_loss_statement import profit_and_loss_statement as report
from erpnext.accounts.report.profit_and_loss_statement.profit_and_loss_statement import execute
from erpnext.accounts.test.accounts_mixin import AccountsTestMixin
from erpnext.tests.utils import ERPNextTestSuite


class TestProfitAndLossStatement(ERPNextTestSuite, AccountsTestMixin):
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
					patch("frappe.database.duckdb.database.get_latest_sync", return_value=object()),
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
					patch("frappe.database.duckdb.database.get_latest_sync", return_value=object()),
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
						patch("frappe.database.duckdb.database.get_latest_sync", return_value=object()),
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
