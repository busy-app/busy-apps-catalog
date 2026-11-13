"""Offline tests of the real-bar helper: validation, UART log, screens, smoke run."""

import base64
import struct
import zlib
from pathlib import Path

import pytest

APP = "app.zzq.bartest"
HEX = "a" * 64


def request() -> dict:
    return {
        "head_sha": "1" * 40,
        "run_id": 4242,
        "run_attempt": 2,
        "app_id": APP,
        "version": "3.1.4",
        "package_sha256": "7" * 64,
        "firmware": {"target": "f22", "version": "1.4.1", "sha256": "9" * 64},
    }


def result() -> dict:
    req = request()
    return {
        **{
            k: req[k]
            for k in (
                "head_sha",
                "run_id",
                "run_attempt",
                "app_id",
                "version",
                "package_sha256",
            )
        },
        "status": "pass",
        "cleanup": "pass",
        "launch": "accepted",
        "observed_seconds": 60,
        "script_seen": True,
        "log_complete": True,
        "runtime_errors": [],
        "artifacts": {"app.log": HEX, "front.png": HEX, "back.png": HEX},
        "firmware": dict(req["firmware"]),
    }


def test_validate_request_valid() -> None:
    from tools.bar import validate_request

    validate_request(request())


@pytest.mark.parametrize(
    "field",
    ["head_sha", "run_id", "run_attempt", "app_id", "version", "package_sha256"],
)
def test_validate_request_each_field_broken(field: str) -> None:
    from tools.bar import validate_request

    bad = request()
    bad[field] = "bad id!" if field == "app_id" else "zz"
    if field in ("run_id", "run_attempt"):
        bad[field] = "x"
    with pytest.raises(ValueError):
        validate_request(bad)
    missing = request()
    del missing[field]
    with pytest.raises(ValueError):
        validate_request(missing)


def test_validate_result_valid() -> None:
    from tools.bar import validate_result

    validate_result(result(), request())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "fail"),
        ("cleanup", "fail"),
        ("launch", "refused"),
        ("observed_seconds", 59),
        ("script_seen", False),
        ("log_complete", False),
        ("runtime_errors", ["js_exception"]),
        ("artifacts", {"app.log": HEX}),
        ("artifacts", {"app.log": HEX, "front.png": HEX, "back.png": "nothex"}),
        ("firmware", {"target": "f99", "version": "1.4.1", "sha256": HEX}),
        ("firmware", {"target": "f22", "version": "1.4", "sha256": HEX}),
        ("firmware", {"target": "f22", "version": "1.4.1", "sha256": "short"}),
    ],
)
def test_validate_result_each_field_broken(field: str, value) -> None:
    from tools.bar import validate_result

    bad = result()
    bad[field] = value
    with pytest.raises(ValueError):
        validate_result(bad, request())


@pytest.mark.parametrize(
    "field",
    ["head_sha", "run_id", "run_attempt", "app_id", "version", "package_sha256"],
)
def test_validate_result_of_another_package(field: str) -> None:
    from tools.bar import validate_result

    other = result()
    other[field] = (
        99
        if field in ("run_id", "run_attempt")
        else "f" * (40 if field == "head_sha" else 8)
    )
    if field == "version":
        other[field] = "9.9.9"
    if field == "package_sha256":
        other[field] = "0" * 64
    with pytest.raises(ValueError, match="belongs to another package"):
        validate_result(other, request())


# ---- ApplicationLog -------------------------------------------------------


def make_log(tmp_path: Path, initial: str = ""):
    from tools.bar import ApplicationLog

    source = tmp_path / "uart.log"
    source.write_text(initial)
    output = tmp_path / "app.log"
    return source, output, ApplicationLog(source, APP, output)


def append(path: Path, text: str) -> None:
    with path.open("a") as handle:
        handle.write(text)


def err_kind(exc: pytest.ExceptionInfo) -> str:
    return getattr(exc.value, "kind", None) or str(exc.value)


def test_log_starts_at_construction_size_and_filters(tmp_path: Path) -> None:
    source, output, log = make_log(tmp_path, f"[I][{APP}] old line\n")
    append(
        source,
        f"[I][{APP}] hello zq\n[I][app.other.app] foreign zq\n"
        + f"[I][JsRunner] Running script: /ext/apps/{APP}/scripts/main.js\n"
        + "[I][JsRunner] runner zq\n[I][JsAppLauncher] launcher zq\n"
        "[I][JsInput] input zq\n[I][JsInterval] interval zq\n",
    )
    log.poll()
    text = output.read_text()
    assert "hello zq" in text and "runner zq" in text and "launcher zq" in text
    assert "input zq" in text and "interval zq" in text
    assert "foreign zq" not in text and "old line" not in text
    assert not log.errors


