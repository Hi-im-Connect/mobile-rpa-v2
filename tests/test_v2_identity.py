from mobile_rpa import appconnect
from mobile_rpa.settings import load_env

from .test_app import client, login  # noqa: F401  (fixture + helper)


def test_v2_identity(monkeypatch, tmp_path):
    monkeypatch.setenv("MRPA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("MRPA_PUBLIC_URL", raising=False)
    assert load_env().public_url == "https://digimate.fastautomate.com/mobile2"
    assert appconnect.APP_PACKAGE == "com.fastautomate.agent"
    assert appconnect.CALLBACK_SCHEMES == {"fastautomate2"}
    assert appconnect.APK.name == "fa-portal-v2.apk"


def test_connect_page_opens_the_v2_app(client):  # noqa: F811
    login(client)
    url = client.post("/api/app/invite").json()["url"]
    page = client.get("/connect?t=" + url.split("t=")[1]).text
    assert "fastautomate2://connect" in page
    assert "scheme=fastautomate2;package=com.fastautomate.agent" in page
    assert "/app/FastAutomate-v2.apk" in page
    assert client.get("/connect/device?deviceId=d1").text.count('value="fastautomate2"') == 1
