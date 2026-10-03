from unittest.mock import call, patch

from erpnext.tests.utils import ERPNextTestSuite
from erpnext.utilities import get_site_info
from erpnext.utilities.activation import get_help_messages, get_level


class TestActivation(ERPNextTestSuite):
	def test_activation(self):
		site_info = {"activation": {"activation_level": 0, "sales_data": []}}
		levels = get_level(site_info)
		self.assertTrue(levels)

	def test_help_level_seven_uses_empty_seed_and_short_circuits(self):
		with (
			patch(
				"erpnext.utilities.activation.get_level", autospec=True, return_value={"activation_level": 7}
			) as level,
			patch("erpnext.utilities.activation.erpnext.get_default_company") as company,
			patch("erpnext.utilities.activation.frappe.get_cached_value") as domain,
			patch("erpnext.utilities.activation.frappe.db.count") as count,
		):
			self.assertEqual(get_help_messages(), [])
			level.assert_called_once_with({})
			company.assert_not_called()
			domain.assert_not_called()
			count.assert_not_called()

	def test_help_level_six_continues_to_message_counts(self):
		with (
			patch(
				"erpnext.utilities.activation.get_level", autospec=True, return_value={"activation_level": 6}
			) as level,
			patch(
				"erpnext.utilities.activation.erpnext.get_default_company", return_value="Example Company"
			) as company,
			patch("erpnext.utilities.activation.frappe.get_cached_value", return_value="Education") as domain,
			patch("erpnext.utilities.activation.frappe.db.count", return_value=3) as count,
		):
			self.assertEqual(get_help_messages(), [])
			level.assert_called_once_with({})
			company.assert_called_once_with()
			domain.assert_called_once_with("Company", "Example Company", "domain")
			count.assert_called_once_with("Employee")

	def test_help_matching_domain_filters_targets_and_preserves_order(self):
		counts = {"Lead": 2, "Quotation": 3, "Sales Order": 3, "Purchase Order": 2, "User": 3, "Employee": 2}
		with (
			patch(
				"erpnext.utilities.activation.get_level", autospec=True, return_value={"activation_level": 6}
			),
			patch("erpnext.utilities.activation.erpnext.get_default_company", return_value="Example Company"),
			patch("erpnext.utilities.activation.frappe.get_cached_value", return_value="Manufacturing"),
			patch("erpnext.utilities.activation.frappe.db.count", side_effect=counts.__getitem__) as count,
		):
			messages = get_help_messages()
			self.assertEqual(
				[(message.doctype, message.route, message.target, message.count) for message in messages],
				[
					("Lead", "List/Lead", 3, 2),
					("Purchase Order", "List/Purchase Order", 3, 2),
					("Employee", "List/Employee", 3, 2),
				],
			)
			self.assertEqual(count.call_args_list, [call(doctype) for doctype in counts])

	def test_help_unmatched_domain_keeps_only_unrestricted_message(self):
		with (
			patch(
				"erpnext.utilities.activation.get_level", autospec=True, return_value={"activation_level": 6}
			),
			patch("erpnext.utilities.activation.erpnext.get_default_company", return_value="Example Company"),
			patch("erpnext.utilities.activation.frappe.get_cached_value", return_value="Education"),
			patch(
				"erpnext.utilities.activation.frappe.db.count", side_effect={"Employee": 2}.__getitem__
			) as count,
		):
			messages = get_help_messages()
			self.assertEqual(
				[(message.doctype, message.route, message.target, message.count) for message in messages],
				[("Employee", "List/Employee", 3, 2)],
			)
			count.assert_called_once_with("Employee")

	def test_get_level_empty_seed_with_fabricated_dependencies(self):
		for asset_count, expected_level in ((5, 3), (6, 4)):
			with (
				self.subTest(asset_count=asset_count),
				patch(
					"erpnext.utilities.activation.frappe.db.count",
					side_effect=lambda doctype, *args: {"Asset": asset_count, "Communication": 11}.get(
						doctype, 0
					),
				) as count,
				patch(
					"erpnext.utilities.activation.get_setup_wizard_completed_apps", return_value=["erpnext"]
				),
				patch("erpnext.utilities.activation.frappe.db.exists", return_value=True) as exists,
				patch(
					"erpnext.utilities.activation.frappe.utils.now_datetime",
					return_value="2026-01-03 12:00:00",
				),
				patch(
					"erpnext.utilities.activation.frappe.utils.add_to_date",
					return_value="2026-01-01 12:00:00",
				) as cutoff,
			):
				seed = {}
				result = get_level(seed)
				self.assertEqual(result["activation_level"], expected_level)
				self.assertEqual(len(result["sales_data"]), 25)
				self.assertEqual(result["sales_data"][0], {"Asset": asset_count})
				self.assertEqual(result["sales_data"][-1], {"Communication": 11})
				self.assertEqual(seed, {})
				self.assertEqual(count.call_count, 25)
				self.assertEqual(count.call_args, call("Communication", {"communication_medium": "Email"}))
				cutoff.assert_called_once_with("2026-01-03 12:00:00", days=-2)
				exists.assert_called_once_with("User", {"last_login": [">", "2026-01-01 12:00:00"]})

	def test_get_level_preserves_seeded_telemetry_accumulator(self):
		sales_data = [{"Prior Hook": 9}]
		opaque = object()
		activation = {"activation_level": 4, "sales_data": sales_data}
		site_info = {"activation": activation, "opaque": opaque}
		with (
			patch("erpnext.utilities.activation.frappe.db.count", return_value=0),
			patch("erpnext.utilities.activation.get_setup_wizard_completed_apps", return_value=[]),
			patch("erpnext.utilities.activation.frappe.db.exists", return_value=False),
			patch(
				"erpnext.utilities.activation.frappe.utils.now_datetime", return_value="2026-01-03 12:00:00"
			),
			patch(
				"erpnext.utilities.activation.frappe.utils.add_to_date", return_value="2026-01-01 12:00:00"
			),
		):
			result = get_level(site_info)
		self.assertEqual(result["activation_level"], 4)
		self.assertIs(result["sales_data"], sales_data)
		self.assertEqual(sales_data[0], {"Prior Hook": 9})
		self.assertEqual(len(sales_data), 26)
		self.assertEqual(sales_data[-1], {"Communication": 0})
		self.assertIs(site_info["activation"], activation)
		self.assertEqual(activation["activation_level"], 4)
		self.assertIs(site_info["opaque"], opaque)

	def test_get_site_info_forwards_identical_site_info(self):
		site_info = {"activation": {"activation_level": 4, "sales_data": [{"Prior Hook": 9}]}}
		activation = {"activation_level": 5, "sales_data": []}
		with (
			patch("erpnext.utilities.get_level", autospec=True, return_value=activation) as level,
			patch("erpnext.utilities.frappe.db.get_single_value", return_value="Example Company") as company,
			patch("erpnext.utilities.frappe.get_cached_value", return_value="Manufacturing") as domain,
		):
			result = get_site_info(site_info)
		self.assertIs(level.call_args.args[0], site_info)
		self.assertEqual(level.call_count, 1)
		self.assertIs(result["activation"], activation)
		self.assertEqual(result["company"], "Example Company")
		self.assertEqual(result["domain"], "Manufacturing")
		company.assert_called_once_with("Global Defaults", "default_company")
		domain.assert_called_once_with("Company", "Example Company", "domain")
