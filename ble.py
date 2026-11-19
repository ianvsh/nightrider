"""Bluetooth LE access: a GATT server on BlueZ's D-Bus API.

Characteristics (UUIDs and message format are in PROTOCOL.md):
  info   - read: JSON status (same as /api/info)
  live   - notify: each new sample as JSON, at most LIVE_HZ per second
  query  - write: a JSON command, e.g. {"cmd":"sessions"}
  result - notify: the JSON reply to the last command

Notifications carry messages split into chunks: byte 0 is a header (bit 7 =
last chunk, bits 0-6 = chunk number mod 128), the rest is UTF-8 JSON.
No pairing is required.
"""
import json
import socket
import threading
import time

import dbus
import dbus.exceptions
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

BLUEZ = "org.bluez"
APP_PATH = "/org/datalogger"
ADV_PATH = "/org/datalogger/adv0"
GATT_MANAGER = "org.bluez.GattManager1"
ADV_MANAGER = "org.bluez.LEAdvertisingManager1"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"
PROP_IFACE = "org.freedesktop.DBus.Properties"
SVC_IFACE = "org.bluez.GattService1"
CHR_IFACE = "org.bluez.GattCharacteristic1"

SERVICE_UUID = "a5f00001-3c1e-4b8a-9d2f-6c1e0d4a7b10"
INFO_UUID = "a5f00002-3c1e-4b8a-9d2f-6c1e0d4a7b10"
LIVE_UUID = "a5f00003-3c1e-4b8a-9d2f-6c1e0d4a7b10"
QUERY_UUID = "a5f00004-3c1e-4b8a-9d2f-6c1e0d4a7b10"
RESULT_UUID = "a5f00005-3c1e-4b8a-9d2f-6c1e0d4a7b10"

CHUNK = 182          # notification size: fits iPhone's 185-byte MTU (Android: request >=185)
LIVE_HZ = 2
MAX_BLE_SAMPLES = 200
MAX_READ = 512       # ATT limit for a characteristic value


def chunks(msg, size=CHUNK):
    """Split a message (str) into framed notification payloads."""
    data = msg.encode()
    body = size - 1
    parts = [data[i:i + body] for i in range(0, len(data), body)] or [b""]
    out = []
    for n, part in enumerate(parts):
        header = (n % 128) | (0x80 if n == len(parts) - 1 else 0)
        out.append(bytes([header]) + part)
    return out


def unchunk(frames):
    """Reassemble framed payloads into the message (used by tests / reference for the app)."""
    return b"".join(f[1:] for f in frames).decode()


def handle_query(logger, raw):
    """Run one JSON command from the phone; returns the reply as a dict."""
    store = logger.store
    try:
        q = json.loads(raw)
        cmd = q.get("cmd")
        if cmd == "info":
            result = logger.info()
        elif cmd == "latest":
            result = store.latest
        elif cmd == "sessions":
            result = store.sessions(min(int(q.get("limit", 20)), 100), q.get("before"))
        elif cmd == "samples":
            result = store.samples(q.get("session"), int(q.get("since_id", 0)),
                                   min(int(q.get("limit", 50)), MAX_BLE_SAMPLES))
        else:
            return {"cmd": cmd, "ok": False, "error": "unknown cmd"}
        return {"cmd": cmd, "ok": True, "result": result}
    except (ValueError, TypeError, AttributeError) as e:
        return {"ok": False, "error": f"bad query: {e}"}


def _dumps(obj):
    return json.dumps(obj, separators=(",", ":"))


def _bytes(b):
    return dbus.Array([dbus.Byte(x) for x in b], signature="y")


