import pytest

from fpb.proxy import ProxyError, parse_proxy, redact


def test_http_with_credentials():
    p = parse_proxy("http://alice:s3cret@proxy.example.com:8080")
    assert (p.scheme, p.host, p.port, p.username, p.password) == ("http", "proxy.example.com", 8080, "alice", "s3cret")
    assert p.to_playwright() == {
        "server": "http://proxy.example.com:8080",
        "username": "alice",
        "password": "s3cret",
    }
    assert p.masked() == "http://alice:***@proxy.example.com:8080"
    assert "s3cret" not in p.masked()


def test_no_credentials():
    p = parse_proxy("socks5://203.0.113.11:1080")
    assert p.to_playwright() == {"server": "socks5://203.0.113.11:1080"}
    assert p.masked() == "socks5://203.0.113.11:1080"
    assert list(p.secrets()) == []


def test_percent_encoded_password_round_trips():
    p = parse_proxy("http://bob:p%40ss%3Aw%2Frd@h:1")
    assert p.password == "p@ss:w/rd"
    assert parse_proxy(p.url()) == p
    assert set(p.secrets()) == {"p@ss:w/rd", "p%40ss%3Aw%2Frd"}


def test_socks5h_alias_and_scheme_case():
    assert parse_proxy("SOCKS5H://h:1080").scheme == "socks5"
    assert parse_proxy("HTTPS://h:443").scheme == "https"


def test_ipv6_host():
    p = parse_proxy("http://[::1]:3128")
    assert p.host == "::1"
    assert p.server == "http://[::1]:3128"


@pytest.mark.parametrize(
    "url, needle",
    [
        ("", "empty"),
        ("proxy.example.com:8080", "scheme"),
        ("ftp://h:21", "unsupported"),
        ("http://h", "no port"),
        ("http://:8080", "no host"),
        ("http://h:99999", "malformed"),
        ("http://h:8080/path", "path"),
        ("http://h:8080?x=1", "path"),
        ("http://:pw@h:8080", "no user name"),
    ],
)
def test_rejects_bad_urls(url, needle):
    with pytest.raises(ProxyError) as err:
        parse_proxy(url)
    assert needle in str(err.value)


def test_error_message_never_contains_password():
    with pytest.raises(ProxyError) as err:
        parse_proxy("http://u:topsecret@h:notaport")
    assert "topsecret" not in str(err.value)


def test_redact():
    assert redact("proxy http://u:abc@h failed (abc)", ["abc"]) == "proxy http://u:***@h failed (***)"
    assert redact("nothing here", []) == "nothing here"



def test_parse_bypass():
    import pytest

    from fpb.proxy import ProxyError, parse_bypass

    assert parse_bypass(["api.example.com, .example.org", "*.cdn.test", "API.example.com"]) == [
        "api.example.com", ".example.org", ".cdn.test"]
    assert parse_bypass([]) == []
    for bad, why in [("https://api.example.com", "host name only"), ("h.test:8100", "host name only"),
                     ("ai", "too broad"), (".com", "too broad"), ("a b.test", "not a host name")]:
        with pytest.raises(ProxyError, match=why):
            parse_bypass([bad])
