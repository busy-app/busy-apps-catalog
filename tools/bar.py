"""Run a checked package for one minute on BusyBarTest and retain application evidence."""

import argparse
import base64
import hashlib
import ipaddress
import json
import os
import re
import struct
import time
import zlib
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

DIRECTORY = "https://update.busy.app/busybar-firmware/directory.json"
LIMIT = 128 * 1024 * 1024
BINDING = ("head_sha", "run_id", "run_attempt", "app_id", "version", "package_sha256")
ARTIFACTS = {"app.log", "front.png", "back.png"}


def digest(path: Path) -> str:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > LIMIT:
        raise ValueError("Invalid package artifact")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path: Path) -> dict:
    if path.is_symlink() or path.stat().st_size > 1024 * 1024:
        raise ValueError("Invalid result artifact")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def validate_request(data: dict) -> None:
    if (
        not all(isinstance(data.get(k), str) for k in ("head_sha", "package_sha256", "app_id", "version"))
        or not re.fullmatch(r"[0-9a-f]{40}", str(data.get("head_sha", "")))
        or not re.fullmatch(r"[0-9a-f]{64}", str(data.get("package_sha256", "")))
        or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,31}", str(data.get("app_id", "")))
        or not re.fullmatch(r"\d+\.\d+\.\d+", str(data.get("version", "")))
        or any(type(data.get(k)) is not int or data[k] < 1 for k in ("run_id", "run_attempt"))
    ):
        raise ValueError("Invalid hardware request")


def validate_result(result: dict, expected: dict) -> None:
    """Require a complete healthy minute, application evidence and successful cleanup."""
    validate_request(result)
    if any(result.get(k) != expected.get(k) for k in BINDING):
        raise ValueError("Hardware result belongs to another package or CI attempt")
    firmware = result.get("firmware", {})
    artifacts = result.get("artifacts", {})
    if (
        result.get("status") != "pass"
        or result.get("cleanup") != "pass"
        or result.get("launch") != "accepted"
        or type(result.get("observed_seconds")) is not int
        or result["observed_seconds"] != 60
        or result.get("script_seen") is not True
        or result.get("log_complete") is not True
        or result.get("runtime_errors") != []
        or not isinstance(artifacts, dict)
        or set(artifacts) != ARTIFACTS
        or any(not re.fullmatch(r"[0-9a-f]{64}", str(value)) for value in artifacts.values())
        or not isinstance(firmware, dict)
        or firmware.get("target") not in ("f21", "f22")
        or not re.fullmatch(r"\d+\.\d+\.\d+", str(firmware.get("version", "")))
        or not re.fullmatch(r"[0-9a-f]{64}", str(firmware.get("sha256", "")))
    ):
        raise ValueError("BAR smoke check or cleanup did not pass")


APP_ERRORS = {"js_exception", "bar_crash_or_restart"}
# The bar answers these to a package it will not take; 408/409/429/5xx are the stand's.
PACKAGE_REFUSED = {400, 413, 415, 422}


def application_fault(result: dict) -> str | None:
    """
    What the application did wrong, read from the result's fields only; None if unsure.

    A fault of the stand (lost or silent UART, Wi-Fi, cleanup, screenshots, a swapped or
    re-flashed bar, a bar that did not answer) is never the author's, so anything not
    listed here is not a fault. Malformed fields count as "unsure".
    """
    launched = result.get("launch") == "accepted"
    errors = result.get("runtime_errors")
    seen = set()
    if isinstance(errors, list):
        seen = {item for item in errors if isinstance(item, str)} & APP_ERRORS
    # A restart is the app's only once its script was seen running.
    if not (launched and result.get("script_seen") is True):
        seen.discard("bar_crash_or_restart")
    if seen:
        return f"the application raised errors on the BAR ({', '.join(sorted(seen))})"
    error = result.get("error")
    if not isinstance(error, dict):
        return None
    step, kind, type_ = error.get("step"), error.get("kind"), error.get("type")
    status = error.get("http_status")
    if kind == "script_not_seen" and launched:
        return "the application was launched but its script never started"
    if kind == "bar_rebooted" and launched:
        return "the BAR restarted while the application was running"
    if kind == "installed_version_mismatch" and step == "list":
        return "the installed application is missing or has a different version"
    if (
        step in ("stage", "install")
        and type_ == "DeviceError"
        and type(status) is int
        and status in PACKAGE_REFUSED
    ):
        return f"the BAR refused to {step} the package (HTTP {status})"
    return None


