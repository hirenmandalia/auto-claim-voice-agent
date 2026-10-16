"""Regression tests for every deterministic rule that bit us during phone testing of the demo.

No network. Run:  .venv/bin/python -m unittest tests.test_demo_rules -v
Each test names the incident it guards against."""
import io
import os
import unittest
from unittest.mock import patch

from PIL import Image

import policies
from live_demo import live_tools, server
from schemas import ClaimNarrative, SafetyFact


def claim(**overrides):
    base = dict(policyholder_name="Jordan Lee", policy_number="AUTO-90210", contact_method="email",
                date_of_loss="2026-10-15", loss_location="Coleman Ave, San Jose",
                loss_description="Rear-ended at a red light, dented bumper", reported_date="2026-10-15",
                raw_narrative_summary="Rear-ended at a red light on Coleman Ave, bumper dented, nobody hurt.")
    base.update(overrides)
    return ClaimNarrative(**base)


def workflow(missing=(), docs_provided=(), route="policy_review", **claim_fields):
    """Minimal workflow dict shaped like the ADK graph output, enough for summarize_workflow_for_voice."""
    items = [{"item": d, "already_provided": d in docs_provided} for d in
             ("Photos of vehicles and scene", "Police report or incident exchange form", "Other driver and witness information")]
    normalized = {"policyholder_name": "Jordan Lee", "policy_number": "AUTO-90210", "date_of_loss": "2026-10-15",
                  "loss_location": "Coleman Ave, San Jose", "contact_method": "email", "loss_description": "Rear-ended"}
    normalized.update(claim_fields)
    return {"claim_intake_packet": {"claimant_next_message": "?", "adjuster_handoff_summary": ""},
            "field_validation": {"missing_fields": list(missing)},
            "fraud_safety_gate": {"final_routing_decision": route},
            "claim_classification": {"claim_type": "auto_collision", "severity": "low"},
            "document_checklist": {"items": items},
            "normalized_claim": normalized}


class EscalationRules(unittest.TestCase):
    """Incident: 'Escalated to a person' flashed on an ordinary rear-end claim."""

    def test_uncertain_injury_does_not_escalate(self):
        c = claim(safety_facts=[SafetyFact(category="injury", status="uncertain", description="not answered yet")])
        self.assertEqual(policies._positive_safety_concerns(c), [])
        self.assertEqual(policies._uncertain_safety_concerns(c), ["not answered yet"])

    def test_present_injury_escalates(self):
        c = claim(safety_facts=[SafetyFact(category="injury", status="present", description="neck pain")])
        self.assertEqual(policies._positive_safety_concerns(c), ["neck pain"])

    def test_negated_injury_language_is_not_positive(self):
        self.assertFalse(policies._has_positive_safety_language("No one was hurt."))
        self.assertTrue(policies._has_positive_safety_language("My neck hurts"))


class ValidatorRules(unittest.TestCase):
    """Incidents: claim never closed because of 'other driver's insurance details'; 'specific email address' blocker."""

    def test_document_type_facts_do_not_block(self):
        c = claim(missing_or_uncertain_facts=["Other driver's identity and insurance details", "Police report number", "Photos of the damage"])
        self.assertEqual(policies.validate_required_claim_fields(c)["missing_fields"], [])

    def test_contact_preference_is_complete(self):
        c = claim(contact_method="email", missing_or_uncertain_facts=["Specific email address"])
        self.assertEqual(policies.validate_required_claim_fields(c)["missing_fields"], [])

    def test_real_core_gap_still_blocks(self):
        c = claim(loss_location="not specified", missing_or_uncertain_facts=["Exact cause of the loss"])
        missing = policies.validate_required_claim_fields(c)["missing_fields"]
        self.assertIn("loss_location", missing)
        self.assertIn("Exact cause of the loss", missing)

    def test_auto_checklist_is_trimmed(self):
        items = [i for i, _ in policies.TYPE_REQUIRED_DOCS["auto_collision"]]
        self.assertEqual(items, ["Photos of vehicles and scene", "Police report or incident exchange form", "Other driver and witness information"])

    def test_contact_question_is_phone_or_email(self):
        self.assertIn("phone or by email", policies.BLOCKING_FIELD_QUESTIONS["contact_method"])


