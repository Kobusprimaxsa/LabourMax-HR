"""Django system checks — configuration failures reported as one line, not fifteen.

Registered in ``CoreConfig.ready()`` and therefore run by ``manage.py check``,
``runserver``, ``migrate`` and the CI step that sits before the test suite.

The pattern this exists for is narrow and has already happened once. A setting
that is read *at use* rather than at import — which is the right way to read a
secret, because an empty one must stop a write rather than stop the application
booting — gives no signal at all until something touches it. Then eleven tests
fail at once, deep inside the encryption layer, on a fresh clone whose only real
problem is that nobody ran the setup script.

A check turns that into one line, before anything else runs.
"""

from __future__ import annotations

from django.conf import settings
from django.core.checks import Warning as CheckWarning
from django.core.checks import register

#: Warning rather than Error, deliberately. The application must still boot and
#: still serve pages that touch no encrypted column — a missing key is a
#: half-finished setup, not a broken deployment. CI runs
#: ``manage.py check --fail-level WARNING``, so it is fatal exactly where it
#: should be.
ENCRYPTION_KEY_MISSING = "labourmax.W001"


@register()
def encryption_key_is_set(app_configs, **kwargs):
    """``FIELD_ENCRYPTION_KEY`` has to exist before anything writes an ID number.

    ``EncryptedCharField`` refuses the write when it is empty rather than falling
    back to plaintext (D-77), which is the correct behaviour and an opaque one:
    the message arrives at the first ``save()``, once per failing test, with no
    hint that the cause is one blank line in ``.env``.

    Found by CI, which had no key at all: every test touching
    ``employer_bank_account`` failed with ImproperlyConfigured while the same
    suite was green on the developer's machine, where ``setup-windows.ps1`` had
    generated one months earlier (D-94).
    """
    if getattr(settings, "FIELD_ENCRYPTION_KEY", "") or "":
        return []

    return [
        CheckWarning(
            "FIELD_ENCRYPTION_KEY is not set.",
            hint=(
                "Every encrypted column refuses to write rather than storing "
                "plaintext, so ID numbers and bank account numbers cannot be "
                "saved at all until this is set. Generate one with:\n"
                '  python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"\n'
                "and put it in .env as FIELD_ENCRYPTION_KEY. On Windows, "
                "setup-windows.ps1 does this for you."
            ),
            id=ENCRYPTION_KEY_MISSING,
        )
    ]
