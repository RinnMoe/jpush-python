"""Synchronous receive-only JPush client with background socket loops."""

from __future__ import annotations

import hashlib
import json
import secrets
import socket
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from queue import Empty, Full, Queue
from typing import Any

from .codecs import (
    CMD_TAG_ALIAS,
    CMD_TAG_ALIAS_V2,
    IOS_VERSION_STRING,
    Codec,
    JHeadCodec,
)
from .errors import ConnectionClosedError, JPushError, ProtocolError
from .models import (
    Ack,
    Credentials,
    LoginParams,
    LoginResult,
    Platform,
    PlatformAndroid,
    PlatformIOS,
    Push,
    RegisterParams,
    RegisterResult,
)
from .packet import read_raw_frame
from .sis import (
    DEFAULT_SIS_HOSTS,
    FALLBACK_CONN,
    SIS_TIMEOUT,
    build_sis_request,
    query_sis,
)
from .store import Store

DEFAULT_SDK_VERSION = "2.1.0"
DEFAULT_APP_VERSION = "1.0.0"
DEFAULT_HEARTBEAT_INTERVAL = 15.0
DIAL_TIMEOUT = 15.0
IO_TIMEOUT = 20.0


Dialer = Callable[[str, float | None], Any]
Logger = Callable[..., Any]


@dataclass
class Config:
    """Configuration for :class:`Client`.

    ``dial`` receives an ``ip:port`` string and a timeout in seconds and must
    return a socket-like object implementing ``sendall``, ``recv`` and
    ``close``.  Leaving it unset uses :func:`socket.create_connection`.
    """

    app_key: str
    sdk_version: str = DEFAULT_SDK_VERSION
    app_version: str = DEFAULT_APP_VERSION
    platform: Platform = PlatformAndroid
    package_name: str = ""
    channel: str = ""
    device_model: str = ""
    client_info: str = ""
    client_version: int = 0
    reg_business: int = 0
    store: Store | None = None
    sis_hosts: Sequence[str] = field(default_factory=tuple)
    codec: Codec | None = None
    servers: Sequence[str] = field(default_factory=tuple)
    dial: Dialer | None = None
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL
    logger: Logger | None = None

    def __post_init__(self) -> None:
        if not self.sdk_version:
            self.sdk_version = DEFAULT_SDK_VERSION
        if not self.app_version:
            self.app_version = DEFAULT_APP_VERSION
        if self.heartbeat_interval <= 0:
            self.heartbeat_interval = DEFAULT_HEARTBEAT_INTERVAL
        if self.client_version == 0:
            self.client_version = version_int(self.sdk_version)
        self.platform = Platform(self.platform)