class SubtypeChecklist(unittest.TestCase):
    """Scenario run: a glass, hail, or deer claim must never hear about a police report or another driver."""

    @staticmethod
    def narr(desc):
        return claim(loss_description=desc, raw_narrative_summary=desc)

    def test_subtypes_detected_from_the_claimants_words(self):
        cases = {"a rock chipped my windshield on 101": "glass", "hail dented the hood and roof": "weather", "I hit a deer on Calaveras Road": "animal",
                 "scraped the bumper on a concrete light pole": "single_vehicle", "someone keyed the driver side": "vandalism",
                 "rear-ended at a red light and the driver drove off": "hit_and_run", "another car ran a red light and hit my passenger side": "two_party",
                 "a sedan clipped my rear quarter panel changing lanes": "two_party"}
        for desc, want in cases.items():
            self.assertEqual(policies.auto_subtype(self.narr(desc)), want, desc)

    def test_checklist_narrows_by_subtype(self):
        from schemas import ClaimClassification
        cls = ClaimClassification(claim_type="auto_collision", severity="low", severity_rationale="minor", likely_policy_line="Personal auto")
        docs = lambda d: [x for x, _ in policies.required_docs_for(self.narr(d), cls)]
        self.assertEqual(docs("hail dented the hood"), ["Photos of vehicles and scene"])
        self.assertEqual(docs("a rock chipped my windshield"), ["Photos of vehicles and scene"])
        self.assertEqual(docs("someone keyed my van"), ["Photos of vehicles and scene", "Police report or incident exchange form"])
        self.assertEqual(len(docs("another car ran a red light and hit me")), 3)
        self.assertEqual(len(docs("rear-ended and the driver drove off")), 3)


class ClosingGate(unittest.TestCase):
    """Incidents: closing before the checklist; closing never arriving; re-asking known facts."""

    def test_closes_when_core_known_and_docs_addressed(self):
        convo = "Agent: any photos? Claimant: no photos. Agent: police report? Claimant: no. Agent: other driver? Claimant: they drove off"
        s = live_tools.summarize_workflow_for_voice(workflow(), convo)
        self.assertTrue(s["closing_allowed"])
        self.assertEqual(s["documents_not_yet_asked_about"], [])

    def test_does_not_close_before_docs_are_mentioned(self):
        s = live_tools.summarize_workflow_for_voice(workflow(), "Agent: what happened? Claimant: a cart hit my van")
        self.assertFalse(s["closing_allowed"])
        self.assertEqual(len(s["documents_not_yet_asked_about"]), 3)

    def test_claimant_volunteering_counts_as_addressed(self):
        convo = "Claimant: no photos, no police report, and no other driver or witness"
        s = live_tools.summarize_workflow_for_voice(workflow(), convo)
        self.assertTrue(s["closing_allowed"])

    def test_third_party_open_item_does_not_block(self):
        s = live_tools.summarize_workflow_for_voice(workflow(missing=["Other driver's identity and insurance details"]),
                                                     "photos police other driver witness")
        self.assertEqual(s["core_facts_still_needed"], [])
        self.assertTrue(s["closing_allowed"])

    def test_core_open_item_blocks(self):
        s = live_tools.summarize_workflow_for_voice(workflow(missing=["date_of_loss"]), "photos police other driver witness")
        self.assertEqual(s["core_facts_still_needed"], ["date_of_loss"])
        self.assertFalse(s["closing_allowed"])

    def test_live_subtype_narrows_checklist_before_notes_catch_up(self):
        """Scenario run 3: hail and keyed-van callers were asked about police/other driver because the cached notes were a turn behind."""
        claimant = "The hailstorm last night dented the hood and roof of my van in my driveway."
        s = live_tools.summarize_workflow_for_voice(workflow(), "Agent: what happened?", claimant)
        self.assertEqual(s["documents_not_yet_asked_about"], ["Photos of vehicles and scene"])
        claimant2 = "Someone keyed the whole driver's side of my van at Safeway."
        s2 = live_tools.summarize_workflow_for_voice(workflow(), "Agent: what happened?", claimant2)
        self.assertEqual(s2["documents_not_yet_asked_about"], ["Photos of vehicles and scene", "Police report or incident exchange form"])

    def test_volunteered_answers_are_already_known(self):
        """Scenario run 3: 'no police report' volunteered up front was asked again."""
        v = live_tools.volunteered_checklist_answers("I was rear-ended, no injuries, I have photos and their insurance card, no police report, email is best")
        self.assertEqual(v["police_report"], "none, the claimant said so"); self.assertEqual(v["photos"], "the claimant has them"); self.assertEqual(v["other_driver"], "the claimant has their details")
        s = live_tools.summarize_workflow_for_voice(workflow(), "", "no police report")
        self.assertIn("police_report", s["already_known_do_not_ask_again"])

    def test_offered_photos_block_closing_until_asked_to_show(self):
        """Scenario run: the agent closed although the caller said they had photos and the other driver's card."""
        convo = "Agent: photos? police? other driver? Claimant: I have photos and their insurance card, no police report"
        s = live_tools.summarize_workflow_for_voice(workflow(), convo, "I have photos and their insurance card, no police report")
        self.assertFalse(s["closing_allowed"]); self.assertTrue(s["offered_but_not_yet_collected"])
        s2 = live_tools.summarize_workflow_for_voice(workflow(), convo + " Agent: could you turn on your camera and show me the card?", "I have photos and their insurance card, no police report")
        self.assertTrue(s2["closing_allowed"])

    def test_already_known_list_is_populated(self):
        s = live_tools.summarize_workflow_for_voice(workflow(), "")
        known = s["already_known_do_not_ask_again"]
        self.assertEqual(known["policy_number"], "AUTO-90210")
        self.assertEqual(known["date_of_loss"], "2026-10-15")
        self.assertNotIn("x", live_tools.summarize_workflow_for_voice(workflow(date_of_loss="not specified"), "")["already_known_do_not_ask_again"].get("date_of_loss", ""))


