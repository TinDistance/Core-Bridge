# -*- coding: utf-8 -*-
"""K230 蜂鸣器：正常运行静音，初始化 / 紧急 / 恢复三种节奏。"""
import ast
import importlib
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

MPY_MODULES = {"gc", "json", "os", "socket", "struct", "time", "uctypes",
               "network", "media", "machine", "sys"}


def _install_mpy_stubs():
    if not hasattr(sys, "print_exception"):
        sys.print_exception = lambda e, f=None: None  # noqa: F401
    for name in ("network", "uctypes", "machine"):
        sys.modules.setdefault(name, types.ModuleType(name))
    media = types.ModuleType("media")
    for sub in ("sensor", "media", "vencoder"):
        module = types.ModuleType("media." + sub)
        sys.modules["media." + sub] = module
        setattr(media, sub, module)
    sys.modules["media"] = media
    sys.modules["media.sensor"].Sensor = lambda **kw: None
    sys.modules["media.sensor"].CAM_CHN_ID_0 = 0
    sys.modules["media.vencoder"].Encoder = lambda **kw: None


def _load():
    _install_mpy_stubs()
    import k230.rtp_push as rp
    importlib.reload(rp)
    return rp


def _source():
    return (REPO / "k230" / "rtp_push.py").read_text(encoding="utf-8")


class FakePwm:
    def __init__(self):
        self.freqs = []
        self.duties = []
        self.deinited = False

    def freq(self, hz):
        self.freqs.append(hz)

    def duty_u16(self, duty):
        self.duties.append(duty)

    def deinit(self):
        self.deinited = True


def _player():
    rp = _load()
    buzzer = rp.Buzzer()
    pwm = FakePwm()
    buzzer._pwm = pwm
    buzzer._ready = True
    return rp, buzzer, pwm


def _pattern_ms(rp, event):
    return sum(step[2] for step in rp.BUZZ_PATTERNS[event][0])


def test_buzzer_pin_follows_board_id():
    rp = _load()
    assert rp.buzzer_pin_for("k230_canmv_lckfb") == 43
    assert rp.buzzer_pin_for("k230d") == 61
    assert rp.buzzer_pin_for("") == 61


def test_init_beep_is_two_tones_then_silence():
    rp, buzzer, pwm = _player()
    assert buzzer.play(rp.EVENT_INIT) is True
    assert pwm.freqs[0] == 4000
    assert pwm.duties[0] == rp.BUZZER_DUTY_U16
    start = buzzer._step_ms
    buzzer.tick(start + _pattern_ms(rp, rp.EVENT_INIT))
    assert buzzer._pattern == ()
    assert buzzer._event is None
    assert pwm.duties[-1] == 0
    frozen = len(pwm.duties)
    buzzer.tick(start + 5000)
    assert len(pwm.duties) == frozen


def test_idle_tick_does_not_drive_the_buzzer():
    _rp, buzzer, pwm = _player()
    buzzer.tick(0)
    buzzer.tick(10000)
    assert pwm.duties == []
    assert pwm.freqs == []


def test_camera_offline_is_a_long_tone_with_a_gap():
    rp, buzzer, pwm = _player()
    steps, repeat = rp.BUZZ_PATTERNS[rp.EVENT_CAMERA]
    assert repeat is True
    assert steps[0][2] >= 800
    assert steps[1][2] >= 500 and steps[1][0] == 0

    buzzer.set_condition("camera", True)
    assert buzzer._event == rp.EVENT_CAMERA
    assert buzzer._repeat is True
    start = buzzer._step_ms
    buzzer.tick(start + steps[0][2])
    assert pwm.duties[-1] == 0
    buzzer.tick(start + _pattern_ms(rp, rp.EVENT_CAMERA))
    assert buzzer._event == rp.EVENT_CAMERA
    assert buzzer._repeat is True
    assert pwm.duties[-1] == rp.BUZZER_DUTY_U16

    held = buzzer._step_ms
    buzzer.set_condition("camera", True)
    buzzer.set_condition("estop", True)
    assert buzzer._event == rp.EVENT_CAMERA
    assert buzzer._step_ms == held

    buzzer.set_condition("camera", False)
    assert buzzer._event == rp.EVENT_EMERGENCY
    buzzer.set_condition("estop", False)
    assert buzzer._event == rp.EVENT_RECOVERY
    assert buzzer._repeat is False
    recovery_at = buzzer._step_ms
    buzzer.tick(recovery_at + _pattern_ms(rp, rp.EVENT_RECOVERY))
    assert buzzer._pattern == ()
    assert buzzer._event is None
    assert pwm.duties[-1] == 0


def test_stalled_oneshot_goes_quiet_and_alarm_stays_latched():
    rp, buzzer, pwm = _player()
    buzzer.play(rp.EVENT_INIT)
    buzzer.tick(buzzer._step_ms + 5000)
    assert buzzer._pattern == ()
    assert pwm.duties[-1] == 0

    buzzer.set_condition("wifi", True)
    assert buzzer._event == rp.EVENT_EMERGENCY
    buzzer.tick(buzzer._step_ms + 5000)
    assert buzzer._repeat is True
    assert buzzer._event == rp.EVENT_EMERGENCY
    assert pwm.duties[-1] == rp.BUZZER_DUTY_U16


