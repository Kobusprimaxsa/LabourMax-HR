"""The configuration checks — because a secret read at use gives no signal at all.

``EncryptedCharField`` reads ``FIELD_ENCRYPTION_KEY`` at write time and refuses
rather than falling back to plaintext (D-77). That is right, and it is opaque: the
first thing anybody hears about a missing key is eleven ImproperlyConfigured
failures deep inside the encryption layer.

That is not hypothetical. CI had no key at all, and stayed red across four commits
while the same suite was green on the developer's machine, where the setup script
had generated one (D-94). One line before the suite would have said so.
"""

from __future__ import annotations

from core.checks import ENCRYPTION_KEY_MISSING, encryption_key_is_set


def test_a_missing_key_is_reported_once_and_clearly(settings):
    settings.FIELD_ENCRYPTION_KEY = ""

    issues = encryption_key_is_set(None)

    assert len(issues) == 1
    assert issues[0].id == ENCRYPTION_KEY_MISSING
    assert "FIELD_ENCRYPTION_KEY" in issues[0].msg
    assert "Fernet.generate_key" in issues[0].hint, (
        "The hint has to carry the command. A check that says what is wrong and "
        "not how to fix it sends the reader to the source."
    )


def test_a_configured_key_reports_nothing(settings):
    settings.FIELD_ENCRYPTION_KEY = "S0m3-t3st-k3y-0f-th3-right-l3ngth-AAAAAAAAAA="
    assert encryption_key_is_set(None) == []


def test_whitespace_is_not_a_key(settings):
    """An env file line reading ``FIELD_ENCRYPTION_KEY= `` is not a configured key."""
    settings.FIELD_ENCRYPTION_KEY = None
    assert encryption_key_is_set(None) != []


def test_the_check_is_registered_with_django():
    """Registered, not merely defined.

    A check function nobody registered is a test that passes and a CI step that
    never runs it. CI runs ``manage.py check --fail-level WARNING``, so this has
    to be reachable from Django's own registry.
    """
    from django.core.checks import registry

    registered = {check.__name__ for check in registry.registry.get_checks()}
    assert "encryption_key_is_set" in registered