class LiveConfigRules(unittest.TestCase):
    """Incidents: Spanish/Devanagari drift, noise barge-in, double speech after tool results, sketches, 'the model' talk."""

    def test_language_pinned_for_speech_and_both_transcripts(self):
        cfg = live_tools.build_live_config()
        self.assertEqual(cfg.speech_config.language_code, "en-US")
        self.assertEqual(cfg.input_audio_transcription.language_codes, ["en-US"])
        self.assertEqual(cfg.output_audio_transcription.language_codes, ["en-US"])

    def test_voice_detection_is_low_sensitivity(self):
        aad = live_tools.build_live_config().realtime_input_config.automatic_activity_detection
        self.assertEqual(aad.start_of_speech_sensitivity.name, "START_SENSITIVITY_LOW")
        self.assertGreaterEqual(aad.silence_duration_ms, 900)

    def test_packet_sync_is_silent_other_tools_continue(self):
        self.assertEqual(live_tools.scheduling_for(urgent=False, name="sync_claim_packet").name, "SILENT")
        self.assertEqual(live_tools.scheduling_for(urgent=False, name="lookup_policy").name, "WHEN_IDLE")
        self.assertEqual(live_tools.scheduling_for(urgent=True, name="sync_claim_packet").name, "INTERRUPT")

    def test_sketch_tool_off_by_default(self):
        with patch.dict(os.environ, {"FNOL_SKETCH": ""}):
            self.assertNotIn("draw_incident_sketch", live_tools.active_tool_names())
            self.assertIn("SKETCHING IS DISABLED", live_tools.sketch_policy_instruction())

    def test_barge_in_default_on(self):
        with patch.dict(os.environ, {"FNOL_BARGE_IN": ""}):
            self.assertTrue(live_tools.barge_in_enabled())
        with patch.dict(os.environ, {"FNOL_BARGE_IN": "off"}):
            self.assertFalse(live_tools.barge_in_enabled())

    def test_instruction_contains_guardrails(self):
        text = live_tools.SYSTEM_INSTRUCTION
        for phrase in ("never talk about how you work", "Never ask for the claimant's name or policy number",
                       "already_known_do_not_ask_again", "closing_allowed", "Always speak English"):
            self.assertIn(phrase, text, phrase)


