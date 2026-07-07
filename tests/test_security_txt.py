def test_security_txt(client):
    resp = client.get("/.well-known/security.txt")
    assert resp.status_code == 200
    assert resp["Content-Type"] == "text/plain; charset=utf-8"
    body = resp.content.decode()
    assert "Contact: mailto:security@samandmonevents.party" in body
    # Expires is a required RFC 9116 field and must be a future date.
    assert "Expires:" in body
