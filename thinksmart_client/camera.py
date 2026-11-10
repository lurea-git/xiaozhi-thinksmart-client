"""The front camera: one still photo as JPEG, and handing it to the hub.

libcamera's `cam` tool opens the sensor, runs a few frames so the exposure settles, and writes the
last one as PPM; ffmpeg turns it into JPEG. 1304x980 because the full 2600x1960 frame does not fit
the device's memory. Only one capture runs at a time (the sensor has a single user).
"""
import asyncio
import logging
import re
import shutil
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

CAM = shutil.which('cam') or '/usr/local/bin/cam'
SIZE = 'width=1304,height=980'
FRAMES = 20           # auto exposure needs about this many frames from a cold start
MAX_BYTES = 900 * 1024   # the hub takes at most this
CAPTURE_TIMEOUT = 20
# libcamera log lines that do not matter to the person at the screen
KNOWN_ERRORS = (
    ('not found', '摄像头没有找到，请确认摄像头驱动已启用'),
    ('allocation failure', '内存不够，暂时拍不了照'),
    ('Device or resource busy', '摄像头正在被别的程序使用'),
    ('Permission denied', '没有权限使用摄像头'),
)


class CameraError(Exception):
    """Carries a sentence meant for the screen."""


def _explain(stderr):
    for needle, sentence in KNOWN_ERRORS:
        if needle in stderr:
            return sentence
    return '拍照失败，请稍后再试'


async def _run(*args, timeout):
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL,
                                                stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode, err.decode('utf-8', 'replace')


class Camera:
    def __init__(self):
        self._lock = asyncio.Lock()

    @property
    def busy(self):
        return self._lock.locked()

    async def _camera_index(self):
        """The number `cam -l` gives the first camera ("1: Internal front camera ..."); it is 1 here, not 0."""
        proc = await asyncio.create_subprocess_exec(CAM, '-l', stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 10)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        m = re.search(r'^\s*(\d+): ', out.decode('utf-8', 'replace'), re.M)
        if not m:
            raise CameraError('摄像头没有找到，请确认摄像头驱动已启用')
        return m.group(1)

    async def capture(self):
        """Take a photo; returns JPEG bytes. Raises CameraError with a sentence for the screen."""
        if self._lock.locked():
            raise CameraError('正在拍照，请稍等')
        async with self._lock:
            with tempfile.TemporaryDirectory(prefix='tscam-') as tmp:
                ppm, jpg = Path(tmp) / 'f-#.ppm', Path(tmp) / 'photo.jpg'
                try:
                    index = await self._camera_index()
                    code, err = await _run(CAM, '--camera', index, f'--capture={FRAMES}', '--stream', SIZE,
                                           f'--file={ppm}', timeout=CAPTURE_TIMEOUT)
                except asyncio.TimeoutError:
                    raise CameraError('拍照超时，请再试一次')
                except OSError as e:
                    log.error('cannot start cam: %s', e)
                    raise CameraError('摄像头工具没有安装')
                frames = sorted(Path(tmp).glob('f-*.ppm'))
                if code != 0 or not frames:
                    log.error('cam failed (%s): %s', code, err[-400:])
                    raise CameraError(_explain(err))
                try:
                    code, err = await _run('ffmpeg', '-v', 'error', '-y', '-i', str(frames[-1]),
                                           '-q:v', '4', str(jpg), timeout=15)
                except (asyncio.TimeoutError, OSError):
                    raise CameraError('照片处理失败')
                if code != 0 or not jpg.exists():
                    log.error('ffmpeg failed: %s', err[-300:])
                    raise CameraError('照片处理失败')
                data = jpg.read_bytes()
            if len(data) > MAX_BYTES:
                log.warning('photo is %d KB, over the hub limit', len(data) // 1024)
                raise CameraError('照片太大，暂时发不出去')
            log.info('photo taken: %d KB', len(data) // 1024)
            return data


def send_to_hub(hub_http, device_id, jpeg, timeout=15):
    """POST the photo to the hub (blocking: run it in a thread). Returns the hub's answer as a dict."""
    import json
    from urllib.parse import quote

    req = urllib.request.Request(f'{hub_http}/kiosk/photo?device={quote(device_id)}', data=jpeg,
                                 headers={'Content-Type': 'image/jpeg'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            message = json.loads(e.read()).get('error')
        except ValueError:
            message = None
        raise CameraError(message or f'网关拒绝了照片（{e.code}）')
    except (urllib.error.URLError, OSError, TimeoutError):
        raise CameraError('连不上网关，照片没发出去')


def discard_on_hub(hub_http, device_id, timeout=5):
    from urllib.parse import quote

    req = urllib.request.Request(f'{hub_http}/kiosk/photo?device={quote(device_id)}', method='DELETE')
    try:
        urllib.request.urlopen(req, timeout=timeout).read()
    except (urllib.error.URLError, OSError, TimeoutError):
        pass
