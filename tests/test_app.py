"""Verify the actual UGR markup and delivery behavior without sending real email."""

from dataclasses import replace
from datetime import date, datetime, timezone
from email import policy
from email.parser import BytesParser
import os
from pathlib import Path
import smtplib
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import app


FIXTURE = Path(__file__).parent / "fixtures" / "ugr-2026-09-19.html"
MONDAY = date(2026, 9, 21)


def config(path):
    """Use fake SMTP details and an isolated state file for each delivery test."""
    return app.Config("smtp.example.com", 587, "starttls", "user", "password", "sender@example.com",
                      ("one@example.com", "two@example.com"), (), "main", ZoneInfo("Europe/Madrid"),
                      app.time(9), app.time(12), 900, path, include_images=False)


class ParserTests(unittest.TestCase):
    """Exercise dates, campus separation and completeness against a real snapshot."""

    @classmethod
    def setUpClass(cls):
        cls.html = FIXTURE.read_text(encoding="utf-8")

    def test_exact_date_and_both_options(self):
        menus = app.extract_menu(self.html, MONDAY)
        self.assertEqual(set(menus), {"1", "2"})
        self.assertIn(("segundo", "Pollo Asado"), menus["1"])
        self.assertIn(("segundo", "Tofu Gratinado Con Piña y Alioli De Soja"), menus["2"])
        self.assertNotIn("Fideuá", str(menus))

    def test_locations_do_not_mix(self):
        main = app.extract_menu(self.html, MONDAY)
        pts = app.extract_menu(self.html, MONDAY, "pts")
        self.assertIn(("acompanamiento", "Calabacín Frito A La Andaluza"), main["1"])
        self.assertIn(("acompanamiento", "Patatas fritas"), pts["1"])
        self.assertEqual(len([row for row in pts["1"] if row[0] == "primero"]), 2)

    def test_multiple_sides_are_preserved(self):
        menus = app.extract_menu(self.html, date(2026, 9, 22))
        sides = [dish for course, dish in menus["2"] if course == "acompanamiento"]
        self.assertEqual(sides, ["Ensalada Mixta 1", "Empanadillas De Verduras"])

    def test_no_fallback_for_absent_dates_or_different_year(self):
        for day in (date(2026, 9, 19), date(2026, 9, 20), date(2025, 9, 21)):
            with self.subTest(day=day):
                self.assertEqual(app.extract_menu(self.html, day), {})

    def test_changed_layout_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "recognizable"):
            app.extract_menu("<h1>Maintenance</h1><p>Try again later</p>", MONDAY)

    def test_partial_menu_is_an_error(self):
        changed = self.html.replace("Postre", "Removed course")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            app.extract_menu(changed, MONDAY)

    def test_date_without_options_is_an_error(self):
        changed = self.html.replace("Menú 1", "Removed option").replace("Menú 2", "Removed option")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            app.extract_menu(changed, MONDAY)

    def test_duplicate_menu_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            app.extract_menu(self.html + self.html, MONDAY)

    def test_friday_is_available_for_preview(self):
        self.assertIn(("primero", "Fideuá"), app.extract_menu(self.html, date(2026, 9, 18))["1"])

    def test_email_escapes_html_and_keeps_plain_text(self):
        _, plain, html = app.newsletter(MONDAY, {"1": [("primero", "Rice <script> & peas")]}, "main")
        self.assertIn("Rice <script> & peas", plain)
        self.assertIn("Rice &lt;script&gt; &amp; peas", html)
        self.assertNotIn("<script>", html)
        self.assertIn(app.SOURCE, plain)

    def test_second_menu_has_vegetarian_label_in_both_formats(self):
        menus = app.extract_menu(self.html, MONDAY)
        _, plain, html = app.newsletter(MONDAY, menus, "main")
        self.assertIn("Menu 2 · Vegetarian", plain)
        self.assertIn("Menu 2 · Vegetarian</h2>", html)
        self.assertNotIn("Menu 1 · Vegetarian", plain + html)