def test_estop_bit_alarms_and_clear_frame_recovers():
    rp, buzzer, _pwm = _player()
    rp.buzzer = buzzer

    def frame(estop, fire=0):
        data = bytearray(15)
        data[0] = 0xAA
        data[1] = 0x55
        data[9] = fire
        data[10] = estop
        data[13] = 0x0D
        data[14] = 0x0A
        crc = rp.crc16_ccitt_false(bytes(data[:11]))
        data[11] = (crc >> 8) & 0xFF
        data[12] = crc & 0xFF
        return bytes(data)

    class Port:
        def write(self, data):
            return len(data)

    class Sock:
        def __init__(self, payload):
            self.payload = payload
            self.timeout = None

        def settimeout(self, timeout):
            self.timeout = timeout

        def recv(self, _size):
            if self.payload is None:
                raise OSError(11)
            payload = self.payload
            self.payload = None
            return payload

    link = rp.CommandLink(Port())
    link.pump(Sock(frame(0, fire=1)))
    assert buzzer._event is None

    link.pump(Sock(frame(1)))
    assert buzzer._event == rp.EVENT_EMERGENCY
    assert buzzer._conditions["estop"] is True

    link.pump(Sock(frame(0)))
    assert buzzer._event == rp.EVENT_RECOVERY
    assert buzzer._conditions["estop"] is False


def _fast_finish(buzzer):
    guard = 0
    while buzzer._pattern and not buzzer._repeat and guard < 8:
        guard += 1
        buzzer.tick(buzzer._step_ms + buzzer._pattern[buzzer._index][2])


def _spy_play(buzzer):
    played = []
    orig = buzzer.play

    def wrapped(event, now_ms=None):
        played.append(event)
        return orig(event, now_ms)

    buzzer.play = wrapped
    buzzer.finish = lambda: _fast_finish(buzzer)
    return played


def test_wifi_connect_beeps_on_first_link_and_on_reconnect(monkeypatch):
    rp, buzzer, _pwm = _player()
    rp.buzzer = buzzer
    played = _spy_play(buzzer)
    rp.wifi_link_up = False
    monkeypatch.setattr(rp.os, "exitpoint", lambda: None, raising=False)
    attempts = {"n": 0}

    def fake_connect(_wlan):
        attempts["n"] += 1
        return attempts["n"] >= 2

    monkeypatch.setattr(rp, "connect_wifi", fake_connect)
    monkeypatch.setattr(rp, "sleep_ms", lambda _ms: None)

    class Wlan:
        def isconnected(self):
            return False

    rp.wait_for_wifi(Wlan())
    assert played == [rp.EVENT_WIFI]
    assert buzzer._event is None
    assert rp.wifi_link_up is True

    attempts["n"] = 0
    rp.wait_for_wifi(Wlan())
    assert played == [rp.EVENT_WIFI, rp.EVENT_EMERGENCY, rp.EVENT_WIFI]
    assert buzzer._conditions["wifi"] is False
    assert buzzer._event is None
    assert rp.EVENT_RECOVERY not in played


def test_wifi_connect_beep_then_camera_long_tone_resumes(monkeypatch):
    rp, buzzer, _pwm = _player()
    rp.buzzer = buzzer
    played = _spy_play(buzzer)
    rp.wifi_link_up = True
    buzzer.set_condition("camera", True)
    monkeypatch.setattr(rp.os, "exitpoint", lambda: None, raising=False)
    monkeypatch.setattr(rp, "connect_wifi", lambda _wlan: True)
    monkeypatch.setattr(rp, "sleep_ms", lambda _ms: None)

    class Wlan:
        def isconnected(self):
            return False

    rp.wait_for_wifi(Wlan())
    assert buzzer._conditions["camera"] is True
    assert buzzer._conditions["wifi"] is False
    assert played[-2:] == [rp.EVENT_WIFI, rp.EVENT_CAMERA]
    assert buzzer._event == rp.EVENT_CAMERA
    assert buzzer._repeat is True


def test_rtp_push_stays_in_the_micropython_subset():
    tree = ast.parse(_source())
    modules = set()
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
        elif isinstance(node, ast.JoinedStr):
            bad.append(("f-string", node.lineno))
        elif isinstance(node, ast.NamedExpr):
            bad.append(("walrus", node.lineno))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.returns is not None or any(
                arg.annotation for arg in node.args.args
            ):
                bad.append(("annotation", node.lineno))
        elif isinstance(node, ast.AnnAssign):
            bad.append(("AnnAssign", node.lineno))
    assert modules <= MPY_MODULES, sorted(modules - MPY_MODULES)
    assert not bad, bad