def judge(result: dict, expected: dict) -> tuple[str, str]:
    """
    Verdict on a hardware result: ("pass" | "fail" | "not_run", why).

    "fail" only for a result of this very package and run that shows an application fault.
    A foreign, broken or stand-caused result is "not_run": it never passes and never fails
    the author.
    """
    if any(result.get(key) != expected.get(key) for key in BINDING):
        return "not_run", "the result belongs to another package or CI attempt"
    try:
        validate_result(result, expected)
        return "pass", ""
    except ValueError:
        pass
    try:
        fault = application_fault(result)
    except (TypeError, KeyError, AttributeError):
        fault = None
    return ("fail", fault) if fault else ("not_run", "the stand did not give a usable result")


class DeviceError(Exception):
    def __init__(self, status=0):
        super().__init__(f"HTTP {status}" if status else "Device unreachable")
        self.status = status


class ObservationError(ValueError):
    """A bounded, non-sensitive reason why observation cannot pass."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch(url, *, method="GET", data=None, timeout=15, limit=1024 * 1024):
    """No proxy credentials, redirects, cookies or unbounded device responses."""
    headers = {"User-Agent": "busy-apps-catalog-ci"}
    if isinstance(data, bytes):
        headers["Content-Type"] = "application/octet-stream"
    if isinstance(data, dict):
        headers["Content-Type"], data = "application/json", json.dumps(data).encode()
    try:
        opener = build_opener(ProxyHandler({}), NoRedirect())
        with opener.open(Request(url, data=data, method=method, headers=headers), timeout=timeout) as response:
            content = response.read(limit + 1)
            if len(content) > limit:
                raise ValueError("Response exceeds size limit")
            return content
    except HTTPError as error:
        raise DeviceError(error.code) from None
    except (URLError, OSError):
        raise DeviceError() from None


class Device:
    def __init__(self, address):
        ip = ipaddress.IPv4Address(address)
        if not any(ip in ipaddress.IPv4Network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")):
            raise ValueError("BAR must have a private IPv4 USB address")
        self.base = f"http://{ip}/api/"

    def call(self, method, path, *, query=None, data=None, timeout=3):
        value = json.loads(
            fetch(
                self.base + path + ("?" + urlencode(query) if query else ""), method=method, data=data, timeout=timeout
            )
        )
        if not isinstance(value, dict):
            raise ValueError("Device response must be an object")
        return value

    def screen(self, display):
        return fetch(self.base + "screen?" + urlencode({"display": display}), timeout=10, limit=20000)


def save_screen(content, display, path):
    """Convert the API's base64 BGR888 or packed L4 framebuffer to PNG."""
    raw = base64.b64decode(content, validate=True)
    width, height, channels = (72, 16, 3) if display == 0 else (160, 80, 1)
    if len(raw) != (3456 if display == 0 else 6400):
        raise ValueError("Unexpected screenshot size")
    if display == 0:
        pixels = bytearray(raw)
        pixels[0::3], pixels[2::3] = raw[2::3], raw[0::3]
    else:
        pixels = bytes(value for pair in raw for value in ((pair & 15) * 17, (pair >> 4) * 17))
    stride = width * channels
    rows = b"".join(b"\0" + pixels[start : start + stride] for start in range(0, len(pixels), stride))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 2 if channels == 3 else 0, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")
    )


