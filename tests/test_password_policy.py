import pytest
from pydantic import ValidationError

from app.schemas.auth_schema import RegisterRequest, _check_password_strength


@pytest.mark.parametrize("password", ["", "Ab1!", "Ab1!56789"])
def test_password_too_short_rejected(password):
    with pytest.raises(ValueError, match="10자 이상"):
        _check_password_strength(password)


def test_password_no_digit_rejected():
    with pytest.raises(ValueError, match="숫자"):
        _check_password_strength("Abcdefgh!@")


def test_password_no_letter_rejected():
    with pytest.raises(ValueError, match="글자"):
        _check_password_strength("1234567890!")


def test_password_no_special_rejected():
    with pytest.raises(ValueError, match="특수문자"):
        _check_password_strength("Abcd1234ef")


def test_password_with_whitespace_rejected():
    with pytest.raises(ValueError, match="공백"):
        _check_password_strength("Abc 1234!@")


def test_password_too_long_bytes_rejected():
    with pytest.raises(ValueError, match="72바이트"):
        _check_password_strength("A1!" + "한" * 24)


def test_password_at_byte_limit_accepted():
    password = "A1!" + "a" * 69
    assert len(password.encode("utf-8")) == 72
    assert _check_password_strength(password) == password


def test_password_korean_only_accepted():
    password = "한글한글한글가1!@"
    assert _check_password_strength(password) == password


def test_password_no_letter_message_says_글자():
    with pytest.raises(ValueError, match="글자"):
        _check_password_strength("1234567890!")


def test_password_valid_minimum():
    valid = "Abcd1234!@"
    assert _check_password_strength(valid) == valid


def test_register_request_validation():
    with pytest.raises(ValidationError):
        RegisterRequest(
            email="test@example.com",
            password="weak",
            terms_agreed=True,
        )
