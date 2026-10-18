import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import radar  # noqa: E402
from crustdata_api import Crustdata  # noqa: E402

SINCE, LEFT_AFTER = "2026-07-01", "2026-01-01"
DOMAINS = ["labco.ai", "payco.com"]


def role(name, title, website, start, end=None, years=None):
    return {"name": name, "title": title, "company_website": website, "start_date": start,
            "end_date": end, "years_at_company_raw": years}


def person(pid, current, past, country="United States"):
    return {"crustdata_person_id": pid,
            "basic_profile": {"name": f"Founder {pid}", "location": {"country": country}},
            "social_handles": {"professional_network_identifier": {"profile_url": f"https://example.com/in/{pid}"}},
            "experience": {"employment_details": {"current": current, "past": past}}}


GOOD = person(1, [role("Stealth Startup", "Co-Founder & CTO", "https://stealth.example", "2026-08-01T00:00:00")],
              [role("LabCo", "Staff Research Engineer", "https://www.labco.ai/", "2022-01-01", "2026-07-01T00:00:00", 4.5),
               role("OldCo", "Founder", "https://oldco.io", "2018-01-01", "2021-12-01", 3.9)])


class QualifyTests(unittest.TestCase):
    def test_good_founder(self):
        r = radar.qualify(GOOD, DOMAINS, SINCE, LEFT_AFTER)
        self.assertIsNotNone(r)
        self.assertEqual(r["last_company"], "LabCo")
        self.assertTrue(r["repeat_founder"])
        self.assertEqual(r["score"], 10)

    def test_last_job_elsewhere_is_dropped(self):
        p = person(2, GOOD["experience"]["employment_details"]["current"],
                   [role("LabCo", "Engineer", "https://labco.ai", "2020-01-01", "2024-01-01"),
                    role("OtherCo", "Engineer", "https://other.co", "2024-01-01", "2026-07-01")])
        self.assertIsNone(radar.qualify(p, DOMAINS, SINCE, LEFT_AFTER))

    def test_intern_is_dropped(self):
        p = person(3, GOOD["experience"]["employment_details"]["current"],
                   [role("LabCo", "Research Intern", "https://labco.ai", "2026-01-01", "2026-06-01")])
        self.assertIsNone(radar.qualify(p, DOMAINS, SINCE, LEFT_AFTER))

    def test_left_long_ago_is_dropped(self):
        p = person(4, GOOD["experience"]["employment_details"]["current"],
                   [role("LabCo", "Engineer", "https://labco.ai", "2019-01-01", "2025-03-01")])
        self.assertIsNone(radar.qualify(p, DOMAINS, SINCE, LEFT_AFTER))

    def test_old_stealth_role_is_dropped(self):
        p = person(5, [role("Stealth", "Founder", "", "2025-01-01")],
                   [role("LabCo", "Engineer", "https://labco.ai", "2022-01-01", "2026-06-01")])
        self.assertIsNone(radar.qualify(p, DOMAINS, SINCE, LEFT_AFTER))

    def test_non_founder_stealth_role_is_dropped(self):
        p = person(6, [role("Stealth AI Startup", "Software Engineer", "", "2026-08-01")],
                   [role("LabCo", "Engineer", "https://labco.ai", "2022-01-01", "2026-06-01")])
        self.assertIsNone(radar.qualify(p, DOMAINS, SINCE, LEFT_AFTER))


class RunTests(unittest.TestCase):
    def test_run_dedupes_and_uses_person_search(self):
        calls = []

        def transport(url, body, headers, timeout):
            calls.append((url, body))
            return {"profiles": [GOOD, GOOD], "next_cursor": None}

        out = radar.run(Crustdata(api_key="test", transport=transport), DOMAINS, SINCE, LEFT_AFTER, "United States", 10)
        self.assertEqual(out["scanned"], 2)
        self.assertEqual(len(out["founders"]), 1)
        self.assertTrue(calls[0][0].endswith("/person/search"))
        f = json.dumps(calls[0][1]["filters"])
        for needed in ("experience.employment_details.current.title",
                       "experience.employment_details.past.company_website_domain",
                       "basic_profile.location.country"):
            self.assertIn(needed, f)

    def test_pagination_follows_cursor(self):
        pages = [{"profiles": [GOOD], "next_cursor": "abc"}, {"profiles": [], "next_cursor": None}]
        seen = []

        def transport(url, body, headers, timeout):
            seen.append(body.get("cursor"))
            return pages.pop(0)

        radar.run(Crustdata(api_key="test", transport=transport), DOMAINS, SINCE, LEFT_AFTER, None, 50)
        self.assertEqual(seen, [None, "abc"])


class OutputTests(unittest.TestCase):
    def test_summary_and_slack(self):
        r = radar.qualify(GOOD, DOMAINS, SINCE, LEFT_AFTER)
        s = radar.summary({"scanned": 5, "founders": [r]})
        self.assertEqual(s["stealth_founders"], 1)
        self.assertEqual(s["by_last_company"], {"LabCo": 1})
        self.assertIn("1 new stealth founders", radar.slack_text([r], SINCE))

    def test_state(self):
        r = radar.qualify(GOOD, DOMAINS, SINCE, LEFT_AFTER)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "seen.json")
            radar.save_state([r], path)
            self.assertEqual(radar.only_new([r], path), [])


if __name__ == "__main__":
    unittest.main()
