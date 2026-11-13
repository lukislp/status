import json
import posixpath
import re
import string
import unittest
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

from statuspage import diagnosis, i18n, model, notify, render
from statuspage.config import ConfigError, load_config, localized, validate_config
from statuspage.manual import parse_incident

from .test_manual import INCIDENT, MAINTENANCE
from .test_model import SETTINGS, T0, config, fail, ok
from .test_notify import BASE, NAMES, outage
from .test_render import ROOT, build, sample_data

NOW = T0 + timedelta(hours=8)


def fields(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


class HeadLinks(HTMLParser):
    """Collects what a page says about its language and its other-language twin."""

    def __init__(self):
        super().__init__()
        self.lang = None
        self.canonical = None
        self.alternates = {}
        self.switch = {}  # lang -> href of the visible language switch
        self.current = None
        self.feed_href = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "html":
            self.lang = a.get("lang")
        elif tag == "link" and a.get("rel") == "canonical":
            self.canonical = a["href"]
        elif tag == "link" and a.get("rel") == "alternate" and a.get("hreflang"):
            self.alternates[a["hreflang"]] = a["href"]
        elif tag == "link" and a.get("type") == "application/atom+xml":
            self.feed_href = a["href"]
        elif tag == "a" and "lang-link" in (a.get("class") or ""):
            self.switch[a["hreflang"]] = a["href"]
        elif tag == "span" and "lang-current" in (a.get("class") or ""):
            self.current = a.get("lang")


def head(path: Path) -> HeadLinks:
    parser = HeadLinks()
    parser.feed(path.read_text(encoding="utf-8"))
    return parser


def url_to_file(root: Path, url: str, base: str = "https://status.example.org") -> Path:
    path = url.removeprefix(base).lstrip("/")
    if path == "" or path.endswith("/"):
        path += "index.html"
    return root / path


class TranslationTableTest(unittest.TestCase):
    def test_both_languages_have_the_same_keys(self):
        de, en = set(i18n.STRINGS["de"]), set(i18n.STRINGS["en"])
        self.assertEqual(sorted(en - de), [], "missing in German")
        self.assertEqual(sorted(de - en), [], "missing in English")

    def test_no_empty_texts_and_same_placeholders(self):
        for key, english in i18n.STRINGS["en"].items():
            german = i18n.STRINGS["de"][key]
            self.assertTrue(english.strip() and german.strip(), key)
            self.assertEqual(fields(english), fields(german), key)

    def test_every_status_banner_update_and_impact_has_a_text(self):
        for lang in i18n.LANGS:
            for status in model.SEVERITY + ["planned", "unknown"]:
                self.assertIn(f"status.{status}", i18n.STRINGS[lang])
            for status in model.SEVERITY:
                self.assertIn(f"banner.{status}", i18n.STRINGS[lang])
            for impact in model.IMPACTS:
                self.assertIn(f"impact.{impact}", i18n.STRINGS[lang])
            for status in ["investigating", "identified", "monitoring", "update", "resolved",
                           "scheduled", "in-progress", "verifying", "completed"]:
                self.assertIn(f"update.{status}", i18n.STRINGS[lang])

    def test_every_failure_phase_is_worded_in_both_languages(self):
        for phase in diagnosis.PHASES:
            self.assertEqual(i18n.t("en", f"phase.{phase}"), diagnosis.PHASE_TEXT[phase])
            self.assertTrue(i18n.t("de", f"phase.{phase}"))

    def test_german_is_the_default_language(self):
        self.assertEqual(i18n.DEFAULT_LANG, "de")
        self.assertEqual(render.lang_path("de", "index.html"), "index.html")
        self.assertEqual(render.lang_path("en", "index.html"), "en/index.html")


class FormatTest(unittest.TestCase):
    def test_dates_use_each_languages_format(self):
        dt = datetime(2026, 11, 13, 21, 5, tzinfo=timezone.utc)
        self.assertEqual(i18n.fmt_dt(dt, "de"), "13.11.2026, 21:05 UTC")
        self.assertEqual(i18n.fmt_dt(dt, "en"), "Nov 13, 2026 - 21:05 UTC")
        self.assertEqual(i18n.fmt_dt(datetime(2026, 11, 3, 8, 0), "de"), "3.11.2026, 08:00 UTC")
        self.assertEqual(i18n.fmt_day(dt, "de"), "13.11.2026")
        self.assertEqual(i18n.fmt_day(dt, "en"), "Nov 13, 2026")
        self.assertEqual(i18n.fmt_month(dt, "de"), "November 2026")
        self.assertEqual(i18n.fmt_month(datetime(2026, 3, 1), "de"), "März 2026")
        self.assertEqual(i18n.fmt_short_date(datetime(2026, 11, 2), "de"), "Mo., 2. Nov.")
        self.assertEqual(i18n.fmt_short_date(datetime(2026, 11, 2), "en"), "Mon, Nov 2")

    def test_numbers_and_durations(self):
        self.assertEqual(render.fmt_uptime(99.996, "de"), "99,99 % Verfügbarkeit")
        self.assertEqual(render.fmt_uptime(100.0, "de"), "100,0 % Verfügbarkeit")
        self.assertEqual(render.fmt_uptime(None, "de"), "Noch keine Daten")
        self.assertEqual(i18n.fmt_duration(15, "de"), "15 Min.")
        self.assertEqual(i18n.fmt_duration(135, "de"), "2 Std. 15 Min.")
        self.assertEqual(i18n.fmt_duration(60 * 72, "de"), "3 Tage")
        self.assertEqual(i18n.fmt_duration(135, "en"), "2 h 15 min")
        self.assertEqual(model.format_duration(135), "2 h 15 min")

    def test_pages_show_the_dates_of_their_language(self):
        out = build(sample_data(), [], NOW)
        de = (out / "index.html").read_text(encoding="utf-8")
        en = (out / "en" / "index.html").read_text(encoding="utf-8")
        self.assertIn("30.10.2026, 12:20 UTC", de)
        self.assertIn("Oct 30, 2026 - 12:20 UTC", en)
        self.assertNotIn("Oct 30, 2026", de)
        self.assertNotIn("30.10.2026", en)


class LanguageTreeTest(unittest.TestCase):
    def setUp(self):
        manual = [parse_incident(INCIDENT, "2026-10-30-login", {"web", "app"}),
                  parse_incident(MAINTENANCE, "db-upgrade", {"web", "app"}, "maintenance")]
        self.root = build(sample_data(), manual, NOW)
        self.pages = sorted(p for p in self.root.rglob("*.html") if p.name != "404.html")

    def test_both_trees_exist_for_every_page_kind(self):
        auto = next((self.root / "incidents").glob("auto-web-*.html")).name
        for name in ["index.html", "history.html", "feed.xml", "incidents/2026-10-30-login.html",
                     "incidents/db-upgrade.html", f"incidents/{auto}"]:
            self.assertTrue((self.root / name).is_file(), name)
            self.assertTrue((self.root / "en" / name).is_file(), name)
        self.assertEqual(len(self.pages), 10)

    def test_html_lang_matches_the_tree(self):
        for path in self.pages:
            expected = "en" if "en" in path.relative_to(self.root).parts[:1] else "de"
            self.assertEqual(head(path).lang, expected, path)
        self.assertEqual(head(self.root / "404.html").lang, "de")

    def test_hreflang_is_reciprocal_and_x_default_is_german(self):
        for path in self.pages:
            info = head(path)
            self.assertEqual(set(info.alternates), {"de", "en", "x-default"}, path)
            self.assertEqual(info.alternates["x-default"], info.alternates["de"], path)
            self.assertEqual(info.canonical, info.alternates[info.lang], path)
            for lang in ("de", "en"):
                target = url_to_file(self.root, info.alternates[lang])
                self.assertTrue(target.is_file(), f"{path}: {lang} -> {target}")
                twin = head(target)
                self.assertEqual(twin.lang, lang, target)
                self.assertEqual(twin.alternates, info.alternates, target)  # same set from both sides
                self.assertEqual(twin.canonical, info.alternates[lang], target)

    def test_the_language_switch_links_to_the_same_page_in_the_other_language(self):
        for path in self.pages:
            info = head(path)
            other = "en" if info.lang == "de" else "de"
            self.assertEqual(info.current, info.lang, path)
            self.assertEqual(set(info.switch), {other}, path)
            href = info.switch[other]
            target = (path.parent / href)
            target = Path(posixpath.normpath(target.as_posix()))
            if target.is_dir():
                target = target / "index.html"
            self.assertTrue(target.is_file(), f"{path}: {href}")
            self.assertEqual(head(target).lang, other)
            self.assertEqual(url_to_file(self.root, head(target).canonical), target)
            # And back again.
            back = Path(posixpath.normpath((target.parent / head(target).switch[info.lang]).as_posix()))
            if back.is_dir():
                back = back / "index.html"
            self.assertEqual(back, path)

    def test_known_switch_hrefs(self):
        self.assertEqual(head(self.root / "index.html").switch["en"], "en/")
        self.assertEqual(head(self.root / "en" / "index.html").switch["de"], "../")
        self.assertEqual(head(self.root / "history.html").switch["en"], "en/history.html")
        self.assertEqual(head(self.root / "incidents" / "db-upgrade.html").switch["en"], "../en/incidents/db-upgrade.html")
        self.assertEqual(head(self.root / "en" / "incidents" / "db-upgrade.html").switch["de"], "../../incidents/db-upgrade.html")

    def test_each_page_links_its_own_languages_feed(self):
        self.assertEqual(head(self.root / "index.html").feed_href, "feed.xml")
        self.assertEqual(head(self.root / "en" / "index.html").feed_href, "feed.xml")
        self.assertEqual(head(self.root / "incidents" / "db-upgrade.html").feed_href, "../feed.xml")
        self.assertEqual(head(self.root / "en" / "incidents" / "db-upgrade.html").feed_href, "../feed.xml")
        de = (self.root / "index.html").read_text(encoding="utf-8")
        en = (self.root / "en" / "index.html").read_text(encoding="utf-8")
        self.assertIn("Abonnieren (Atom)</span>", de)
        self.assertIn("Subscribe (Atom)</span>", en)
        self.assertNotIn("Subscribe", de)
        self.assertNotIn("Abonnieren", en)

    def test_the_feeds(self):
        de = (self.root / "feed.xml").read_text(encoding="utf-8")
        en = (self.root / "en" / "feed.xml").read_text(encoding="utf-8")
        self.assertIn('xml:lang="de"', de)
        self.assertIn('xml:lang="en"', en)
        self.assertIn('rel="self" type="application/atom+xml" href="https://status.example.org/feed.xml"', de)
        self.assertIn('rel="self" type="application/atom+xml" href="https://status.example.org/en/feed.xml"', en)
        self.assertIn("<id>https://status.example.org/incidents/db-upgrade.html</id>", de)  # unchanged ids
        self.assertIn("<id>https://status.example.org/en/incidents/db-upgrade.html</id>", en)
        self.assertIn("<title>Wartung: ", de)
        self.assertIn("<title>Maintenance: ", en)
        self.assertIn('label="Wartung"', de)
        self.assertIn('label="Störung"', de)
        self.assertIn("Web ist nicht verfügbar", de)
        self.assertIn("Web is unavailable", en)
        for name, text in (("de", de), ("en", en)):
            self.assertEqual(text.count("<entry>"), 3, name)

    def test_index_incident_and_history_are_translated(self):
        index = (self.root / "index.html").read_text(encoding="utf-8")
        for text in ("Alle Dienste sind betriebsbereit", "Betriebsbereit", "Demnächst", "Geplante Wartung",
                     "vor 90 Tagen", "Heute", "Störungshistorie", "Zuletzt geprüft",
                     "Verfügbarkeit der letzten 90 Tage."):
            self.assertIn(text, index)
        history = (self.root / "history.html").read_text(encoding="utf-8")
        self.assertIn("Störungshistorie</h1>", history)
        self.assertIn("Oktober 2026", history)
        self.assertIn("30.10.2026, 12:05 UTC bis 12:20 UTC", history)
        incident = (self.root / "incidents" / "2026-10-30-login.html").read_text(encoding="utf-8")
        self.assertIn("Aktueller Status", incident)
        self.assertIn("Veröffentlicht", incident)
        self.assertIn("Behoben", incident)
        en_history = (self.root / "en" / "history.html").read_text(encoding="utf-8")
        self.assertIn("Incident history</h1>", en_history)
        self.assertIn("October 2026", en_history)
        self.assertIn("Oct 30, 2026 - 12:05 UTC to 12:20 UTC", en_history)
        self.assertIn("Current status", (self.root / "en" / "incidents" / "2026-10-30-login.html").read_text(encoding="utf-8"))

    def test_maintenance_in_both_languages(self):
        de = (self.root / "index.html").read_text(encoding="utf-8")
        en = (self.root / "en" / "index.html").read_text(encoding="utf-8")
        self.assertIn("Mo., 2. Nov. &middot; 02:00&ndash;03:00 UTC", de)
        self.assertIn("Mon, Nov 2 &middot; 02:00&ndash;03:00 UTC", en)
        self.assertIn("(1 Std.)", de)
        self.assertIn("(1 h)", en)
        page = (self.root / "incidents" / "db-upgrade.html").read_text(encoding="utf-8")
        self.assertIn("Geplant von 2.11.2026, 02:00 UTC bis 2.11.2026, 03:00 UTC", page)

    def test_bar_tooltips_are_translated(self):
        de = (self.root / "index.html").read_text(encoding="utf-8")
        en = (self.root / "en" / "index.html").read_text(encoding="utf-8")
        self.assertIn("30.10.2026: 60,00 % Verfügbarkeit", de)
        self.assertIn("Oct 30, 2026: 60.00 % uptime", en)
        self.assertIn("Keine Störungen", de)
        self.assertIn("No incidents", en)
        self.assertIn("Tägliche Verfügbarkeit von Web", de)

    def test_automatic_incident_text_in_both_languages(self):
        de = next((self.root / "incidents").glob("auto-web-*.html")).read_text(encoding="utf-8")
        en = next((self.root / "en" / "incidents").glob("auto-web-*.html")).read_text(encoding="utf-8")
        self.assertIn("Web ist nicht verfügbar", de)
        self.assertIn("Die automatischen Prüfungen für Web schlagen fehl.", de)
        self.assertIn("sind wieder erfolgreich. Die Komponente war etwa 15 Min. beeinträchtigt.", de)
        self.assertIn("Automatisch durch die Verfügbarkeitsprüfungen eröffnet.", de)
        self.assertIn("Web is unavailable", en)
        self.assertIn("Automated checks for Web are failing.", en)
        self.assertIn("The component was affected for about 15 min.", en)
        self.assertIn("Opened automatically by the uptime checks.", en)

    def test_owner_texts_are_shown_as_written_in_both_languages(self):
        for path in (self.root / "incidents" / "2026-10-30-login.html", self.root / "en" / "incidents" / "2026-10-30-login.html"):
            self.assertIn("App login fails", path.read_text(encoding="utf-8"))

    def test_status_json_is_language_neutral(self):
        self.assertFalse((self.root / "en" / "status.json").exists())
        doc = json.loads((self.root / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["status"]["description"], "Everything is up and running")
        self.assertEqual(sorted(doc), ["components", "incidents", "maintenance", "page", "status"])
        self.assertEqual([m["title"] for m in doc["maintenance"]], ["Database upgrade"])

    def test_404_points_to_both_languages(self):
        text = (self.root / "404.html").read_text(encoding="utf-8")
        self.assertIn("Seite nicht gefunden", text)
        self.assertIn('href="/"', text)
        self.assertIn('href="/en/"', text)
        self.assertIn('href="/style.css"', text)

    def test_german_footer_uses_german_legal_links_when_configured(self):
        cfg = config()
        cfg["site"].update(imprint_url="https://x.example/en/imprint", imprint_url_de="https://x.example/impressum",
                           privacy_url="https://x.example/en/imprint")
        out = Path(build(sample_data(), [], NOW).parent) / "legal"
        render.build_site(cfg, sample_data(), [], NOW, ROOT / "site", out)
        de = (out / "index.html").read_text(encoding="utf-8")
        en = (out / "en" / "index.html").read_text(encoding="utf-8")
        self.assertIn('<a href="https://x.example/impressum">Impressum</a>', de)
        self.assertIn('<a href="https://x.example/en/imprint">Imprint</a>', en)
        self.assertIn('<a href="https://x.example/en/imprint">Datenschutz</a>', de)  # falls back to the English URL


class AutomaticIncidentTemplateTest(unittest.TestCase):
    def test_stored_english_text_is_the_english_rendering(self):
        data = outage()
        incident = data["incidents"]["incidents"][0]
        names = lambda cid: "Web"  # noqa: E731
        self.assertEqual(incident["title"], "Web is unavailable")
        self.assertEqual(incident["title"], i18n.render_tpl("en", incident["tpl"], names))
        for update in incident["updates"]:
            self.assertEqual(update["body"], i18n.render_tpl("en", update["tpl"], names))
        self.assertEqual(i18n.render_tpl("de", incident["tpl"], names), "Web ist nicht verfügbar")

    def test_partial_escalation_and_where_text_in_german(self):
        cfg = config()
        data = model.empty_data()
        bad = {**fail("HTTP 503"), "diag": {"phase": "http_status"}}
        good = ok()
        run = lambda results, minute: model.apply_run(data, cfg, results, T0 + timedelta(minutes=minute))[0]  # noqa: E731
        data = run({"app": [good, good]}, 0)
        data = run({"app": [bad, good]}, 5)
        data = run({"app": [bad, good]}, 10)
        incident = data["incidents"]["incidents"][0]
        self.assertEqual(incident["tpl"]["kind"], "partial")
        update = incident["updates"][0]
        self.assertEqual(update["tpl"]["where"], [["x", "http_status"]])
        self.assertIn("Where it failed - x: the server answered with an unexpected status.", update["body"])
        german = i18n.render_tpl("de", update["tpl"], lambda cid: "App")
        self.assertTrue(german.startswith("Die automatischen Prüfungen für App schlagen fehl."), german)
        self.assertIn("Fehlerstelle - x: der Server antwortete mit einem unerwarteten Status.", german)

    def test_telegram_alerts_link_to_the_german_incident_page(self):
        data = outage()
        incident = data["incidents"]["incidents"][0]
        text = notify.pending_alerts(data, NAMES, BASE)[0].text
        self.assertTrue(text.endswith(f"{BASE}/incidents/{incident['id']}.html"), text)
        self.assertNotIn("/en/", text)


class ConfigTextTest(unittest.TestCase):
    def comp(self, **extra):
        return {
            "site": {"title": "t", "base_url": "https://s.example"},
            "settings": SETTINGS,
            "groups": [{"name": "G", "components": [{"id": "a", "name": "A", "checks": [{"url": "https://a.example"}], **extra}]}],
        }

    def test_german_name_and_description_are_accepted(self):
        cfg = validate_config(self.comp(name_de="Ä", description="D", description_de="DD"))
        comp = cfg["groups"][0]["components"][0]
        self.assertEqual(localized(comp, "name", "de"), "Ä")
        self.assertEqual(localized(comp, "name", "en"), "A")
        self.assertEqual(localized(comp, "description", "de"), "DD")
        self.assertEqual(localized(comp, "description", "en"), "D")

    def test_missing_german_text_falls_back_to_english(self):
        comp = validate_config(self.comp(description="D"))["groups"][0]["components"][0]
        self.assertEqual(localized(comp, "name", "de"), "A")
        self.assertEqual(localized(comp, "description", "de"), "D")
        self.assertEqual(localized(comp, "description", "en"), "D")

    def test_invalid_german_texts_are_rejected(self):
        for key in ("name_de", "description_de"):
            for bad in ("", "   ", 5, None, ["x"]):
                with self.assertRaises(ConfigError, msg=f"{key}={bad!r}"):
                    validate_config(self.comp(**{key: bad}))

    def test_unknown_keys_are_still_rejected(self):
        with self.assertRaises(ConfigError):
            validate_config(self.comp(name_fr="x"))

    def test_german_text_shows_on_the_german_page_only(self):
        cfg = validate_config(self.comp(name_de="Ä-Dienst", description="Eng", description_de="Beschreibung DE"))
        out = Path(build(sample_data(), [], NOW).parent) / "cfg"
        render.build_site(cfg, model.empty_data(), [], NOW, ROOT / "site", out)
        de = (out / "index.html").read_text(encoding="utf-8")
        en = (out / "en" / "index.html").read_text(encoding="utf-8")
        self.assertIn("Ä-Dienst", de)
        self.assertIn("Beschreibung DE", de)
        self.assertNotIn("Eng<", de)
        self.assertIn(">A</h3>", en)
        self.assertIn("Eng", en)
        self.assertNotIn("Beschreibung DE", en)

    def test_the_real_config_has_german_descriptions_for_every_component(self):
        cfg = load_config(ROOT / "config.json")
        components = [c for g in cfg["groups"] for c in g["components"]]
        self.assertTrue(components)
        for comp in components:
            self.assertTrue(comp.get("description_de"), comp["id"])
            self.assertNotEqual(comp["description_de"], comp.get("description"), comp["id"])


if __name__ == "__main__":
    unittest.main()