class Client:
    """A JPush receive-only client.

    Call :meth:`register` first, then :meth:`set_alias` and either
    :meth:`wait_for_push` or :meth:`run`.  Network operations are synchronous;
    one receiver thread and one heartbeat thread are started after login.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self.codec: Codec = config.codec or JHeadCodec()
        self.credentials = Credentials()
        self._conn: Any = None
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._rid = 0
        self._sid = 0
        self._pushes: Queue[Push] = Queue(maxsize=16)
        self._acks: Queue[Ack] = Queue(maxsize=16)
        self._done = threading.Event()
        self._close_error: BaseException | None = None
        self._loops_started = False
        self._threads: list[threading.Thread] = []

    @property
    def reg_id(self) -> str:
        return self.credentials.reg_id

    def log(self, message: str, *args: object) -> None:
        if self.config.logger is None:
            return
        try:
            self.config.logger(message, *args)
        except TypeError:
            # A Python logger conventionally accepts one already-formatted
            # string, while the Go API receives format + arguments.
            self.config.logger(message % args if args else message)

    def _next_rid(self) -> int:
        with self._state_lock:
            self._rid += 1
            return self._rid

    def register(self, timeout: float | None = None) -> Credentials:
        """Connect, register if necessary, log in, and start receive loops."""

        if not self.config.app_key:
            raise JPushError("jpush: AppKey is required")
        deadline = _deadline(timeout)

        if self.config.store is not None:
            try:
                saved = _store_load(self.config.store)
            except Exception as exc:  # noqa: BLE001 - custom stores may fail arbitrarily
                self.log("load credentials: %s", exc)
                saved = None
            if saved is not None and saved.valid():
                self.credentials = replace(saved)
                self.log(
                    "reusing stored credentials regId=%s juid=%d",
                    saved.reg_id,
                    saved.juid,
                )

        try:
            self._connect(deadline)
            if not self.credentials.valid():
                self._register(deadline)
                if self.config.store is not None:
                    try:
                        _store_save(self.config.store, self.credentials)
                    except Exception as exc:  # noqa: BLE001 - persistence is best effort
                        self.log("save credentials: %s", exc)
            self._login(deadline)
        except Exception as exc:
            self._close(exc)
            raise

        self._start_loops()
        return replace(self.credentials)

    def _connect(self, deadline: float | None) -> None:
        if self.config.servers:
            servers = list(self.config.servers) + [FALLBACK_CONN]
        else:
            servers = self.resolve_servers(deadline)
        last_error: BaseException | None = None
        for address in servers:
            try:
                connection = self._dial(address, _remaining(deadline, DIAL_TIMEOUT))
            except Exception as exc:  # noqa: BLE001 - dialers may wrap transports
                self.log("dial %s: %s", address, exc)
                last_error = exc
                continue
            self.log("connected to %s", address)
            self._conn = connection
            return
        if last_error is None:
            last_error = ConnectionClosedError("no push servers")
        raise JPushError(f"jpush: connect: {last_error}") from last_error

    def _dial(self, address: str, timeout: float | None) -> Any:
        if self.config.dial is not None:
            return self.config.dial(address, timeout)
        host, port = _split_address(address)
        return socket.create_connection((host, port), timeout=timeout)

    def resolve_servers(self, deadline: float | None = None) -> list[str]:
        hosts = list(self.config.sis_hosts) or list(DEFAULT_SIS_HOSTS)
        payload = build_sis_request(
            self.config.app_key,
            self.config.sdk_version,
            self.credentials.juid,
            "wifi",
        )
        connections: list[str] = []
        for host in hosts:
            try:
                response = query_sis(
                    host,
                    payload,
                    timeout=_remaining(deadline, SIS_TIMEOUT),
                )
            except Exception as exc:  # noqa: BLE001 - SIS hosts are best effort
                self.log("sis %s: %s", host, exc)
                continue
            connections.extend(response.ips)
            connections.extend(response.op_conns)
            if connections:
                break
        connections.append(FALLBACK_CONN)
        return connections

    def _round_trip(self, deadline: float | None, request: bytes) -> Any:
        if self._conn is None:
            raise ConnectionClosedError("jpush: not connected")
        timeout = _remaining(deadline, IO_TIMEOUT)
        _set_timeout(self._conn, timeout)
        try:
            self.log("-> %s", request.hex(" "))
            _send_all(self._conn, request)
            response_frame = read_raw_frame(self._conn)
            self.log("<- %s", response_frame.hex(" "))
            return self.codec.decode(response_frame)
        finally:
            _set_timeout(self._conn, None)

    def _register(self, deadline: float | None) -> None:
        self._ensure_identity()
        params = self.build_register_params()
        self.log(
            "register plaintext key=%r apkVersion=%r clientInfo=%r "
            "deviceToken=%r advertisingId=%r buildType=%d apsType=%d "
            "keyExt=%r regBusiness=%d accountID=%r",
            params.key,
            params.apk_version,
            params.client_info,
            params.device_token,
            params.advertising_id,
            params.build_type,
            params.aps_type,
            params.key_ext,
            params.reg_business,
            params.account_id,
        )
        response = self._round_trip(
            deadline,
            self.codec.encode_register(self._next_rid(), params),
        )
        if not isinstance(response, RegisterResult):
            raise ProtocolError(
                f"jpush: register: unexpected response {type(response).__name__}"
            )
        if response.code != 0:
            raise JPushError(
                f"jpush: register failed: code {response.code} {response.error}"
            )
        if not response.reg_id or response.juid == 0:
            raise ProtocolError(
                "jpush: register returned empty regId/juid (wire format mismatch?)"
            )
        old = self.credentials
        self.credentials = Credentials(
            juid=response.juid,
            password=response.password,
            reg_id=response.reg_id,
            device_id=response.device_id,
            udid=old.udid,
            android_id=old.android_id,
            device_token=old.device_token,
        )
        self.log("registered regId=%s juid=%d", response.reg_id, response.juid)

    def _login(self, deadline: float | None) -> None:
        app_key = self.config.app_key.strip().lower()
        params = LoginParams(
            password_md5=md5_hex(self.credentials.password),
            app_key=app_key,
            client_version=self.config.client_version,
            version_string=IOS_VERSION_STRING,
        )
        if self.config.platform == PlatformIOS:
            params.device_hash = md5_hex(
                self.config.package_name.strip() + app_key + self.credentials.udid
            )
        response = self._round_trip(
            deadline,
            self.codec.encode_login(self._next_rid(), self.credentials.juid, params),
        )
        if not isinstance(response, LoginResult):
            raise ProtocolError(
                f"jpush: login: unexpected response {type(response).__name__}"
            )
        if response.code != 0:
            raise JPushError(
                f"jpush: login failed: code {response.code} {response.error}"
            )
        self._sid = response.sid
        self.log("logged in sid=%d serverTime=%d", response.sid, response.server_time)

    def _start_loops(self) -> None:
        if self._loops_started:
            return
        self._loops_started = True
        receiver = threading.Thread(
            target=self._read_loop,
            name="jpush-receiver",
            daemon=True,
        )
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            name="jpush-heartbeat",
            daemon=True,
        )
        self._threads.extend((receiver, heartbeat))
        receiver.start()
        heartbeat.start()

    def _write(self, data: bytes) -> None:
        with self._write_lock:
            if self._conn is None:
                raise ConnectionClosedError("jpush: not connected")
            self.log("-> %s", data.hex(" "))
            _send_all(self._conn, data)

    def _read_loop(self) -> None:
        while not self._done.is_set():
            try:
                if self._conn is None:
                    raise ConnectionClosedError("jpush: not connected")
                raw = read_raw_frame(self._conn)
            except Exception as exc:  # noqa: BLE001 - receiver must close cleanly
                if not self._done.is_set():
                    self._close(exc)
                return
            self.log("<- %s", raw.hex(" "))
            try:
                response = self.codec.decode(raw)
            except Exception as exc:  # noqa: BLE001 - codecs may be user supplied
                self.log("decode: %s", exc)
                continue
            if isinstance(response, Push):
                self._ack_push(response)
                while not self._done.is_set():
                    try:
                        self._pushes.put(response, timeout=0.1)
                        break
                    except Full:
                        continue
            elif isinstance(response, Ack):
                try:
                    self._acks.put_nowait(response)
                except Full:
                    pass
            else:
                self.log("ignoring %s", type(response).__name__)

    def _heartbeat_loop(self) -> None:
        while not self._done.wait(self.config.heartbeat_interval):
            try:
                self._write(
                    self.codec.encode_heartbeat(
                        self._next_rid(), self.credentials.juid, self._sid
                    )
                )
            except Exception as exc:  # noqa: BLE001 - heartbeat failure closes client
                self._close(exc)
                return

    def _ack_push(self, push: Push) -> None:
        try:
            self._write(
                self.codec.encode_push_ack(
                    self._next_rid(),
                    self.credentials.juid,
                    self._sid,
                    0,
                    push.msg_type,
                    push.msg_id,
                )
            )
        except Exception as exc:  # noqa: BLE001 - ACK failure is non-fatal
            self.log("ack push %d: %s", push.msg_id, exc)

    def set_alias(self, alias: str, timeout: float | None = None) -> None:
        deadline = _deadline(timeout)
        action = self._alias_action(alias)
        try:
            self._write(
                self.codec.encode_set_alias(
                    self._next_rid(),
                    self.credentials.juid,
                    self._sid,
                    self.config.app_key,
                    action,
                )
            )
        except Exception as exc:
            raise JPushError(f"jpush: set alias: {exc}") from exc

        while True:
            ack = self._next_ack(deadline)
            if ack is None:
                raise self._closed_error("jpush: set alias: connection closed")
            if ack.request_command not in (CMD_TAG_ALIAS, CMD_TAG_ALIAS_V2):
                continue
            if ack.status > 0:
                raise JPushError(f"jpush: set alias rejected: status {ack.status}")
            self.log("alias %r set", alias)
            return

    def wait_for_push(
        self,
        match: Callable[[Push], bool] | None = None,
        timeout: float | None = None,
    ) -> Push:
        deadline = _deadline(timeout)
        while True:
            push = self._next_push(deadline)
            if push is None:
                raise self._closed_error("jpush: connection closed")
            if match is None or match(push):
                return push

    def run(
        self,
        on_push: Callable[[Push], Any],
        timeout: float | None = None,
    ) -> None:
        deadline = _deadline(timeout)
        while True:
            push = self._next_push(deadline)
            if push is None:
                raise self._closed_error("jpush: connection closed")
            result = on_push(push)
            if result is not None:
                if isinstance(result, BaseException):
                    raise result
                raise JPushError(str(result))

    def close(self) -> None:
        self._close(None)

    def _close(self, cause: BaseException | None) -> None:
        with self._close_lock:
            if self._done.is_set():
                return
            self._close_error = cause
            self._done.set()
            with self._write_lock:
                if self._conn is not None:
                    try:
                        self._conn.close()
                    except Exception as exc:  # noqa: BLE001 - custom socket close is best effort
                        self.log("close socket: %s", exc)

    def _closed_error(self, prefix: str) -> BaseException:
        if self._close_error is not None:
            return self._close_error
        return ConnectionClosedError(prefix)

    def _next_push(self, deadline: float | None) -> Push | None:
        return self._next_queue(self._pushes, deadline)

    def _next_ack(self, deadline: float | None) -> Ack | None:
        return self._next_queue(self._acks, deadline)

    def _next_queue(self, queue: Queue[Any], deadline: float | None) -> Any | None:
        while True:
            try:
                return queue.get_nowait()
            except Empty:
                pass
            if self._done.is_set():
                return None
            remaining = _remaining(deadline, 0.1)
            if remaining is not None and remaining <= 0:
                raise TimeoutError("jpush operation timed out")
            try:
                return queue.get(timeout=remaining)
            except Empty:
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("jpush operation timed out")

    def _ensure_identity(self) -> None:
        if not self.credentials.udid:
            if self.config.platform == PlatformIOS:
                self.credentials.udid = "iPhone" + secrets.token_hex(16).upper()
            else:
                self.credentials.udid = secrets.token_hex(16)
        if not self.credentials.android_id:
            self.credentials.android_id = secrets.token_hex(8)

    def build_register_params(self) -> RegisterParams:
        app_key = self.config.app_key.strip().lower()
        app_id = self.config.package_name.strip()
        params = RegisterParams(
            key=f"{self.credentials.udid}$$ $${app_id}$${app_key}",
            apk_version=self.config.app_version,
            client_info=self.config.client_info,
            reg_business=self.config.reg_business,
        )
        if self.config.platform == PlatformIOS:
            if not params.client_info:
                params.client_info = default_client_info_ios(self.config)
            params.key_ext = "1$$" + md5_hex(app_id + app_key + self.credentials.udid)
            params.device_token = (
                (self.credentials.device_token + "$$")
                if self.credentials.device_token
                else "$$"
            )
            params.advertising_id = " "
            params.build_type = 2
            params.aps_type = 0xFF
            return params
        if not params.client_info:
            params.client_info = default_client_info(self.config)
        params.key_ext = (
            f"0$${self.credentials.udid}$$ $${self.credentials.android_id}$$ $$"
        )
        return params

    def _alias_action(self, alias: str) -> str:
        platform = "i" if self.config.platform == PlatformIOS else "a"
        return json.dumps(
            {"platform": platform, "op": "set", "alias": alias},
            ensure_ascii=False,
            separators=(",", ":"),
        )

    # Go-source-compatible spellings.
    Register = register
    SetAlias = set_alias
    WaitForPush = wait_for_push
    Run = run
    Close = close

    def RegID(self) -> str:
        return self.reg_id


def _store_load(store: Store) -> Credentials | None:
    try:
        load = store.load
    except AttributeError:
        load = store.Load  # type: ignore[attr-defined]
    return load()


def _store_save(store: Store, credentials: Credentials) -> None:
    try:
        save = store.save
    except AttributeError:
        save = store.Save  # type: ignore[attr-defined]
    save(credentials)


def _send_all(connection: Any, data: bytes) -> None:
    if hasattr(connection, "sendall"):
        connection.sendall(data)
        return
    sent = 0
    while sent < len(data):
        count = connection.send(data[sent:])
        if count <= 0:
            raise ConnectionClosedError("socket closed while sending")
        sent += count


def _set_timeout(connection: Any, timeout: float | None) -> None:
    setter = getattr(connection, "settimeout", None)
    if setter is not None:
        setter(timeout)


def _split_address(address: str) -> tuple[str, int]:
    if address.startswith("["):
        host, _, port = address[1:].partition("]:")
    else:
        host, separator, port = address.rpartition(":")
        if not separator:
            raise ValueError(f"address has no port: {address!r}")
    if not host or not port:
        raise ValueError(f"invalid address: {address!r}")
    return host, int(port)


def _deadline(timeout: float | None) -> float | None:
    return None if timeout is None else time.monotonic() + max(0.0, timeout)


def _remaining(deadline: float | None, default: float | None) -> float | None:
    if deadline is None:
        return default
    return max(0.0, min(default, deadline - time.monotonic())) if default else max(
        0.0, deadline - time.monotonic()
    )


def version_int(value: str) -> int:
    result = 0
    parts = value.split(".")
    for index in range(3):
        try:
            number = int(parts[index].strip())
        except (IndexError, ValueError):
            number = 0
        result = result * 1000 + number
    return result


def md5_hex(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def default_client_info(config: Config) -> str:
    model = config.device_model or "Pixel 5"
    return "$$".join(
        (
            "33",
            model,
            "unknown",
            "1080*2340",
            "en",
            str((4 << 16) | (7 << 8) | 3),
            "0",
            "google",
            "android",
            "13&33",
        )
    )


def default_client_info_ios(config: Config) -> str:
    model = config.device_model or "iPhone17,1"
    channel = config.channel or "developer-default"
    return f"26.5$${model}$$ $$ $${channel}$$3.4.0|||2.4.0||"


def New(config: Config) -> Client:
    """Go-style factory alias; normal Python code can call ``Client(config)``."""

    return Client(config)
