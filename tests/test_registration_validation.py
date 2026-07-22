import unittest
from pathlib import Path

from app.routes.auth import _registration_validation_error


class RegistrationValidationTests(unittest.TestCase):
    VALID_EMAIL = "owner@example.com"
    VALID_PASSWORD = "correct pass✓"

    def validate(self, name="Test Owner", email=VALID_EMAIL, password=VALID_PASSWORD, confirm=None):
        return _registration_validation_error(
            name, email, password, password if confirm is None else confirm
        )

    def test_name_boundaries_and_blank_values(self):
        self.assertIsNotNone(self.validate(name=""))
        self.assertIsNotNone(self.validate(name=" "))
        self.assertIsNotNone(self.validate(name="A"))
        self.assertIsNone(self.validate(name="AB"))
        self.assertIsNone(self.validate(name="A" * 100))
        self.assertIsNotNone(self.validate(name="A" * 101))

    def test_legitimate_unicode_and_punctuation_names(self):
        for name in ("Dhara Prasath", "Anne-Marie", "O'Connor", "Dr. José", "李 明", "D’Arcy"):
            with self.subTest(name=name):
                self.assertIsNone(self.validate(name=name))

    def test_name_rejects_crlf_null_control_and_xss(self):
        for name in ("First\rLast", "First\nLast", "First\0Last", "First\tLast", "<script>alert(1)</script>"):
            with self.subTest(name=repr(name)):
                self.assertIsNotNone(self.validate(name=name))

    def test_email_normalization_is_performed_by_registration_route(self):
        raw = " Owner@Example.COM "
        normalized = raw.strip().lower()
        self.assertEqual("owner@example.com", normalized)
        self.assertIsNone(self.validate(email=normalized))

    def test_email_syntax_rejections(self):
        for email in (
            "bad", "a@@example.com", "a@", "@example.com", "a@localhost",
            ".a@example.com", "a..b@example.com", "a@example..com",
            "a\n@example.com", "a\0@example.com",
        ):
            with self.subTest(email=repr(email)):
                self.assertIsNotNone(self.validate(email=email))

    def test_email_254_and_255_character_boundaries(self):
        local = "a" * 64
        domain_189 = ".".join(("b" * 63, "c" * 63, "d" * 61))
        email_254 = f"{local}@{domain_189}"
        self.assertEqual(254, len(email_254))
        self.assertIsNone(self.validate(email=email_254))
        self.assertIsNotNone(self.validate(email=email_254 + "x"))

    def test_password_boundaries_spaces_unicode_and_mismatch(self):
        self.assertIsNotNone(self.validate(password="a" * 11))
        self.assertIsNone(self.validate(password="a" * 12))
        self.assertIsNone(self.validate(password="a" * 128))
        self.assertIsNotNone(self.validate(password="a" * 129))
        self.assertIsNone(self.validate(password="spaces work ✓"))
        self.assertIsNotNone(self.validate(confirm="different password"))

    def test_browser_limits_match_backend_limits(self):
        template = (
            Path(__file__).resolve().parents[1] / "app" / "templates" / "register.html"
        ).read_text(encoding="utf-8")
        self.assertIn('name="name"', template)
        self.assertIn('minlength="2" maxlength="100"', template)
        self.assertIn('name="email"', template)
        self.assertIn('maxlength="254"', template)
        self.assertEqual(2, template.count('minlength="12" maxlength="128"'))

if __name__ == "__main__":
    unittest.main()
