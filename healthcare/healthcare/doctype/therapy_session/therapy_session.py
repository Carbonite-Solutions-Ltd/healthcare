# Copyright (c) 2020, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt


import datetime

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.model.mapper import get_mapped_doc
from frappe.utils import flt, get_link_to_form, get_time, getdate, today

from healthcare.healthcare.doctype.healthcare_settings.healthcare_settings import (
	get_income_account,
	get_receivable_account,
)
from healthcare.healthcare.doctype.nursing_task.nursing_task import NursingTask
from healthcare.healthcare.doctype.service_request.service_request import (
	set_service_request_status,
)
from healthcare.healthcare.utils import validate_nursing_tasks


class TherapySession(Document):
	def validate(self):
		self.set_exercises_from_therapy_type()
		self.validate_duplicate()
		self.set_total_counts()
		
		# Set initial status if not set
		if not self.status:
			self.status = "Not Invoiced"

	def before_submit(self):
		# Only allow submit if status is Paid
		if self.status != "Paid":
			frappe.throw(_("Cannot submit Therapy Session. Payment must be completed first. Current status: {0}").format(self.status))

	def after_insert(self):
		self.create_nursing_tasks(post_event=False)

	def on_update(self):
		if self.appointment:
			frappe.db.set_value("Patient Appointment", self.appointment, "status", "Closed")

	def on_submit(self):
		validate_nursing_tasks(self)
		self.update_sessions_count_in_therapy_plan()
		
		# Update status to Completed on submit
		self.db_set("status", "Completed", update_modified=False)

		if self.service_request:
			status = "active-Request Status"
			sessions_completed = self.check_sessions_completed()
			if sessions_completed:
				status = "completed-Request Status"

			set_service_request_status(self.service_request, status)

	def on_cancel(self):
		if self.appointment:
			frappe.db.set_value("Patient Appointment", self.appointment, "status", "Open")
		if self.service_request:
			frappe.db.set_value("Service Request", self.service_request, "status", "active-Request Status")

		self.update_sessions_count_in_therapy_plan(on_cancel=True)
		
		# Reset status to Not Invoiced on cancel
		self.db_set("status", "Not Invoiced", update_modified=False)

	def validate_duplicate(self):
		end_time = datetime.datetime.combine(
			getdate(self.start_date), get_time(self.start_time)
		) + datetime.timedelta(minutes=flt(self.duration))

		overlaps = frappe.db.sql(
			"""
		select
			name
		from
			`tabTherapy Session`
		where
			start_date=%s and name!=%s and docstatus!=2
			and (practitioner=%s or patient=%s) and
			((start_time<%s and start_time + INTERVAL duration MINUTE>%s) or
			(start_time>%s and start_time<%s) or
			(start_time=%s))
		""",
			(
				self.start_date,
				self.name,
				self.practitioner,
				self.patient,
				self.start_time,
				end_time.time(),
				self.start_time,
				end_time.time(),
				self.start_time,
			),
		)

		if overlaps:
			overlapping_details = _("Therapy Session overlaps with {0}").format(
				get_link_to_form("Therapy Session", overlaps[0][0])
			)
			frappe.throw(overlapping_details, title=_("Therapy Sessions Overlapping"))

	def create_nursing_tasks(self, post_event=True):
		template = frappe.db.get_value("Therapy Type", self.therapy_type, "nursing_checklist_template")
		if template:
			NursingTask.create_nursing_tasks_from_template(
				template,
				self,
				start_time=frappe.utils.get_datetime(f"{self.start_date} {self.start_time}"),
				post_event=post_event,
			)

	def update_sessions_count_in_therapy_plan(self, on_cancel=False):
		therapy_plan = frappe.get_doc("Therapy Plan", self.therapy_plan)
		for entry in therapy_plan.therapy_plan_details:
			if entry.therapy_type == self.therapy_type:
				if on_cancel:
					entry.sessions_completed -= 1
				else:
					entry.sessions_completed += 1
		therapy_plan.save()

	def set_total_counts(self):
		target_total = 0
		counts_completed = 0
		for entry in self.exercises:
			if entry.counts_target:
				target_total += entry.counts_target
			if entry.counts_completed:
				counts_completed += entry.counts_completed

		self.db_set("total_counts_targeted", target_total)
		self.db_set("total_counts_completed", counts_completed)

	def set_exercises_from_therapy_type(self):
		if self.therapy_type and not self.exercises:
			therapy_type_doc = frappe.get_cached_doc("Therapy Type", self.therapy_type)
			if therapy_type_doc.exercises:
				for exercise in therapy_type_doc.exercises:
					self.append(
						"exercises",
						(frappe.copy_doc(exercise)).as_dict(),
					)

	def before_insert(self):
		if self.service_request:
			therapy_session = frappe.db.exists(
				"Therapy Session",
				{"service_request": self.service_request, "docstatus": 0},
			)
			if therapy_session:
				frappe.throw(
					_("Therapy Session {0} already created from service request {1}").format(
						frappe.bold(get_link_to_form("Therapy Session", therapy_session)),
						frappe.bold(get_link_to_form("Service Request", self.service_request)),
					),
					title=_("Already Exist"),
				)

	def check_sessions_completed(self):
		total_sessions_requested = frappe.db.get_value("Service Request", self.service_request, "quantity")
		sessions = frappe.db.count(
			"Therapy Session", filters={"docstatus": ["!=", 2], "service_request": self.service_request}
		)

		return True if total_sessions_requested == sessions else False


