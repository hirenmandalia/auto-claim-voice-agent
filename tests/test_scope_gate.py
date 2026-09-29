"""The auto-only scope gate is deterministic, so it is tested without any model calls."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "live_demo"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from live_tools import apply_scope_gate, summarize_workflow_for_voice  # noqa: E402


def workflow(claim_type, line, description="Something happened", route="needs_docs"):
    return {
        "normalized_claim": {"loss_description": description},
        "claim_classification": {"claim_type": claim_type, "likely_policy_line": line, "severity": "low"},
        "fraud_safety_gate": {"final_routing_decision": route, "audit_trail": []},
        "field_validation": {"missing_fields": []},
        "document_checklist": {"items": []},
        "claim_intake_packet": {"claimant_next_message": "", "adjuster_handoff_summary": ""},
    }


def route_of(result):
    return result["fraud_safety_gate"]["final_routing_decision"]


class ScopeGateTests(unittest.TestCase):
    def test_non_auto_claims_are_out_of_scope(self):
        for claim_type, line in [
            ("home_water_damage", "Homeowners"),
            ("health_medical_reimbursement", "Health"),
            ("travel_delay_cancellation", "Travel"),
            ("theft_property_loss", "Renters property"),
        ]:
            with self.subTest(claim_type=claim_type):
                self.assertEqual(route_of(apply_scope_gate(workflow(claim_type, line))), "out_of_scope")

    def test_auto_claims_keep_their_route(self):
        for claim_type, line in [
            ("auto_collision", "Personal auto"),
            ("theft_property_loss", "Personal auto"),
            ("other", "Auto comprehensive"),
        ]:
            with self.subTest(claim_type=claim_type):
                self.assertEqual(route_of(apply_scope_gate(workflow(claim_type, line))), "needs_docs")

    def test_no_loss_described_yet_is_left_alone(self):
        result = apply_scope_gate(workflow("home_water_damage", "Homeowners", description="not specified"))
        self.assertEqual(route_of(result), "needs_docs")

    def test_emergency_keeps_priority_over_scope(self):
        result = apply_scope_gate(workflow("home_water_damage", "Homeowners", route="emergency_escalation"))
        self.assertEqual(route_of(result), "emergency_escalation")

    def test_gate_writes_an_audit_entry(self):
        result = apply_scope_gate(workflow("home_water_damage", "Homeowners"))
        self.assertTrue(any("Scope gate" in line for line in result["fraud_safety_gate"]["audit_trail"]))

    def test_voice_summary_tells_the_agent_to_decline(self):
        gated = apply_scope_gate(workflow("home_water_damage", "Homeowners"))
        summary = summarize_workflow_for_voice(gated)
        self.assertTrue(summary["out_of_scope"])
        self.assertIn("only accepts auto claims", summary["instruction"])


if __name__ == "__main__":
    unittest.main()
