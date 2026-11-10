"""Photo button: capturing (the cam tool and ffmpeg are faked), sending to the hub, the waiting photo's
lifetime, and what a typed or spoken question does to it. Runs like test_barge_in.py (same stubs).
"""
import asyncio
import json
import time
import urllib.error

import pytest

import test_barge_in  # noqa: F401  (stubs the audio stack)
from thinksmart_client import app, camera


def published(c):
    """Every event the client has published so far, oldest first."""
    q, out = c.events_q, []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def make_client():
    c = test_barge_in.make_client()
    c.cfg = {'hub_http': 'http://127.0.0.1:1', 'device_id': 'dev-1', 'follow_up_seconds': 10}
    c.camera = camera.Camera()
    c.photo = None
    c.events_q = asyncio.Queue()
    c.events.queues.add(c.events_q)
    return c


class FakeCamera:
    """Stands in for camera.Camera.capture: returns bytes or raises."""

    def __init__(self, result=b'\xff\xd8\xff\xe0' + b'x' * 100, error=None):
        self.result, self.error = result, error

    async def capture(self):
        if self.error:
            raise camera.CameraError(self.error)
        return self.result


def fake_hub(monkeypatch, answer=None, error=None):
    calls = []

    def send(hub_http, device_id, jpeg, timeout=15):
        calls.append((hub_http, device_id, jpeg))
        if error:
            raise camera.CameraError(error)
        return answer or {'ok': True, 'model': '智谱 GLM-4V'}

    monkeypatch.setattr(camera, 'send_to_hub', send)
    return calls


def test_take_photo_keeps_picture_and_announces_it(monkeypatch):
    c = make_client()
    c.camera = FakeCamera()
    calls = fake_hub(monkeypatch)
    r = asyncio.run(c.take_photo())
    assert r == {'ok': True, 'model': '智谱 GLM-4V', 'seconds': 120}
    assert calls and calls[0][1] == 'dev-1'
    assert c.photo_jpeg() is not None
    assert published(c)[-1] == ('photo', {'on': True, 'model': '智谱 GLM-4V', 'seconds': 120})
    view = c._photo_view()
    assert view['on'] and view['model'] == '智谱 GLM-4V' and 118 <= view['seconds'] <= 120


def test_camera_failure_reaches_the_page_as_a_sentence(monkeypatch):
    c = make_client()
    c.camera = FakeCamera(error='摄像头没有找到，请确认摄像头驱动已启用')
    fake_hub(monkeypatch)
    r = asyncio.run(c.take_photo())
    assert r == {'ok': False, 'error': '摄像头没有找到，请确认摄像头驱动已启用'}
    assert c.photo is None and published(c) == []


def test_hub_refusal_is_shown_and_no_photo_is_kept(monkeypatch):
    c = make_client()
    c.camera = FakeCamera()
    fake_hub(monkeypatch, error='还没配置视觉模型，请先在管理后台的“视觉模型”页填好 API Key')
    r = asyncio.run(c.take_photo())
    assert not r['ok'] and '视觉模型' in r['error']
    assert c.photo is None


def test_unexpected_error_still_gives_a_sentence(monkeypatch):
    c = make_client()

    class Broken:
        async def capture(self):
            raise RuntimeError('boom')

    c.camera = Broken()
    r = asyncio.run(c.take_photo())
    assert r == {'ok': False, 'error': '拍照失败，请稍后再试'}


def test_photo_expires_on_the_client_too():
    c = make_client()
    c.photo = {'jpeg': b'x', 'at': time.monotonic() - app.Client.PHOTO_SECONDS - 1, 'model': 'm'}
    assert c.photo_jpeg() is None and c.photo is None
    assert c._photo_view() == {'on': False}


@pytest.mark.asyncio
async def test_typed_question_uses_up_the_photo():
    c = make_client()
    c.photo = {'jpeg': b'x', 'at': time.monotonic(), 'model': 'm'}
    c.tools_listed = asyncio.Event()
    c.tools_listed.set()
    await c.say('这是什么')
    assert c.photo is None
    assert ('photo', {'on': False}) in published(c)


@pytest.mark.asyncio
async def test_recognised_speech_uses_up_the_photo():
    c = make_client()
    c.photo = {'jpeg': b'x', 'at': time.monotonic(), 'model': 'm'}
    await c.start_listening('realtime')
    await c._on_json({'type': 'stt', 'text': '这道题对不对'})
    assert c.photo is None
    assert ('photo', {'on': False}) in published(c)


@pytest.mark.asyncio
async def test_speech_without_a_photo_changes_nothing():
    c = make_client()
    await c.start_listening('realtime')
    await c._on_json({'type': 'stt', 'text': '今天天气怎么样'})
    assert not [e for e in published(c) if e[0] == 'photo']


def test_capture_is_refused_while_another_runs():
    cam = camera.Camera()

    async def run():
        async with cam._lock:
            with pytest.raises(camera.CameraError, match='正在拍照'):
                await cam.capture()

    asyncio.run(run())


def test_camera_index_is_read_from_cam_list(monkeypatch):
    cam = camera.Camera()

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return (b'Available cameras:\n1: Internal front camera (/base/camera@37)\n', b'')

    async def fake_exec(*args, **kw):
        assert args[-1] == '-l'
        return FakeProc()

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', fake_exec)
    assert asyncio.run(cam._camera_index()) == '1'


def test_no_camera_listed_is_a_clear_error(monkeypatch):
    cam = camera.Camera()

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return (b'Available cameras:\n', b'')

    async def fake_exec(*args, **kw):
        return FakeProc()

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', fake_exec)
    with pytest.raises(camera.CameraError, match='没有找到'):
        asyncio.run(cam._camera_index())


def test_send_to_hub_maps_refusal_to_the_hub_sentence(monkeypatch):
    def refuse(req, timeout=None):
        import io
        raise urllib.error.HTTPError(req.full_url, 409, 'Conflict', {},
                                     io.BytesIO(json.dumps({'ok': False, 'error': '还没配置视觉模型'}).encode()))

    monkeypatch.setattr(camera.urllib.request, 'urlopen', refuse)
    with pytest.raises(camera.CameraError, match='还没配置视觉模型'):
        camera.send_to_hub('http://x', 'dev', b'\xff\xd8\xff')


def test_send_to_hub_says_when_the_gateway_is_down(monkeypatch):
    def down(req, timeout=None):
        raise urllib.error.URLError('refused')

    monkeypatch.setattr(camera.urllib.request, 'urlopen', down)
    with pytest.raises(camera.CameraError, match='连不上网关'):
        camera.send_to_hub('http://x', 'dev', b'\xff\xd8\xff')