class Characteristic(dbus.service.Object):
    def __init__(self, bus, index, uuid, flags, service_path):
        self.path = f"{service_path}/char{index}"
        self.uuid, self.flags, self.service_path = uuid, flags, service_path
        self.notifying = False
        super().__init__(bus, self.path)

    def props(self):
        return {CHR_IFACE: {
            "Service": dbus.ObjectPath(self.service_path),
            "UUID": self.uuid,
            "Flags": dbus.Array(self.flags, signature="s"),
        }}

    def notify(self, payload):
        if self.notifying:
            self.PropertiesChanged(CHR_IFACE, {"Value": _bytes(payload)}, [])

    def send(self, msg):
        for c in chunks(msg):
            self.notify(c)

    @dbus.service.method(PROP_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        return self.props()[CHR_IFACE]

    @dbus.service.signal(PROP_IFACE, signature="sa{sv}as")
    def PropertiesChanged(self, interface, changed, invalidated):
        pass

    @dbus.service.method(CHR_IFACE, in_signature="a{sv}", out_signature="ay")
    def ReadValue(self, options):
        value = self.read()[:MAX_READ]
        return _bytes(value[int(options.get("offset", 0)):])

    @dbus.service.method(CHR_IFACE, in_signature="aya{sv}")
    def WriteValue(self, value, options):
        self.write(bytes(value))

    @dbus.service.method(CHR_IFACE)
    def StartNotify(self):
        self.notifying = True

    @dbus.service.method(CHR_IFACE)
    def StopNotify(self):
        self.notifying = False

    def read(self):
        raise dbus.exceptions.DBusException("org.bluez.Error.NotSupported")

    def write(self, value):
        raise dbus.exceptions.DBusException("org.bluez.Error.NotSupported")


class Application(dbus.service.Object):
    """The GATT service and its characteristics, exported for BlueZ."""

    def __init__(self, bus, logger):
        super().__init__(bus, APP_PATH)
        self.svc_path = APP_PATH + "/service0"
        self.logger = logger
        self.last_live = 0

        app = self

        class Info(Characteristic):
            def read(self):
                return _dumps(app.logger.info()).encode()

        class Query(Characteristic):
            def write(self, value):
                reply = handle_query(app.logger, value.decode("utf-8", "replace"))
                # reply after WriteValue returns, so the phone sees the write succeed first
                GLib.idle_add(lambda: app.result.send(_dumps(reply)) and False)

        self.chars = [
            Info(bus, 0, INFO_UUID, ["read"], self.svc_path),
            Characteristic(bus, 1, LIVE_UUID, ["notify"], self.svc_path),
            Query(bus, 2, QUERY_UUID, ["write", "write-without-response"], self.svc_path),
            Characteristic(bus, 3, RESULT_UUID, ["notify"], self.svc_path),
        ]
        self.live, self.result = self.chars[1], self.chars[3]

    def on_sample(self, sample):
        """Called from the logger thread for each new sample."""
        now = time.time()
        if not self.live.notifying or now - self.last_live < 1 / LIVE_HZ:
            return
        self.last_live = now
        msg = _dumps(sample)
        GLib.idle_add(lambda: self.live.send(msg) and False)

    @dbus.service.method(OM_IFACE, out_signature="a{oa{sa{sv}}}")
    def GetManagedObjects(self):
        objs = {dbus.ObjectPath(self.svc_path): {SVC_IFACE: {
            "UUID": SERVICE_UUID, "Primary": True}}}
        for c in self.chars:
            objs[dbus.ObjectPath(c.path)] = c.props()
        return objs


class Advertisement(dbus.service.Object):
    def __init__(self, bus):
        super().__init__(bus, ADV_PATH)

    @dbus.service.method(PROP_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        return {
            "Type": "peripheral",
            "ServiceUUIDs": dbus.Array([SERVICE_UUID], signature="s"),
            "LocalName": socket.gethostname(),   # BlueZ puts it in the scan response
            # iPhones ignore adverts without the discoverable flag
            "Discoverable": dbus.Boolean(True),
            "MinInterval": dbus.UInt32(100),      # ms; BlueZ's default is 1.28 s
            "MaxInterval": dbus.UInt32(150),
        }

    @dbus.service.method("org.bluez.LEAdvertisement1")
    def Release(self):
        pass


class BLE:
    def __init__(self, logger, log):
        self.logger, self.log = logger, log
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self.bus = dbus.SystemBus()
        self.app = Application(self.bus, logger)
        self.adv = Advertisement(self.bus)
        self.registered = False
        logger.listeners.append(self.app.on_sample)
        # (re)register whenever bluetoothd starts or restarts
        self.bus.watch_name_owner(BLUEZ, self._bluez_changed)

    def _adapter(self):
        om = dbus.Interface(self.bus.get_object(BLUEZ, "/"), OM_IFACE)
        for path, ifaces in om.GetManagedObjects().items():
            if GATT_MANAGER in ifaces and ADV_MANAGER in ifaces:
                return path
        return None

    def _bluez_changed(self, owner):
        self.registered = False
        if owner:
            GLib.timeout_add_seconds(3, self._register)

    def _failed(self, e):
        self.log(f"bluetooth: registration failed, retrying: {e}")
        if self.registered:
            self.registered = False
            GLib.timeout_add_seconds(10, self._register)

    def _register(self):
        """Returns True to be retried by GLib until an adapter is ready."""
        if self.registered:
            return False
        try:
            path = self._adapter()
            if not path:
                return True
            obj = self.bus.get_object(BLUEZ, path)
            dbus.Interface(obj, PROP_IFACE).Set("org.bluez.Adapter1", "Powered", True)
            dbus.Interface(obj, GATT_MANAGER).RegisterApplication(
                APP_PATH, {}, reply_handler=lambda: self.log(f"bluetooth: gatt ready on {path}"),
                error_handler=self._failed)
            dbus.Interface(obj, ADV_MANAGER).RegisterAdvertisement(
                ADV_PATH, {}, reply_handler=lambda: self.log("bluetooth: advertising"),
                error_handler=self._failed)
            self.registered = True
            return False
        except dbus.exceptions.DBusException as e:
            self.log(f"bluetooth: waiting for adapter ({e.get_dbus_name()})")
            return True


def start(logger, log=print):
    ble = BLE(logger, log)
    loop = GLib.MainLoop()
    threading.Thread(target=loop.run, daemon=True).start()
    return ble