@frappe.whitelist()
def create_therapy_session(source_name, target_doc=None):
	def set_missing_values(source, target):
		therapy_type = frappe.get_doc("Therapy Type", source.therapy_type)
		target.exercises = therapy_type.exercises

	doc = get_mapped_doc(
		"Patient Appointment",
		source_name,
		{
			"Patient Appointment": {
				"doctype": "Therapy Session",
				"field_map": [
					["appointment", "name"],
					["patient", "patient"],
					["patient_age", "patient_age"],
					["gender", "patient_sex"],
					["therapy_type", "therapy_type"],
					["therapy_plan", "therapy_plan"],
					["practitioner", "practitioner"],
					["department", "department"],
					["start_date", "appointment_date"],
					["start_time", "appointment_time"],
					["service_unit", "service_unit"],
					["company", "company"],
					["invoiced", "invoiced"],
				],
			}
		},
		target_doc,
		set_missing_values,
	)

	return doc


@frappe.whitelist()
def create_sales_invoice_for_therapy_session(therapy_session_name):
	"""
	Create Sales Invoice for Therapy Session
	Gets item from Therapy Type and uses rate from Therapy Session
	"""
	try:
		# Get therapy session
		therapy_session = frappe.get_doc("Therapy Session", therapy_session_name)
		
		# Validate status
		if therapy_session.status != "Not Invoiced":
			frappe.throw(_("Sales Invoice already created for this Therapy Session. Current status: {0}").format(therapy_session.status))
		
		# Get patient customer
		customer = frappe.db.get_value("Patient", therapy_session.patient, "customer")
		if not customer:
			frappe.throw(_("Patient {0} does not have a linked Customer").format(therapy_session.patient))
		
		# Get item from therapy type
		therapy_type_doc = frappe.get_doc("Therapy Type", therapy_session.therapy_type)
		if not hasattr(therapy_type_doc, 'item') or not therapy_type_doc.item:
			frappe.throw(_("Therapy Type {0} does not have an item configured").format(therapy_session.therapy_type))
		
		item_code = therapy_type_doc.item
		
		# Check if item exists
		if not frappe.db.exists("Item", item_code):
			frappe.throw(_("Item {0} does not exist").format(item_code))
		
		# Create Sales Invoice
		sales_invoice = frappe.new_doc("Sales Invoice")
		sales_invoice.patient = therapy_session.patient
		sales_invoice.customer = customer
		sales_invoice.company = therapy_session.company
		sales_invoice.posting_date = today()
		sales_invoice.due_date = today()
		sales_invoice.custom_therapy_session = therapy_session.name
		
		# Set custom fields if they exist
		if hasattr(sales_invoice, 'custom_invoice_from'):
			sales_invoice.custom_invoice_from = "Rehabilitation"
		
		if hasattr(sales_invoice, 'custom_therapy_session'):
			sales_invoice.custom_therapy_session = therapy_session.name
		
		# Get therapy type name
		therapy_name = therapy_type_doc.therapy_type if hasattr(therapy_type_doc, 'therapy_type') else therapy_session.therapy_type
		
		# Add item to invoice
		sales_invoice.append("items", {
			"item_code": item_code,
			"item_name": therapy_name,
			"qty": 1,
			"rate": therapy_session.rate or 0,
			"description": f"Therapy Session: {therapy_name} - {therapy_session.patient_name}"
		})
		
		# Insert and submit invoice
		sales_invoice.insert(ignore_permissions=True)
		sales_invoice.submit()
		
		# Update therapy session with invoice reference and status
		frappe.db.set_value(
			"Therapy Session",
			therapy_session.name,
			{
				"sales_invoice": sales_invoice.name,
				"status": "Pending Payment",
				"invoiced": 1
			},
			update_modified=False
		)
		
		frappe.db.commit()
		
		frappe.msgprint(
			_("Sales Invoice {0} created successfully. Status updated to Pending Payment.").format(
				frappe.bold(sales_invoice.name)
			),
			title=_("Invoice Created"),
			indicator="green",
			alert=True
		)
		
		return {
			"sales_invoice": sales_invoice.name,
			"status": "Success"
		}
		
	except Exception as e:
		frappe.log_error(
			message=frappe.get_traceback(),
			title=f"Create Sales Invoice Failed for Therapy Session {therapy_session_name}"
		)
		frappe.throw(_("Failed to create Sales Invoice: {0}").format(str(e)))