class ApplicationLog:
    """Read new UART records, retaining only this application's logs and relevant faults."""

    def __init__(self, source, app_id, output):
        info = source.stat()
        if source.is_symlink() or not source.is_file():
            raise ObservationError("invalid_serial_log")
        self.source, self.app_id, self.output = source, app_id, output
        self.identity, self.offset = (info.st_dev, info.st_ino), info.st_size
        self.pending, self.read_bytes, self.written = b"", 0, 0
        self.script_seen, self.active, self.valid = False, False, True
        self.errors = set()

    def poll(self):
        try:
            info = self.source.stat()
            if (info.st_dev, info.st_ino) != self.identity or info.st_size < self.offset:
                raise ObservationError("serial_log_lost")
            with self.source.open("rb") as stream:
                stream.seek(self.offset)
                data = stream.read(2 * 1024 * 1024 + 1)
            self.offset += len(data)
            self.read_bytes += len(data)
            if len(data) > 2 * 1024 * 1024 or self.read_bytes > 8 * 1024 * 1024:
                raise ObservationError("serial_log_flood")
            lines = (self.pending + data).split(b"\n")
            self.pending = lines.pop()
            if len(self.pending) > 65536:
                raise ObservationError("serial_line_too_long")
            for raw in lines:
                line = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", raw.decode("utf-8", errors="replace"))
                line = "".join(c for c in line if c == "\t" or c.isprintable())
                record = re.search(r"\[([DIWE])\]\[([^\]\r\n]{1,64})\]\s*(.*)$", line)
                level, tag, message = record.groups() if record else ("", "", "")
                selected = tag == self.app_id
                if tag == "JsRunner" and message.startswith("Running script: "):
                    self.active = message.endswith(f"/{self.app_id}/scripts/main.js")
                    self.script_seen |= self.active
                    selected |= self.active
                runtime = tag in {"JsRunner", "JsAppLauncher", "JsInput", "JsInterval"}
                owned = self.active or (tag == "JsAppLauncher" and not self.script_seen)
                if runtime and owned:
                    selected = True
                if (
                    runtime
                    and (owned or self.script_seen)
                    and level == "E"
                    and re.search(r"Error|Exception|exception|Dead interval", message)
                ):
                    self.errors.add("js_exception")
                    selected = True
                if tag == self.app_id and level == "E" and message.startswith("Uncaught"):
                    self.errors.add("js_exception")
                if (tag == "Flipper" and re.search(r"Boot mode|Startup complete", message)) or (
                    tag != self.app_id and re.search(r"\[CRASH\]|HardFault|System halted|Rebooting", line)
                ):
                    self.errors.add("bar_crash_or_restart")
                    selected = True
                lost = re.search(
                    r"\[(?:u5|Si917)\] (?:serial read error|unable to open|reconnecting in|waiting for device)", line
                )
                if lost:
                    selected = True
                if selected:
                    encoded = (line + "\n").encode("utf-8")
                    self.written += len(encoded)
                    if self.written > 2 * 1024 * 1024:
                        raise ObservationError("application_log_flood")
                    with self.output.open("ab") as target:
                        target.write(encoded)
                if lost:
                    raise ObservationError("uart_unavailable")
        except (OSError, ValueError):
            self.valid = False
            raise


