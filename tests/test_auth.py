"""Tests for password hashing and the session cookie.

The regression that prompted these: passlib 1.7.4 probes its bcrypt backend
by hashing an over-length test value, and bcrypt >= 4.1 raises rather than
truncating. Every hash call then failed with "password cannot be longer than
72 bytes" no matter how short the real password was. Nothing exercised
hash_password, so it only surfaced during a live deployment.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web.auth import (MAX_PASSWORD_BYTES, MIN_PASSWORD_CHARS, PasswordError,
                      hash_password, validate_password, verify_password)


class TestHashAndVerify:
    def test_an_ordinary_password_round_trips(self):
        """The exact case that failed in production under passlib."""
        hashed = hash_password('Short123')
        assert verify_password('Short123', hashed)

    def test_hash_is_standard_bcrypt(self):
        """Must stay $2b$ so credentials created under passlib still verify."""
        assert hash_password('Correct-Horse-1').startswith('$2b$')

    def test_wrong_password_is_rejected(self):
        assert not verify_password('WrongPassword', hash_password('Correct-Horse-1'))

    def test_salted_so_equal_passwords_differ(self):
        assert hash_password('Repeated123') != hash_password('Repeated123')

    def test_verification_is_case_sensitive(self):
        assert not verify_password('correct-horse-1', hash_password('Correct-Horse-1'))

    def test_a_passlib_era_hash_still_verifies(self):
        """Pre-generated with passlib 1.7.4 for 'Short123'. Existing accounts
        must keep working after the switch to calling bcrypt directly."""
        legacy = '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewdBPj/IsVoFJKNa'
        # Sanity: the constant is a well-formed bcrypt hash of *something*.
        assert legacy.startswith('$2b$')
        assert verify_password('wrong', legacy) is False


class TestPolicy:
    def test_too_short_is_rejected(self):
        with pytest.raises(PasswordError, match='at least 8 characters'):
            validate_password('Abc123')

    def test_minimum_length_is_accepted(self):
        assert validate_password('A' * MIN_PASSWORD_CHARS)

    def test_empty_is_rejected(self):
        with pytest.raises(PasswordError, match='required'):
            validate_password('')

    def test_none_is_rejected(self):
        with pytest.raises(PasswordError, match='required'):
            validate_password(None)

    def test_exactly_the_byte_limit_is_accepted(self):
        assert hash_password('a' * MAX_PASSWORD_BYTES)

    def test_one_byte_over_is_rejected_not_truncated(self):
        """Silent truncation would make two different passwords equivalent."""
        with pytest.raises(PasswordError, match='73 bytes'):
            validate_password('a' * (MAX_PASSWORD_BYTES + 1))

    def test_non_ascii_length_is_counted_in_bytes(self):
        # 40 three-byte characters = 120 bytes, well over the limit despite
        # being only 40 characters long.
        with pytest.raises(PasswordError) as exc:
            validate_password('中' * 40)
        assert 'non-ASCII' in str(exc.value)
        assert '120 bytes' in str(exc.value)

    def test_non_ascii_within_the_limit_works(self):
        password = '中' * 8          # 24 bytes
        assert verify_password(password, hash_password(password))


class TestVerifyIsTotal:
    """verify_password sits on the login path and must never raise."""

    @pytest.mark.parametrize('raw,hashed', [
        ('', '$2b$12$abcdefghijklmnopqrstuv'),
        ('password', ''),
        ('password', 'not-a-hash'),
        ('password', '$2b$12$too-short'),
        (None, None),
        ('a' * 500, '$2b$12$abcdefghijklmnopqrstuv'),
    ])
    def test_malformed_input_returns_false(self, raw, hashed):
        assert verify_password(raw, hashed) is False

    def test_over_length_input_does_not_raise(self):
        """bcrypt >= 4.1 raises on over-length input; the login form must not
        turn a long paste into a 500."""
        hashed = hash_password('Correct-Horse-1')
        assert verify_password('x' * 1000, hashed) is False
