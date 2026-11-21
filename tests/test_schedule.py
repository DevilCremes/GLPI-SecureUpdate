from datetime import datetime
from pathlib import Path
import os
import pwd
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"service"))
import agent
import common
import schedule


def stamp(value):
    return datetime.fromisoformat(value).timestamp()


class Calendar(unittest.TestCase):
    def next(self, now, kind, **values):
        rule={"kind":kind,"zone":"America/Sao_Paulo","at":"09:00",**values}
        schedule.validate(rule)
        return schedule.next_check({"schedule":rule}, stamp(now))

    def test_simple_elapsed_hours(self):
        self.assertEqual(schedule.next_check({"hours":6},100),21700)

    def test_monday_before_and_at_time(self):
        for now, expected in [("2026-11-20T15:00-03:00","2026-11-23T09:00-03:00"),
                              ("2026-11-23T09:00-03:00","2026-11-30T09:00-03:00")]:
            self.assertEqual(self.next(now,"weekly",weekday=1),stamp(expected))

    def test_first_of_month_across_year(self):
        self.assertEqual(self.next("2026-12-31T22:00-03:00","first"),stamp("2027-01-01T09:00-03:00"))

    def test_seventh(self):
        self.assertEqual(self.next("2026-11-08T10:00-03:00","monthly",day=7),stamp("2026-12-07T09:00-03:00"))

    def test_thirty_first_skips_short_month(self):
        self.assertEqual(self.next("2027-01-31T10:00-03:00","monthly",day=31),stamp("2027-03-31T09:00-03:00"))

    def test_last_day_leap_year(self):
        self.assertEqual(self.next("2028-02-01T09:00-03:00","last"),stamp("2028-02-29T09:00-03:00"))

    def test_dst_missing_and_repeated_time(self):
        self.assertEqual(self.next("2026-03-28T03:00+01:00","daily",zone="Europe/Paris",at="02:30"),stamp("2026-03-30T02:30+02:00"))
        self.assertEqual(self.next("2026-10-24T03:00+02:00","daily",zone="Europe/Paris",at="02:30"),stamp("2026-10-25T02:30+02:00"))
        self.assertEqual(self.next("2026-10-25T02:40+02:00","daily",zone="Europe/Paris",at="02:30"),stamp("2026-10-26T02:30+01:00"))

    def test_validation(self):
        for edit in [dict(kind="bad"), dict(day=True),dict(day=32),dict(zone="Wrong/Clock"),dict(at="24:00"),dict(weekday=1)]:
            with self.subTest(edit=edit), self.assertRaises(ValueError):
                schedule.validate({"kind":"monthly","day":7,"at":"10:30","zone":"UTC",**edit})


class Preferences(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root=Path(temp.name)
        patches=patch.multiple(agent,STATE=root,STORE=root/"panel",JOURNAL=root/"journal",LAST=root/"last")
        patches.start(); self.addCleanup(patches.stop)
        self.user={"web_user":pwd.getpwuid(os.getuid()).pw_name}
        common.write(agent.STORE,{"hours":12,"next":123})
        self.control=agent.Agent(self.user)
        self.settings={"op":"settings","actor":10,"browser":True,"email":True}
        self.rule={"kind":"monthly","day":7,"zone":"America/Sao_Paulo","at":"09:00"}
        self.now=stamp("2026-11-20T10:00-03:00")

    def test_legacy_and_switching_modes(self):
        self.assertIsNone(self.control.s.get("schedule"))
        with patch.object(agent.time,"time",return_value=self.now):
            self.control.dispatch({**self.settings,"schedule":self.rule})
            restored=agent.Agent(self.user)
            self.assertEqual(restored.s["schedule"],self.rule)
            self.assertEqual(restored.s["next"],stamp("2026-12-07T09:00-03:00"))
            restored.dispatch({**self.settings,"hours":8})
            self.assertIsNone(restored.s["schedule"])
            self.assertEqual(restored.s["next"],self.now+8*3600)

    def test_channel_change_preserves_due_and_invalid_setting_preserves_state(self):
        self.control.dispatch({**self.settings,"schedule":self.rule})
        due=self.control.s["next"]
        self.control.dispatch({**self.settings,"schedule":self.rule,"email":False})
        with self.assertRaises(ValueError):
            self.control.dispatch({**self.settings,"schedule":{**self.rule,"day":0}})
        self.assertEqual(self.control.s["next"],due)
        self.assertFalse(self.control.s["email"])

    def test_overdue_deadline_and_manual_checks(self):
        with patch.object(agent.time,"time",return_value=self.now),patch.object(agent.subprocess,"Popen"):
            self.control.dispatch({**self.settings,"schedule":self.rule})
            self.control.save(next=self.now-10*86400)
            self.control.start("check",10)
            self.assertFalse(self.control.running_job["notify"])
            self.assertLess(self.control.s["next"],self.now)
            self.control.process=None
            self.control.start("check",0)
            self.assertTrue(self.control.running_job["notify"])
            self.assertEqual(self.control.s["next"],stamp("2026-12-07T09:00-03:00"))
            self.assertEqual(agent.Agent(self.user).s["next"],self.control.s["next"])

    def test_in_flight_completion_preserves_new_schedule(self):
        with patch.object(agent.time,"time",return_value=self.now):
            self.control.dispatch({**self.settings,"schedule":self.rule})
            self.control.process=Mock(returncode=0)
            self.control.running_job={"op":"check"}
            common.write(agent.STATE/"catalog.json",{})
            self.control.poll()
            self.assertEqual(self.control.s["next"],stamp("2026-12-07T09:00-03:00"))
