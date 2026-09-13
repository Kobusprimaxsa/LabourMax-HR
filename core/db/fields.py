"""Application-level encryption for the two things that must not sit in plaintext.

Bank account numbers (here, in P3) and South African ID numbers (P4). Both are
personal information under POPIA, both appear in documents an employer downloads,
and both are the fields a database dump makes dangerous.

**The design constraint that shapes everything below: Fernet output is
non-deterministic.** Encrypting the same account number twice gives two different
ciphertexts, because each carries its own random IV. That is what makes the scheme
sound, and it makes an encrypted column unsearchable:

    EmployerBankAccount.objects.filter(account_number="1234567890")

would encrypt the search term afresh, compare a ciphertext that has never existed
against rows of different ciphertexts, and return **nothing at all**. Not an error —
an empty result, which reads as "no such account". That is the same silent-failure
shape as row-level security with no tenant pinned, and it is why ``EncryptedCharField``
refuses every lookup except ``isnull`` rather than letting one quietly return zero
rows.

Two plain columns carry what the ciphertext cannot:

``<field>_last4``
    What a person recognises on a payslip or a screen. Four digits identify an
    account to its owner and to nobody else.

``<field>_hash``
    A keyed HMAC-SHA256 of the normalised value, for the two questions that do need
    answering without decrypting: "is this the same account we already have" and
    "has this ID number been captured twice". Keyed, not a plain digest — a bare
    SHA-256 of a ten-digit account number falls to a lookup table in seconds.
"""

from __future__ import annotations

import hashlib
import hmac
import re

from django.conf import settings
from django.core.exceptions import FieldError, ImproperlyConfigured
from django.db import models

try:  # pragma: no cover - import guard, exercised only when cryptography is absent
    from cryptography.fernet import Fernet, InvalidToken
except ModuleNotFoundError:  # pragma: no cover
    Fernet = None
    InvalidToken = Exception


NON_ALPHANUMERIC = re.compile(r"[^0-9A-Za-z]")


def _key() -> bytes:
    """The configured key, or a refusal.

    Deliberately read at use rather than at import. An empty key must stop a write,
    not stop the application booting — but it must stop the write *loudly*. The
    alternative, falling back to storing plaintext when no key is configured, is how
    a production database ends up holding bank account numbers in the clear while
    every test asserting encryption still passes.
    """
    configured = getattr(settings, "FIELD_ENCRYPTION_KEY", "") or ""
    if not configured:
        raise ImproperlyConfigured(
            "FIELD_ENCRYPTION_KEY is not set, and this field refuses to store a value "
            'in plaintext. Generate one with: python -c "from cryptography.fernet '
            'import Fernet; print(Fernet.generate_key().decode())"'
        )
    if Fernet is None:  # pragma: no cover
        raise ImproperlyConfigured("cryptography is not installed.")
    return configured.encode() if isinstance(configured, str) else configured


def normalise(value: str) -> str:
    """Strip everything that is not a digit or letter, and upper-case what is left.

    An account number typed as "1234 5678 90" and one typed as "1234567890" are the
    same account. Without this the hash column would fail to spot the duplicate it
    exists to spot.
    """
    return NON_ALPHANUMERIC.sub("", value or "").upper()


def keyed_hash(value: str, *, scope: str = "") -> str:
    """A keyed digest of the normalised value, for equality without decryption.

    HMAC rather than a bare hash: the space of ten-digit account numbers and of
    South African ID numbers is small enough to enumerate, so an unkeyed digest is
    reversible by anyone holding the column.

    ``scope`` narrows what equality *means*, and on a multi-tenant table it is not
    optional. Without it the same ID number produces the same digest in every
    tenant, so anybody holding this column — a support engineer, a read replica, a
    backup — can tell that an employee of one employer is the same person as an
    employee of another. Nothing decrypts, no policy is bypassed, and a fact that
    crosses the tenant boundary has still escaped.

    That is the quietest kind of leak this schema can have: the isolation suite
    cannot see it, because no row was read that should not have been. So the scope
    is passed at the call site, where the question being asked is visible — pass
    ``f"tenant:{tenant_id}"`` for "has this person been captured twice for this
    subscriber" (D-95).
    """
    if not value:
        return ""
    payload = f"{scope}\x00{normalise(value)}" if scope else normalise(value)
    return hmac.new(_key(), payload.encode(), hashlib.sha256).hexdigest()


def last4(value: str) -> str:
    """The last four characters of the normalised value, for display."""
    return normalise(value)[-4:]


class EncryptedCharField(models.TextField):
    """A short string encrypted at rest, and unsearchable on purpose.

    Stored as a TextField because Fernet ciphertext is far longer than its input and
    its length varies; a CharField's max_length would be a limit on the ciphertext
    rather than on the value, which is the wrong thing to constrain.

    ``max_plaintext_length`` constrains the value instead, and is checked on the way
    in — so an over-long account number is refused at the application boundary
    rather than silently stored.
    """

    description = "A value encrypted with Fernet before it reaches the database"

    def __init__(self, *args, max_plaintext_length: int = 64, **kwargs):
        self.max_plaintext_length = max_plaintext_length
        super().__init__(*args, **kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        if self.max_plaintext_length != 64:
            kwargs["max_plaintext_length"] = self.max_plaintext_length
        return name, path, args, kwargs

    def get_prep_value(self, value):
        if value is None or value == "":
            return value
        value = str(value)
        if len(value) > self.max_plaintext_length:
            raise ValueError(
                f"{self.name}: {len(value)} characters exceeds the "
                f"{self.max_plaintext_length} this field accepts."
            )
        return Fernet(_key()).encrypt(value.encode()).decode()

    def from_db_value(self, value, expression, connection):
        if value is None or value == "":
            return value
        try:
            return Fernet(_key()).decrypt(value.encode()).decode()
        except InvalidToken as exc:
            raise ValueError(
                f"{self.name}: the stored value could not be decrypted with the "
                f"configured FIELD_ENCRYPTION_KEY. The key has changed, or this row "
                f"was written under a different one. Nothing is recoverable without "
                f"the original key."
            ) from exc

    def to_python(self, value):
        return value

    def get_lookup(self, lookup_name):
        """Refuse every lookup but ``isnull``.

        The lookup this exists to prevent is ``exact``. Fernet encrypts the search
        term with a fresh IV, so it can never equal a stored ciphertext, and the
        query returns an empty result rather than an error — "no such account"
        instead of "you cannot ask that". Use the ``_hash`` column for equality and
        the ``_last4`` column for display.
        """
        if lookup_name in {"isnull"}:
            return super().get_lookup(lookup_name)
        raise FieldError(
            f"'{self.name}' is encrypted and cannot be queried with "
            f"'{lookup_name}'. Fernet ciphertext is non-deterministic, so the "
            f"comparison would silently match nothing. Query the companion "
            f"'{self.name}_hash' column with core.db.fields.keyed_hash(value), or "
            f"'{self.name}_last4' for display."
        )