class ServerHelpers(unittest.TestCase):
    """Incidents: dark frames not flagged; 'please wait for the model'; LAN access; account context."""

    @staticmethod
    def jpeg(mean, spread=0, size=(160, 120)):
        import random
        img = Image.new("L", size)
        px = img.load(); rnd = random.Random(0)
        for y in range(size[1]):
            for x in range(size[0]):
                px[x, y] = max(0, min(255, int(rnd.gauss(mean, spread)) if spread else mean))
        buf = io.BytesIO(); img.convert("RGB").save(buf, "JPEG"); return buf.getvalue()

    def test_dark_frames_detected_bright_frames_pass(self):
        self.assertTrue(server.frame_is_dark(server.frame_stats(self.jpeg(15))))
        self.assertTrue(server.frame_is_dark(server.frame_stats(self.jpeg(70, 18))), "night shot with boosted gain")
        self.assertTrue(server.frame_is_dark(server.frame_stats(self.jpeg(85, 25))), "dim room")
        self.assertFalse(server.frame_is_dark(server.frame_stats(self.jpeg(120, 35))))
        self.assertFalse(server.frame_is_dark(server.frame_stats(self.jpeg(170, 40))))
        self.assertFalse(server.frame_is_dark(None))

    def test_meta_talk_guard(self):
        bad = ["To give the model enough time to respond to your most recent prompt, please wait patiently.",
               "This notice is app state.", "Let me process the tool call."]
        good = ["We are processing your claim now.", "I've noted the damage. Could you show me the scratch?",
                "A prompt reply from the adjuster is typical."]
        for t in bad:
            self.assertTrue(server.META_TALK.search(t), t)
        for t in good[:2]:
            self.assertFalse(server.META_TALK.search(t), t)

    def test_off_checklist_guard(self):
        """Incident: 'Do you have any receipts, estimates, or invoices related to the damage?' despite the prompt rule."""
        for t in ("Do you have any receipts, estimates, or invoices related to the damage?", "Could you share a repair estimate?", "Do you have medical records for the injury?"):
            self.assertTrue(server.OFF_CHECKLIST_ASK.search(t), t)
        for t in ("Do you have a police report or exchange form?", "Rental reimbursement is on your policy.", "I've noted the estimate you mentioned."):
            self.assertFalse(server.OFF_CHECKLIST_ASK.search(t), t)

    def test_premature_close_guard(self):
        """Incident: 'your packet is now being submitted for a human review' and 'we will get in touch' before the real closing."""
        for t in ("Your claim packet is now being submitted for a human review.", "We will get in touch with you as soon as the review is complete.", "A human reviewer will look over your policy details."):
            self.assertTrue(server.PREMATURE_CLOSE.search(t), t)
        for t in ("Your claim packet is ready, you can review it in the app, and once you submit it, an adjuster will get started.", "I'm just finishing up my notes and will confirm in a moment."):
            self.assertFalse(server.PREMATURE_CLOSE.search(t), t)

    def test_scope_decline_detected(self):
        self.assertTrue(server.SCOPE_DECLINE.search("I'm so sorry about your kitchen, but I can only take auto insurance claims here."))
        self.assertTrue(server.SCOPE_DECLINE.search("Since I can only take auto insurance claims, you'll need to contact your insurer for that homeowners policy."))
        self.assertFalse(server.SCOPE_DECLINE.search("I can only help with auto claim questions here. Are you and everyone else safe?"), "off-topic decline is not a declined claim")
        self.assertFalse(server.SCOPE_DECLINE.search("I've noted the damage to your van."))
        for t in ("I'm sorry, I still can't make out the damage. Could you get a little closer?", "Could you turn on a light?"):
            self.assertTrue(server.CAMERA_RETRY.search(t), t)
        self.assertFalse(server.CAMERA_RETRY.search("I can see the dent on the sliding door."))

    def test_silence_placeholders_and_camera_asks(self):
        """Incidents: the agent said 'No speech' aloud; 'you can provide that in the app' instead of opening the camera."""
        for t in ("No speech.", " no speech ", "No response", "[silence]", "<no speech>{pause}", "\u200b"):
            self.assertTrue(server.SILENCE_PLACEHOLDER.match(t), t)
        self.assertFalse(server.SILENCE_PLACEHOLDER.match("I can see the dent on the door."))
        for t in ("Could you show me the other driver's insurance card?", "Please turn on your camera", "Hold the form up to the camera"):
            self.assertTrue(server.CAMERA_ASK.search(t), t)
        self.assertFalse(server.CAMERA_ASK.search("Do you have a police report?"))
        self.assertIn("Never say they can provide it later", live_tools.SYSTEM_INSTRUCTION)

    def test_app_context_is_sanitized_and_used(self):
        ctx = server._clean_app_context({"policyholder_name": "Jordan <b>Lee</b>", "policy_number": "AUTO-90210", "bogus": "x", "vehicle": "2013 Toyota Sienna"})
        self.assertEqual(set(ctx), {"policyholder_name", "policy_number", "vehicle"})
        self.assertNotIn("<", ctx["policyholder_name"])
        self.assertIn("Jordan", server.greeting_event(ctx))
        self.assertIn("Do not ask for their name or policy number", server.app_context_instruction(ctx))

    def test_lan_mode_accepts_private_addresses_only_when_enabled(self):
        with patch.dict(os.environ, {"FNOL_ALLOW_LAN": "", "FNOL_TRUST_PROXY": ""}):
            self.assertFalse(server.client_ok("192.168.1.43"))
            self.assertFalse(server.local_host("192.168.1.119:4443"))
            self.assertTrue(server.client_ok("127.0.0.1"))
        with patch.dict(os.environ, {"FNOL_ALLOW_LAN": "1", "FNOL_TRUST_PROXY": ""}):
            self.assertTrue(server.client_ok("192.168.1.43"))
            self.assertTrue(server.local_host("192.168.1.119:4443"))
            self.assertFalse(server.client_ok("8.8.8.8"), "public addresses stay blocked")
            self.assertFalse(server.local_host("evil.example.com:4443"))


if __name__ == "__main__":
    unittest.main()
