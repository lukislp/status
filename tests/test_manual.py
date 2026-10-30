import unittest
from pathlib import Path

from statuspage.manual import IncidentError, load_incidents, load_manual, maintenance_state, parse_incident, phase

IDS = {"web", "app"}
INCIDENT = """---
title: App login fails
components: app, web
impact: major
---

## resolved 2026-10-30 18:40
Fixed.

## investigating 2026-10-30 18:05 UTC
We are **looking** into it.
See [details](https://example.org).
"""
MAINTENANCE = """---
title: Database upgrade
components: app
start: 2026-11-02 02:00
end: 2026-11-02 03:00
---

## scheduled 2026-10-30 10:00
We will upgrade the database.
"""


class ParseTest(unittest.TestCase):
    def test_parses_and_orders_updates(self):
        incident = parse_incident(INCIDENT, "2026-10-30-login", IDS)
        self.assertEqual(incident["components"], ["app", "web"])
        self.assertEqual([u["status"] for u in incident["updates"]], ["investigating", "resolved"])
        self.assertEqual(incident["created_at"], "2026-10-30T18:05:00Z")
        self.assertEqual(incident["resolved_at"], "2026-10-30T18:40:00Z")
        self.assertIn("**looking**", incident["updates"][0]["body"])
        self.assertFalse(incident["auto"])

    def test_unknown_component_is_rejected(self):
        with self.assertRaises(IncidentError):
            parse_incident(INCIDENT.replace("app, web", "nope"), "x", IDS)

    def test_bad_status_and_heading_are_rejected(self):
        with self.assertRaises(IncidentError):
            parse_incident(INCIDENT.replace("## resolved", "## fixed"), "x", IDS)
        with self.assertRaises(IncidentError):
            parse_incident(INCIDENT.replace("## resolved 2026-10-30 18:40", "## resolved yesterday"), "x", IDS)

    def test_missing_front_matter_is_rejected(self):
        with self.assertRaises(IncidentError):
            parse_incident("## investigating 2026-10-30 18:05\nx", "x", IDS)

    def test_bad_file_name_is_rejected(self):
        with self.assertRaises(IncidentError):
            parse_incident(INCIDENT, "Bad Name", IDS)

    def test_maintenance_requires_window_and_maintenance_statuses(self):
        incident = parse_incident(MAINTENANCE, "db", IDS, "maintenance")
        self.assertEqual(incident["impact"], "maintenance")
        self.assertEqual(incident["scheduled_start"], "2026-11-02T02:00:00Z")
        with self.assertRaises(IncidentError):
            parse_incident(MAINTENANCE.replace("end: 2026-11-02 03:00\n", ""), "db", IDS, "maintenance")
        with self.assertRaises(IncidentError):
            parse_incident(MAINTENANCE.replace("## scheduled", "## investigating"), "db", IDS, "maintenance")
        with self.assertRaises(IncidentError):
            parse_incident(MAINTENANCE.replace("end: 2026-11-02 03:00", "end: 2026-11-02 01:00"), "db", IDS, "maintenance")

    def test_maintenance_belongs_in_its_own_folder(self):
        with self.assertRaises(IncidentError):
            parse_incident(MAINTENANCE, "db", IDS)  # start/end are not incident keys
        with self.assertRaises(IncidentError):
            parse_incident(INCIDENT.replace("impact: major", "impact: maintenance"), "x", IDS)


class PhaseTest(unittest.TestCase):
    def test_incident_phases(self):
        incident = parse_incident(INCIDENT, "x", IDS)
        self.assertEqual(phase(incident, "2026-10-30T19:00:00Z"), "past")
        incident["resolved_at"] = None
        self.assertEqual(phase(incident, "2026-10-30T19:00:00Z"), "active")

    def test_maintenance_phases(self):
        incident = parse_incident(MAINTENANCE, "db", IDS, "maintenance")
        self.assertEqual(phase(incident, "2026-11-01T00:00:00Z"), "upcoming")
        self.assertEqual(maintenance_state(incident, "2026-11-01T00:00:00Z"), "scheduled")
        self.assertEqual(phase(incident, "2026-11-02T02:30:00Z"), "active")
        self.assertEqual(maintenance_state(incident, "2026-11-02T02:30:00Z"), "in-progress")
        self.assertEqual(phase(incident, "2026-11-02T04:00:00Z"), "past")
        self.assertEqual(maintenance_state(incident, "2026-11-02T04:00:00Z"), "completed")
        done = parse_incident(MAINTENANCE + "\n## completed 2026-11-02 02:40\nDone.\n", "db", IDS, "maintenance")
        self.assertEqual(maintenance_state(done, "2026-11-02T02:45:00Z"), "completed")


class RepositoryIncidentsTest(unittest.TestCase):
    def test_repository_incident_files_parse(self):
        from statuspage.config import all_components, load_config

        root = Path(__file__).resolve().parent.parent
        ids = {c["id"] for c in all_components(load_config(root / "config.json"))}
        load_manual(root / "incidents", root / "maintenance", ids)

    def test_template_is_valid_but_ignored(self):
        root = Path(__file__).resolve().parent.parent
        template = (root / "incidents" / "_template.md").read_text(encoding="utf-8")
        parse_incident(template, "template", {"studylife-demo"})
        template = (root / "maintenance" / "_template.md").read_text(encoding="utf-8")
        parse_incident(template, "template", {"homelabhub-demo", "piwatch-demo", "studylife-demo"}, "maintenance")

    def test_duplicate_ids_across_folders_are_rejected(self):
        import tempfile

        base = Path(tempfile.mkdtemp())
        (base / "i").mkdir()
        (base / "m").mkdir()
        (base / "i" / "same.md").write_text(INCIDENT, encoding="utf-8")
        (base / "m" / "same.md").write_text(MAINTENANCE, encoding="utf-8")
        with self.assertRaises(IncidentError):
            load_manual(base / "i", base / "m", IDS)
        self.assertEqual(load_incidents(base / "missing", IDS), [])


if __name__ == "__main__":
    unittest.main()