@frappe.whitelist()
def invoice_therapy_session(source_name, target_doc=None):
	"""Legacy function - kept for backward compatibility"""
	def set_missing_values(source, target):
		target.customer = frappe.db.get_value("Patient", source.patient, "customer")
		target.due_date = getdate()
		target.debit_to = get_receivable_account(source.company)
		item = target.append("items", {})
		item = get_therapy_item(source, item)
		target.set_missing_values(for_validate=True)

	doc = get_mapped_doc(
		"Therapy Session",
		source_name,
		{
			"Therapy Session": {
				"doctype": "Sales Invoice",
				"field_map": [
					["patient", "patient"],
					["referring_practitioner", "practitioner"],
					["company", "company"],
					["due_date", "start_date"],
				],
			}
		},
		target_doc,
		set_missing_values,
	)

	return doc


def get_therapy_item(therapy, item):
	item.item_code = frappe.db.get_value("Therapy Type", therapy.therapy_type, "item")
	item.description = _("Therapy Session Charges: {0}").format(therapy.practitioner)
	item.income_account = get_income_account(therapy.practitioner, therapy.company)
	item.cost_center = frappe.get_cached_value("Company", therapy.company, "cost_center")
	item.rate = therapy.rate
	item.amount = therapy.rate
	item.qty = 1
	item.reference_dt = "Therapy Session"
	item.reference_dn = therapy.name
	return item


def on_payment_entry_submit(doc, method):
	"""
	Hook to update Therapy Session status when payment is made
	This should be added to hooks.py as a Payment Entry on_submit hook
	"""
	for reference in doc.references:
		if reference.reference_doctype == "Sales Invoice":
			# Check if this invoice is linked to a therapy session
			therapy_session = frappe.db.get_value(
				"Therapy Session",
				{"sales_invoice": reference.reference_name},
				["name", "status"],
				as_dict=True
			)
			
			if therapy_session and therapy_session.status == "Pending Payment":
				# Update status to Paid
				frappe.db.set_value(
					"Therapy Session",
					therapy_session.name,
					"status",
					"Paid",
					update_modified=False
				)
				
				frappe.msgprint(
					_("Therapy Session {0} payment received. Status updated to Paid. You can now submit the session.").format(
						frappe.bold(therapy_session.name)
					),
					alert=True,
					indicator="green"
				)