def test_log_js_exception(tmp_path: Path) -> None:
    source, _, log = make_log(tmp_path)
    append(source, f"[E][{APP}] Uncaught TypeError: x is not a function\n")
    log.poll()
    assert "js_exception" in log.errors


def test_log_jsrunner_error(tmp_path: Path) -> None:
    source, _, log = make_log(tmp_path)
    append(
        source,
        f"[I][JsRunner] Running script: /ext/apps/{APP}/scripts/main.js\n"
        "[E][JsRunner] Script Error: boom\n",
    )
    log.poll()
    assert "js_exception" in log.errors


@pytest.mark.parametrize(
    "line",
    [
        "[E][Flipper] HardFault at 0x1\n",
        "[W][Flipper] Rebooting\n",
        "[I][Flipper] Startup complete\n",
    ],
)
def test_log_crash_or_restart(tmp_path: Path, line: str) -> None:
    source, _, log = make_log(tmp_path)
    append(source, line)
    log.poll()
    assert "bar_crash_or_restart" in log.errors


def test_log_script_seen(tmp_path: Path) -> None:
    source, _, log = make_log(tmp_path)
    assert not log.script_seen
    append(source, f"[I][JsRunner] Running script: /ext/apps/{APP}/scripts/main.js\n")
    log.poll()
    assert log.script_seen


def test_log_truncated_source(tmp_path: Path) -> None:
    from tools.bar import ObservationError

    source, _, log = make_log(tmp_path, "x" * 500 + "\n")
    source.write_text("short\n")
    with pytest.raises(ObservationError) as exc:
        log.poll()
    assert "serial_log_lost" in err_kind(exc)


def test_log_flood_single_read(tmp_path: Path) -> None:
    from tools.bar import ObservationError

    source, _, log = make_log(tmp_path)
    append(source, ("[I][Flipper] z\n" * 200000))  # ~3 MB, > 2 MiB
    with pytest.raises(ObservationError) as exc:
        log.poll()
    assert "serial_log_flood" in err_kind(exc)


def test_log_flood_total(tmp_path: Path) -> None:
    from tools.bar import ObservationError

    source, _, log = make_log(tmp_path)
    chunk = "[I][Flipper] z\n" * 100000  # 1.5 MB per poll, under single limit
    with pytest.raises(ObservationError) as exc:
        for _ in range(8):
            append(source, chunk)
            log.poll()
    assert "serial_log_flood" in err_kind(exc)


def test_log_line_too_long(tmp_path: Path) -> None:
    from tools.bar import ObservationError

    source, _, log = make_log(tmp_path)
    append(source, "[I][Flipper] " + "y" * (64 * 1024 + 10))  # no newline
    with pytest.raises(ObservationError) as exc:
        log.poll()
    assert "serial_line_too_long" in err_kind(exc)


def test_log_uart_loss(tmp_path: Path) -> None:
    from tools.bar import ObservationError

    source, _, log = make_log(tmp_path)
    append(source, "[u5] serial read error\n")
    with pytest.raises(ObservationError) as exc:
        log.poll()
    assert "uart_unavailable" in err_kind(exc)


# ---- save_screen ----------------------------------------------------------


