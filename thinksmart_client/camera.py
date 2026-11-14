"""The front camera: one still photo per request, from thinksmart-camera.service.

The capture itself runs outside this process: the client's unit is sandboxed away from /dev
(PrivateDevices=yes; PortAudio probing a hardware PCM breaks the q6asm DSP), so libcamera cannot
see the sensor here. thinksmart-camera.service holds the camera device classes and hands back one
JPEG per GET /photo. This module just asks it and talks to the hub.
"""
import array
import fcntl
import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

SERVICE = 'http://127.0.0.1:8091'
PHOTO_TIMEOUT = 40   # ~20 frames to settle the exposure, then ffmpeg
# gpio-keys also carries the lens cover slider (gpio87 "Camera Lens Cover", active-low) as
# SW_CAMERA_LENS_COVER: 1 = covered. Same input device and ioctl as the mic switch (device.Keys).
SW_LENS_COVER = 9
LENS_COVERED = '镜头盖关着，推开镜头盖再拍'


class CameraError(Exception):
    """Carries a sentence meant for the screen."""


def _ask_for_photo(service, timeout):
    try:
        with urllib.request.urlopen(f'{service}/photo', timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        try:
            message = json.loads(e.read()).get('error')
        except ValueError:
            message = None
        raise CameraError(message or '拍照失败，请稍后再试')
    except (urllib.error.URLError, OSError, TimeoutError):
        raise CameraError('拍照服务没有运行')


def lens_covered(device='gpio-keys'):
    """True when the lens cover is closed. False when it is open or cannot be read: a photo is
    never refused on a guess (the sandbox binds /dev/input read-only, so this normally works)."""
    from .device import EVIOCGSW, input_device

    path = input_device(device)
    if not path:
        return False
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        bits = array.array('B', [0] * 8)
        fcntl.ioctl(fd, EVIOCGSW, bits)
    except OSError:
        return False
    finally:
        os.close(fd)
    return bool(int.from_bytes(bytes(bits), 'little') >> SW_LENS_COVER & 1)


class Camera:
    """One capture at a time (the sensor has a single user); the daemon enforces this too."""

    def __init__(self, service=SERVICE):
        self.service = service
        self._busy = False

    @property
    def busy(self):
        return self._busy

    async def capture(self):
        """Take a photo; returns JPEG bytes. Raises CameraError with a sentence for the screen."""
        import asyncio

        if self._busy:
            raise CameraError('正在拍照，请稍等')
        if lens_covered():
            raise CameraError(LENS_COVERED)   # the sensor would only see black
        self._busy = True
        try:
            data = await asyncio.get_running_loop().run_in_executor(
                None, _ask_for_photo, self.service, PHOTO_TIMEOUT)
        except CameraError:
            raise
        except Exception as e:  # noqa: BLE001
            log.error('photo failed: %r', e)
            raise CameraError('拍照失败，请稍后再试')
        finally:
            self._busy = False
        log.info('photo: %d KB', len(data) // 1024)
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
