from unittest.mock import call, patch

import frappe

import erpnext
from erpnext.patches.v10_0 import fichier_des_ecritures_comptables_for_france as france_patch
from erpnext.tests.utils import ERPNextTestSuite


@erpnext.allow_regional
def test_method():
	return "original"


class TestInit(ERPNextTestSuite):
	def test_regional_overrides(self):
		frappe.flags.country = "Maldives"
		self.assertEqual(test_method(), "original")

	def test_france_fec_patch_passes_country_to_fixture_installer(self):
		companies = [frappe._dict(name="Company A"), frappe._dict(name="Company B")]
		with (
			patch.object(france_patch.frappe, "get_all", return_value=companies) as get_all,
			patch.object(france_patch.frappe, "reload_doc") as reload_doc,
			patch.object(france_patch, "install_country_fixtures", autospec=True) as install,
		):
			france_patch.execute()

		get_all.assert_called_once_with("Company", filters={"country": "France"})
		reload_doc.assert_called_once_with("regional", "report", "fichier_des_ecritures_comptables_[fec]")
		self.assertEqual(install.call_args_list, [call("Company A", "France"), call("Company B", "France")])

	def test_france_fec_patch_with_no_france_companies(self):
		with (
			patch.object(france_patch.frappe, "get_all", return_value=[]) as get_all,
			patch.object(france_patch.frappe, "reload_doc") as reload_doc,
			patch.object(france_patch, "install_country_fixtures", autospec=True) as install,
		):
			france_patch.execute()

		get_all.assert_called_once_with("Company", filters={"country": "France"})
		reload_doc.assert_called_once_with("regional", "report", "fichier_des_ecritures_comptables_[fec]")
		install.assert_not_called()
