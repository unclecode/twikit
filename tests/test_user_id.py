import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from twikit import Client
from twikit.errors import NotFound


def settings_answers_404(client):
    calls = []

    async def settings():
        calls.append("settings")
        raise NotFound('status: 404, message: "{"errors":[{"message":"Sorry, that page does not exist","code":34}]}"')

    client.v11.settings = settings
    return calls


def test_user_id_falls_back_to_the_twid_cookie_when_settings_answers_404():
    client = Client()
    client.set_cookies({"auth_token": "a", "ct0": "b", "twid": "u%3D1974596626342686720"}, clear_cookies=True)
    calls = settings_answers_404(client)
    assert asyncio.run(client.user_id()) == "1974596626342686720"
    assert asyncio.run(client.user_id()) == "1974596626342686720"
    assert calls == ["settings"]


def test_user_id_raises_the_original_error_with_no_twid_cookie():
    client = Client()
    client.set_cookies({"auth_token": "a", "ct0": "b"}, clear_cookies=True)
    settings_answers_404(client)
    with pytest.raises(NotFound):
        asyncio.run(client.user_id())


def test_user_id_still_uses_settings_when_it_answers():
    client = Client()
    client.set_cookies({"twid": "u%3D1"}, clear_cookies=True)

    async def settings():
        return {"screen_name": "someone"}, None

    class User:
        id = "42"

    async def by_name(name):
        assert name == "someone"
        return User()

    client.v11.settings = settings
    client.get_user_by_screen_name = by_name
    assert asyncio.run(client.user_id()) == "42"