def latest_release(target="f22") -> tuple[dict, bytes]:
    if target not in {"f21", "f22"}:
        raise ValueError("Unsupported BAR firmware target")
    directory = json.loads(fetch(DIRECTORY))
    channels = [c for c in directory["channels"] if c["id"] == "release"]
    if len(channels) != 1:
        raise ValueError("Expected one release channel")
    release = channels[0]["versions"][0]
    if not isinstance(release["version"], str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", release["version"]):
        raise ValueError("Invalid release version")
    files = [f for f in release["files"] if f.get("target") == target and f.get("type") == "update_tgz"]
    if len(files) != 1 or not files[0]["url"].startswith("https://"):
        raise ValueError("Expected one unsigned release bundle for the BAR target over HTTPS")
    image = fetch(files[0]["url"], timeout=120, limit=LIMIT)
    checksum = hashlib.sha256(image).hexdigest()
    if not image or checksum != files[0]["sha256"].lower():
        raise ValueError("Firmware checksum mismatch")
    return {"version": release["version"], "target": target, "sha256": checksum}, image


def health(data):
    firmware, system = data["firmware"], data["system"]
    parts = re.fullmatch(r"(\d+)d\s+(\d+)h\s+(\d+)m\s+(\d+)s", system["uptime"])
    if not parts or type(system.get("boot_time")) is not int:
        raise ValueError("Missing device boot evidence")
    days, hours, minutes, seconds = map(int, parts.groups())
    identity = (data["device"]["serial_number"], firmware["version"], firmware["commit_hash"], system["boot_time"])
    if not all(isinstance(v, str) and v for v in identity[:3]) or hours >= 24 or minutes >= 60 or seconds >= 60:
        raise ValueError("Invalid device health")
    return identity, ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def remove_data(device, names):
    path = "/ext"
    for name in names:
        entries = device.call("GET", "storage/list", query={"path": path})["list"]
        if not any(entry["name"] == name for entry in entries):
            return
        path += "/" + name
    device.call("DELETE", "storage/remove", query={"path": path}, timeout=15)


def offline(device, *, clock=time.monotonic, sleep=time.sleep):
    if device.call("GET", "transport").get("type") != "usb":
        raise ValueError("BAR checks require USB Ethernet")
    if device.call("GET", "account/info").get("linked") is not False:
        raise ValueError("The test BAR must be unlinked")
    if device.call("GET", "wifi/status").get("state") != "disconnected":
        device.call("POST", "wifi/disconnect", timeout=15)
    deadline = clock() + 20
    while device.call("GET", "wifi/status").get("state") != "disconnected":
        if clock() >= deadline:
            raise ValueError("BAR did not disconnect Wi-Fi")
        sleep(1)
    # A disconnected BAR can still retain a saved password; reboot follows this deletion.
    remove_data(device, ("apps_data", "wifi", "settings.json"))


def provision(device, firmware, image, *, clock=time.monotonic, sleep=time.sleep):
    previous = health(device.call("GET", "status"))
    offline(device, clock=clock, sleep=sleep)
    try:
        device.call("POST", "update", query={"name": "firmware"}, data=image, timeout=120)
    except DeviceError as error:
        if error.status:
            raise
        # A reboot can interrupt the upload response. Never repeat that upload.
    deadline, ready = clock() + 320, 0
    while clock() < deadline:
        sleep(5)
        try:
            current = health(device.call("GET", "status"))
        except (DeviceError, ValueError, KeyError):
            ready = 0
            continue
        if current[0][0] != previous[0][0]:
            raise ValueError("BAR identity changed during firmware installation")
        rebooted = current[0][3] != previous[0][3] or current[1] < previous[1]
        ready = ready + 1 if rebooted and current[0][1] == firmware["version"] else 0
        if ready >= 2:
            offline(device, clock=clock, sleep=sleep)
            device.call("POST", "update/autoupdate", data={"is_enabled": False})
            device.call("POST", "input", query={"key": "start"})
            device.call("POST", "input", query={"key": "back"})
            return
    raise ValueError("BAR did not confirm reboot into the release firmware")


def smoke(device, request, package, serial_log, output_dir, *, clock=time.monotonic, sleep=time.sleep):
    """Only firmware APIs run the package; nothing from the app executes on the runner."""
    output_dir.mkdir(parents=True, exist_ok=True)
    log_output = output_dir / "app.log"
    log_output.write_text("", encoding="utf-8")
    result = {
        **request,
        "status": "fail",
        "cleanup": "not_run",
        "launch": "not_run",
        "observed_seconds": 0,
        "script_seen": False,
        "log_complete": False,
        "runtime_errors": [],
        "artifacts": {},
    }
    touched, completed, started, observer = False, False, None, None
    step = "prepare"

    def failed(phase, error):
        result["status"] = "fail"
        detail = {"step": phase, "type": type(error).__name__}
        if isinstance(error, DeviceError):
            detail["http_status"] = error.status
        if isinstance(error, ObservationError):
            detail["kind"] = str(error)
        result.setdefault("error", detail)
        with log_output.open("a", encoding="utf-8") as target:
            target.write(f"[CI] {phase}: {detail}\n")

    try:
        offline(device, clock=clock, sleep=sleep)
        if device.call("GET", "status")["firmware"]["version"] != request["firmware"]["version"]:
            raise ValueError("Unexpected firmware version")
        remove_data(device, ("apps_data", request["app_id"]))
        touched = True
        step = "stage"
        staged = device.call("POST", "apps/stage", data=package.read_bytes(), timeout=120)
        if (
            staged["staged"]["id"] != request["app_id"]
            or staged["staged"]["version"] != request["version"]
            or type(staged["install_key"]) is not int
        ):
            raise ValueError("Staged application differs from the checked package")
        step = "install"
        device.call("POST", "apps/install", query={"install_key": staged["install_key"]}, timeout=120)
        step = "list"
        installed = [app for app in device.call("GET", "apps/list")["apps"] if app["id"] == request["app_id"]]
        if len(installed) != 1 or installed[0]["version"] != request["version"]:
            raise ObservationError("installed_version_mismatch")
        step = "observe"
        baseline = health(device.call("GET", "status"))
        observer = ApplicationLog(serial_log, request["app_id"], log_output)
        step = "launch"
        device.call("POST", "apps/launch", query={"app_id": request["app_id"]}, timeout=10)
        result["launch"] = "accepted"
        started = clock()
        step = "observe"
        while True:
            observer.poll()
            current = health(device.call("GET", "status"))
            if current[0][:3] != baseline[0][:3]:
                raise ObservationError("bar_identity_changed")
            if current[0] != baseline[0] or current[1] < baseline[1]:
                raise ObservationError("bar_rebooted")
            if device.call("GET", "wifi/status").get("state") != "disconnected":
                raise ObservationError("wifi_reconnected")
            elapsed = clock() - started
            if elapsed >= 60:
                completed = True
                break
            sleep(min(1, 60 - elapsed))
        result["status"] = "pass"
    except (DeviceError, ValueError, KeyError, OSError) as error:
        failed(step, error)
    finally:
        if started is not None:
            result["observed_seconds"] = min(60, int(clock() - started))
            # Keep failure screenshots too; capture each screen before stopping the app.
            for display, name in enumerate(("front.png", "back.png")):
                try:
                    save_screen(device.screen(display), display, output_dir / name)
                except (DeviceError, ValueError, OSError) as error:
                    failed("screenshot_" + name, error)
        if observer is not None:
            try:
                observer.poll()
            except (ValueError, OSError) as error:
                failed("observe", error)
            result.update(
                script_seen=observer.script_seen,
                runtime_errors=sorted(observer.errors),
                log_complete=completed and observer.valid,
            )
            if observer.errors:
                failed("observe", ObservationError("runtime_error"))
            if started is not None and not observer.script_seen:
                # A log that never grew is a dead logger, not an app that did not start.
                kind = "script_not_seen" if observer.read_bytes else "serial_log_silent"
                failed("observe", ObservationError(kind))
        try:
            if touched:
                try:
                    # Deletion also stops the application, including short-lived apps.
                    device.call("DELETE", "apps", query={"app_id": request["app_id"]}, timeout=15)
                except DeviceError as error:
                    if error.status != 404:
                        raise
                if any(app["id"] == request["app_id"] for app in device.call("GET", "apps/list")["apps"]):
                    raise ValueError("Application remains installed after deletion")
                remove_data(device, ("apps_data", request["app_id"]))
            result["cleanup"] = "pass"
        except (DeviceError, ValueError, KeyError, OSError) as error:
            result["cleanup"] = "fail"
            failed("cleanup", error)
        for name in sorted(ARTIFACTS):
            path = output_dir / name
            if path.exists():
                result["artifacts"][name] = digest(path)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = {"status": "fail", "cleanup": "not_run"}
    try:
        request = read_json(args.request)
        validate_request(request)
        result.update(request)
        if digest(args.package) != request["package_sha256"]:
            raise ValueError("Application package digest mismatch")
        device = Device(os.environ["BUSYBAR_IP"])
        target = device.call("GET", "status")["firmware"]["target"]
        if type(target) is not int or target not in {21, 22}:
            raise ValueError("Unsupported BAR firmware target")
        firmware, image = latest_release(f"f{target}")
        provision(device, firmware, image)
        request["firmware"] = firmware
        result = smoke(
            device, request, args.package, Path(os.environ["SESSION_LOG_DIR"]) / "runlog.log", args.output.parent
        )
    except Exception as error:
        result.update(status="fail", error=f"BAR check could not complete ({type(error).__name__})")
    write_json(args.output, result)
    print("BAR smoke check: " + result["status"])
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