def decode_png(path: Path) -> tuple[int, int, list[tuple[int, int, int]]]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, header = 8, b"", None
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        pos += 12 + length
    width, height, depth, color = header[:4]
    assert depth == 8 and color in (0, 2, 6)
    channels = {0: 1, 2: 3, 6: 4}[color]
    raw = zlib.decompress(idat)
    stride = width * channels
    rows, prev = [], bytes(stride)
    for y in range(height):
        base = y * (stride + 1)
        ftype, line = raw[base], bytearray(raw[base + 1 : base + 1 + stride])
        for i in range(stride):
            a = line[i - channels] if i >= channels else 0
            b = prev[i]
            c = prev[i - channels] if i >= channels else 0
            if ftype == 1:
                line[i] = (line[i] + a) & 255
            elif ftype == 2:
                line[i] = (line[i] + b) & 255
            elif ftype == 3:
                line[i] = (line[i] + (a + b) // 2) & 255
            elif ftype == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else b if pb <= pc else c
                line[i] = (line[i] + pred) & 255
        prev = bytes(line)
        rows.append(prev)
    pixels = []
    for row in rows:
        for x in range(width):
            px = row[x * channels : (x + 1) * channels]
            pixels.append((px[0], px[0], px[0]) if channels == 1 else tuple(px[:3]))
    return width, height, pixels


def test_save_screen_front_bgr_red(tmp_path: Path) -> None:
    from tools.bar import save_screen

    raw = bytes([0, 0, 255]) * (72 * 16)
    path = tmp_path / "front.png"
    save_screen(base64.b64encode(raw).decode(), 0, path)
    width, height, pixels = decode_png(path)
    assert (width, height) == (72, 16)
    assert set(pixels) == {(255, 0, 0)}


def test_save_screen_back_l4_low_nibble_first(tmp_path: Path) -> None:
    from tools.bar import save_screen

    raw = bytes([0x2B]) * 6400  # low nibble 0xB first, high nibble 0x2 second
    path = tmp_path / "back.png"
    save_screen(base64.b64encode(raw).decode(), 1, path)
    width, height, pixels = decode_png(path)
    assert (width, height) == (160, 80)
    assert pixels[0] == (0xB * 17,) * 3 and pixels[1] == (0x2 * 17,) * 3


@pytest.mark.parametrize("display", [0, 1])
def test_save_screen_wrong_size(tmp_path: Path, display: int) -> None:
    from tools.bar import save_screen

    with pytest.raises(ValueError):
        save_screen(base64.b64encode(b"\0" * 10).decode(), display, tmp_path / "x.png")


def test_save_screen_bad_base64(tmp_path: Path) -> None:
    from tools.bar import save_screen

    with pytest.raises(ValueError):
        save_screen("!!!not base64!!!", 0, tmp_path / "x.png")


# ---- smoke ----------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.hooks = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        for hook in list(self.hooks):
            hook(self.now)


class FakeDevice:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.calls: list[tuple[str, str]] = []
        self.fail: dict[tuple[str, str], Exception] = {}
        self.boot_time = 1_700_000_000
        self.installed = False
        self.list_version = "3.1.4"
        self.deleted = False

    def call(self, method, path, *, query=None, data=None, timeout=3):
        key = (method, path)
        self.calls.append(key)
        if key in self.fail:
            raise self.fail[key]
        if key == ("POST", "apps/install"):
            self.installed = True
        if key == ("DELETE", "apps"):
            self.deleted = True
        table = {
            ("GET", "transport"): {"type": "usb"},
            ("GET", "account/info"): {"linked": False},
            ("GET", "wifi/status"): {"state": "disconnected"},
            ("GET", "storage/list"): {"list": []},
            ("GET", "status"): {
                "firmware": {"version": "1.4.1", "commit_hash": "abc", "target": "f22"},
                "device": {"serial_number": "SN-ZQ-1"},
                "system": {"uptime": "0d 0h 1m 0s", "boot_time": self.boot_time},
            },
            ("POST", "apps/stage"): {
                "staged": {"id": APP, "version": "3.1.4"},
                "install_key": 17,
            },
            ("POST", "apps/install"): {},
            ("GET", "apps/list"): {
                "apps": []
                if self.deleted
                else [{"id": APP, "version": self.list_version}]
            },
            ("POST", "apps/launch"): {},
            ("DELETE", "apps"): {},
            ("DELETE", "storage/remove"): {},
        }
        return table.get(key, {})

    def screen(self, display):
        size = 3456 if display == 0 else 6400
        return base64.b64encode(bytes(size))


from tools.bar import DeviceError


def run_smoke(tmp_path: Path, configure=None, on_tick=None):
    from tools.bar import smoke

    clock = Clock()
    device = FakeDevice(clock)
    package = tmp_path / "package.tgz"
    package.write_bytes(b"pkg-zq")
    serial = tmp_path / "uart.log"
    serial.write_text("")
    out = tmp_path / "out"
    out.mkdir()
    if configure:
        configure(device, serial)
    if on_tick:
        clock.hooks.append(lambda now: on_tick(device, serial, now))
    res = smoke(device, request(), package, serial, out, clock=clock, sleep=clock.sleep)
    return res, device, out


def test_smoke_good_minute(tmp_path: Path) -> None:
    def tick(device, serial, now):
        if not getattr(tick, "done", False):
            tick.done = True
            append(
                serial,
                f"[I][JsRunner] Running script: /ext/apps/{APP}/scripts/main.js\n",
            )

    res, device, out = run_smoke(tmp_path, on_tick=tick)
    assert res["status"] == "pass" and res["cleanup"] == "pass"
    assert res["launch"] == "accepted" and res["observed_seconds"] == 60
    assert res["script_seen"] is True and res["log_complete"] is True
    assert res["runtime_errors"] == []
    assert ("DELETE", "apps") in device.calls


@pytest.mark.parametrize(
    ("step", "key"),
    [
        ("stage", ("POST", "apps/stage")),
        ("install", ("POST", "apps/install")),
        ("launch", ("POST", "apps/launch")),
    ],
)
def test_smoke_step_failure(tmp_path: Path, step: str, key) -> None:
    def configure(device, serial):
        device.fail[key] = DeviceError(403)

    res, device, _ = run_smoke(tmp_path, configure)
    assert res["status"] == "fail"
    assert res["error"]["step"] == step


def test_smoke_runtime_error(tmp_path: Path) -> None:
    def tick(device, serial, now):
        if not getattr(tick, "done", False):
            tick.done = True
            append(serial, f"[E][{APP}] Uncaught ReferenceError: q\n")

    res, _, _ = run_smoke(tmp_path, on_tick=tick)
    assert "js_exception" in res["runtime_errors"]
    assert res["status"] == "fail"


def test_smoke_bar_reboot(tmp_path: Path) -> None:
    def tick(device, serial, now):
        device.boot_time += 500

    res, _, _ = run_smoke(tmp_path, on_tick=tick)
    assert res["status"] == "fail"
    assert res["error"]["kind"] == "bar_rebooted"


def test_smoke_cleanup_failure(tmp_path: Path) -> None:
    def configure(device, serial):
        device.fail[("DELETE", "apps")] = DeviceError(500)

    res, _, _ = run_smoke(tmp_path, configure)
    assert res["cleanup"] == "fail"


def test_smoke_cleanup_runs_after_install_then_failure(tmp_path: Path) -> None:
    def configure(device, serial):
        device.fail[("POST", "apps/launch")] = DeviceError(500)

    res, device, _ = run_smoke(tmp_path, configure)
    assert ("DELETE", "apps") in device.calls
    assert res["cleanup"] == "pass"


def test_smoke_no_cleanup_when_nothing_installed(tmp_path: Path) -> None:
    def configure(device, serial):
        device.fail[("POST", "apps/stage")] = DeviceError(500)

    _, device, _ = run_smoke(tmp_path, configure)
    # Cleanup also runs after a refused stage (the data was removed first); a 404 is fine.
    assert ("DELETE", "apps") in device.calls and not device.installed


def test_smoke_identity_change_is_not_a_reboot(tmp_path: Path) -> None:
    def tick(device, serial, now):
        device.serial = "SN-OTHER"

    original = FakeDevice.call

    def call(self, method, path, **kw):
        value = original(self, method, path, **kw)
        if (method, path) == ("GET", "status") and getattr(self, "serial", None):
            value = {**value, "device": {"serial_number": self.serial}}
        return value

    FakeDevice.call = call
    try:
        res, _, _ = run_smoke(tmp_path, on_tick=tick)
    finally:
        FakeDevice.call = original
    assert res["error"]["kind"] == "bar_identity_changed"


def test_smoke_installed_version_mismatch_kind(tmp_path: Path) -> None:
    def configure(device, serial):
        device.list_version = "0.0.1"

    res, _, _ = run_smoke(tmp_path, configure)
    assert (
        res["error"]["step"] == "list"
        and res["error"]["kind"] == "installed_version_mismatch"
    )


def test_smoke_silent_log_is_not_script_not_seen(tmp_path: Path) -> None:
    res, _, _ = run_smoke(tmp_path)
    assert res["script_seen"] is False and res["error"]["kind"] == "serial_log_silent"


def test_smoke_log_without_our_script_is_script_not_seen(tmp_path: Path) -> None:
    def tick(device, serial, now):
        if not getattr(tick, "done", False):
            tick.done = True
            append(serial, "[I][Other] something unrelated\n")

    res, _, _ = run_smoke(tmp_path, on_tick=tick)
    assert res["error"]["kind"] == "script_not_seen"
