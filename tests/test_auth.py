from mobile_rpa import auth

SECRET = b"s" * 32


def test_issued_token_is_valid_until_expiry():
    token = auth.issue(SECRET, now=1000)
    assert auth.valid(SECRET, token, now=1001)
    assert not auth.valid(SECRET, token, now=1000 + auth.SESSION_SECONDS + 1)


def test_tampered_or_foreign_tokens_fail():
    token = auth.issue(SECRET, now=1000)
    expires, sig = token.split(".")
    assert not auth.valid(SECRET, f"{int(expires) + 99}.{sig}", now=1001)
    assert not auth.valid(b"x" * 32, token, now=1001)
    assert not auth.valid(SECRET, None)
    assert not auth.valid(SECRET, "garbage")


def test_password_check():
    assert auth.check_password("connect", "connect")
    assert not auth.check_password("Connect", "connect")