class DeliveryTests(unittest.TestCase):
    """Simulate retries, restarts and errors using temporary persistent records."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.config = config(Path(self.directory.name) / "delivery.sqlite3")
        self.connection = app.open_state(self.config.state_path)
        self.fetch = patch("app.fetch_page", return_value=FIXTURE.read_text(encoding="utf-8")).start()
        self.send = patch("app.send_email").start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(lambda: self.connection.close())

    def test_restart_does_not_resend(self):
        self.assertTrue(app.deliver(self.config, MONDAY, self.connection))
        self.connection.close()
        self.connection = app.open_state(self.config.state_path)
        self.assertTrue(app.deliver(self.config, MONDAY, self.connection))
        self.assertEqual(self.send.call_count, 2)
        self.fetch.assert_called_once()

    def test_partial_failure_only_retries_failed_recipient(self):
        self.send.side_effect = [None, smtplib.SMTPException("simulated failure")]
        self.assertFalse(app.deliver(self.config, MONDAY, self.connection))
        self.send.side_effect = None
        self.assertTrue(app.deliver(self.config, MONDAY, self.connection))
        recipients = [call.args[1] for call in self.send.call_args_list]
        self.assertEqual(recipients, ["one@example.com", "two@example.com", "two@example.com"])

    def test_friday_through_sunday_block_direct_delivery(self):
        for day in (date(2026, 9, 25), date(2026, 9, 26), date(2026, 9, 27)):
            with self.subTest(day=day):
                self.assertTrue(app.deliver(self.config, day, self.connection))
        self.fetch.assert_not_called()
        self.send.assert_not_called()

    def test_absent_menu_is_not_marked_delivered(self):
        self.assertFalse(app.deliver(self.config, date(2026, 9, 28), self.connection))
        self.send.assert_not_called()
        self.assertEqual(self.connection.execute("SELECT count(*) FROM deliveries").fetchone()[0], 0)

    def test_late_publication_is_retried(self):
        # A missing Monday becomes available on the following check.
        self.fetch.return_value = FIXTURE.read_text(encoding="utf-8").replace("21  DE", "28  DE")
        self.assertFalse(app.deliver(self.config, MONDAY, self.connection))
        self.fetch.return_value = FIXTURE.read_text(encoding="utf-8")
        self.assertTrue(app.deliver(self.config, MONDAY, self.connection))
        self.assertEqual(self.send.call_count, 2)

    def test_new_date_has_its_own_deliveries(self):
        app.deliver(self.config, MONDAY, self.connection)
        app.deliver(self.config, date(2026, 9, 22), self.connection)
        self.assertEqual(self.send.call_count, 4)

    def test_new_recipient_does_not_resend_existing_recipients(self):
        app.deliver(self.config, MONDAY, self.connection)
        updated = replace(self.config, recipients=(*self.config.recipients, "three@example.com"))
        app.deliver(updated, MONDAY, self.connection)
        self.assertEqual(self.send.call_count, 3)
        self.assertEqual(self.send.call_args.args[1], "three@example.com")

    def test_image_search_runs_once_for_all_recipients_and_skips_completed_day(self):
        cfg = replace(self.config, include_images=True)
        with patch("app.find_images", return_value={}) as search:
            self.assertTrue(app.deliver(cfg, MONDAY, self.connection))
            self.assertTrue(app.deliver(cfg, MONDAY, self.connection))
            search.assert_called_once()
        self.assertEqual(self.send.call_count, 2)


class ScheduleTests(unittest.TestCase):
    """Check the daily window in Madrid through both daylight-saving transitions."""

    def setUp(self):
        self.config = config(Path("unused.sqlite3"))

    def test_friday_through_sunday_are_always_excluded(self):
        for day in (25, 26, 27):
            with self.subTest(day=day):
                self.assertFalse(app.in_window(datetime(2026, 9, day, 9, tzinfo=self.config.timezone), self.config))

    def test_morning_catch_up_and_noon_cutoff(self):
        for hour, minute, expected in ((8, 59, False), (9, 0, True), (10, 30, True), (11, 59, True), (12, 0, False)):
            with self.subTest(hour=hour, minute=minute):
                now = datetime(2026, 9, 21, hour, minute, tzinfo=self.config.timezone)
                self.assertEqual(app.in_window(now, self.config), expected)

    def test_nine_am_remains_local_across_dst(self):
        # Eligible weekdays around each clock change must still open at 09:00 local time.
        for day, utc_hour in ((date(2026, 3, 26), 8), (date(2026, 3, 30), 7),
                              (date(2026, 10, 22), 7), (date(2026, 10, 26), 8)):
            with self.subTest(day=day):
                self.assertTrue(app.in_window(datetime(day.year, day.month, day.day, utc_hour,
                                                      tzinfo=timezone.utc), self.config))
                self.assertFalse(app.in_window(datetime(day.year, day.month, day.day, utc_hour - 1, 59,
                                                       tzinfo=timezone.utc), self.config))


class TestEmailTests(unittest.TestCase):
    """Keep test sends separate from production recipients, dates and delivery records."""

    def setUp(self):
        self.config = replace(config(Path("unused.sqlite3")),
                              test_recipients=("test-one@example.com", "test-two@example.com"))
        self.fetch = patch("app.fetch_page", return_value=FIXTURE.read_text(encoding="utf-8")).start()
        self.send = patch("app.send_email").start()
        self.addCleanup(patch.stopall)

    def test_repeated_test_sends_ignore_schedule_and_delivery_history(self):
        friday = date(2026, 9, 18)
        self.assertTrue(app.send_test_email(self.config, friday))
        self.assertTrue(app.send_test_email(self.config, friday))
        recipients = [call.args[1] for call in self.send.call_args_list]
        self.assertEqual(recipients, ["test-one@example.com", "test-two@example.com"] * 2)
        self.assertTrue(all(call.args[2][0].startswith("[TEST]") for call in self.send.call_args_list))
        self.assertNotIn("one@example.com", recipients)

    def test_empty_test_list_sends_nothing(self):
        self.assertFalse(app.send_test_email(replace(self.config, test_recipients=()), MONDAY))
        self.fetch.assert_not_called()
        self.send.assert_not_called()

    def test_test_email_uses_image_search_without_opening_delivery_state(self):
        cfg = replace(self.config, include_images=True)
        with patch("app.find_images", return_value={}) as search, patch("app.open_state") as state:
            with patch("app.Config.from_env", return_value=cfg), patch("sys.argv", ["app.py", "test-email"]), patch("app.datetime") as now:
                now.now.return_value = datetime(2026, 9, 21, 15, tzinfo=cfg.timezone)
                self.assertEqual(app.main(), 0)
            search.assert_called_once()
            state.assert_not_called()
        self.assertEqual([call.args[1] for call in self.send.call_args_list], list(cfg.test_recipients))


class MailTests(unittest.TestCase):
    """Inspect MIME and TLS behavior through a fake SMTP connection."""

    def test_starttls_private_recipient_and_multipart(self):
        cfg = config(Path("unused.sqlite3"))
        with patch("app.smtplib.SMTP") as factory:
            smtp = factory.return_value
            app.send_email(cfg, cfg.recipients[0], app.newsletter(MONDAY, {"1": [("postre", "Sandía")]}, "main"))
            smtp.starttls.assert_called_once()
            smtp.login.assert_called_once_with("user", "password")
            call = smtp.send_message.call_args
            message = BytesParser(policy=policy.default).parsebytes(call.args[0].as_bytes())
            self.assertEqual(call.kwargs["to_addrs"], ["one@example.com"])
            self.assertEqual(message["To"], "one@example.com")
            self.assertNotIn("two@example.com", message.as_string())
            self.assertEqual(message.get_content_type(), "multipart/alternative")
            self.assertIn("Sandía", message.get_body(preferencelist=("plain",)).get_content())

    def test_ssl_mode_does_not_starttls(self):
        cfg = replace(config(Path("unused.sqlite3")), security="ssl", port=465)
        with patch("app.smtplib.SMTP_SSL") as factory:
            app.send_email(cfg, cfg.recipients[0], ("subject", "plain", "<p>html</p>"))
            factory.return_value.starttls.assert_not_called()
            self.assertEqual(factory.call_args.args, ("smtp.example.com", 465))

    def test_quit_failure_after_acceptance_is_not_a_delivery_failure(self):
        with patch("app.smtplib.SMTP") as factory:
            factory.return_value.quit.side_effect = smtplib.SMTPServerDisconnected()
            app.send_email(config(Path("unused.sqlite3")), "one@example.com", ("subject", "plain", "html"))
            factory.return_value.send_message.assert_called_once()


class ConfigurationTests(unittest.TestCase):
    """Reject settings that would silently prevent scheduled or secure delivery."""

    def setUp(self):
        self.env = {"SMTP_HOST": "smtp.example.com", "MAIL_FROM": "sender@example.com",
                    "RECIPIENTS": "one@example.com,one@example.com"}

    def test_defaults_and_recipient_deduplication(self):
        with patch.dict(os.environ, self.env, clear=True):
            cfg = app.Config.from_env()
        self.assertEqual(cfg.recipients, ("one@example.com",))
        self.assertEqual(cfg.test_recipients, ())
        self.assertEqual(str(cfg.timezone), "Europe/Madrid")
        self.assertEqual(cfg.send_at, app.time(9))

    def test_invalid_settings_fail_early(self):
        for changes in ({"SMTP_SECURITY": "none"}, {"SMTP_PORT": "0"}, {"RECIPIENTS": ""},
                        {"SEND_AT": "13:00"}, {"SMTP_USERNAME": "missing-password"}, {"RETRY_SECONDS": "0"}):
            with self.subTest(changes=changes), patch.dict(os.environ, {**self.env, **changes}, clear=True):
                with self.assertRaises(ValueError):
                    app.Config.from_env()

    def test_password_file(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "password"
            secret.write_text("secret\n")
            with patch.dict(os.environ, {**self.env, "SMTP_USERNAME": "user", "SMTP_PASSWORD_FILE": str(secret)}, clear=True):
                self.assertEqual(app.Config.from_env().password, "secret")

    def test_test_recipients_are_optional_and_deduplicated(self):
        env = {**self.env, "TEST_RECIPIENTS": "test@example.com,test@example.com"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(app.Config.from_env().test_recipients, ("test@example.com",))


if __name__ == "__main__":
    unittest.main()
