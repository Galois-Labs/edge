"""Shared OPC-UA transport manager.

Owns a single asyncio event loop running on a daemon background thread.
Each ``GenericOpcuaDriver`` shares this loop and receives a dedicated
``asyncua.Client`` (one TCP session per (endpoint_url, user_token_id)).

The loop-in-thread pattern lets the rest of the daemon (which is sync) call
into asyncua's coroutines via ``asyncio.run_coroutine_threadsafe``.

Auto-reconnect is delegated to asyncua itself (``Client.session_timeout`` +
``Client.secure_channel_timeout`` keep the channel alive; on transport drop
the driver's reconnect logic re-creates the client).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Awaitable, Callable, Optional, TypeVar

try:
    from asyncua import Client, ua
    OPCUA_AVAILABLE = True
except ImportError:
    Client = None  # type: ignore[assignment,misc]
    ua = None  # type: ignore[assignment]
    OPCUA_AVAILABLE = False

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Default timeout for a single coroutine scheduled on the background loop.
# OPC-UA reads/writes are normally sub-second; 30 s gives lots of headroom for
# slow servers, networks, and reconnect attempts. A negative value disables
# the timeout (callers that want indefinite waits opt-in explicitly).
DEFAULT_LOOP_CALL_TIMEOUT = 30.0


# ---------------------------------------------------------------------------
# Security policy mapping
# ---------------------------------------------------------------------------

def _security_policy_string(
    security_policy: str,
    security_mode: str,
    cert_path: str = "",
    key_path: str = "",
) -> Optional[str]:
    """Build asyncua's set_security() string.

    Returns ``None`` if both policy and mode are ``"None"`` (i.e., disabled).

    Format: ``"{Policy},{Mode},{cert},{key}"``.
    """
    if (security_policy in (None, "", "None")
            and security_mode in (None, "", "None")):
        return None
    pol = security_policy or "None"
    mode = security_mode or "None"
    return f"{pol},{mode},{cert_path},{key_path}"


# ---------------------------------------------------------------------------
# OPCUABusManager
# ---------------------------------------------------------------------------


class OPCUABusManager:
    """Manages a shared asyncio event loop and ``asyncua.Client`` instances.

    Background loop is lazy: it starts when the first client is requested and
    runs until ``shutdown()`` is called (typically at daemon teardown).

    Clients are cached by ``(endpoint_url, user_token_id)`` so multiple
    drivers pointed at the same server with the same identity share one TCP
    session. Different identities (e.g., different user accounts) get
    independent sessions so credentials don't bleed across drivers.
    """

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._loop_ready = threading.Event()
        self._mgr_lock = threading.Lock()
        self._clients: dict[tuple[str, str], dict[str, Any]] = {}

    # -- Loop lifecycle --

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        """Lazily start the background loop and return it."""
        with self._mgr_lock:
            if self._loop is not None and self._loop.is_running():
                return self._loop
            self._loop_ready.clear()
            self._loop_thread = threading.Thread(
                target=self._run_loop,
                name="opcua-loop",
                daemon=True,
            )
            self._loop_thread.start()
        # Wait outside the lock so the thread can set the loop before signal.
        if not self._loop_ready.wait(timeout=10.0):
            raise RuntimeError("OPCUA event loop failed to start within 10s")
        assert self._loop is not None
        return self._loop

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._loop_ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                # Cancel pending tasks before closing.
                pending = asyncio.all_tasks(loop)
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            except Exception:
                pass
            loop.close()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        """Return the running background loop, starting it if necessary."""
        return self._ensure_loop()

    def loop_call(
        self,
        coro: Awaitable[T],
        timeout: float = DEFAULT_LOOP_CALL_TIMEOUT,
    ) -> T:
        """Schedule a coroutine on the background loop and wait for the result.

        ``timeout`` is in seconds; pass a negative number to wait indefinitely.
        Raises ``concurrent.futures.TimeoutError`` on timeout. Re-raises any
        exception thrown inside the coroutine on the calling thread.
        """
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)  # type: ignore[arg-type]
        if timeout is not None and timeout > 0:
            return future.result(timeout=timeout)
        return future.result()

    # -- Client lifecycle --

    @staticmethod
    def _client_key(endpoint_url: str, user_token_id: str = "") -> tuple[str, str]:
        return (endpoint_url, user_token_id)

    def get_client(
        self,
        endpoint_url: str,
        security_policy: str = "None",
        security_mode: str = "None",
        user_token: str = "anonymous",
        username: str = "",
        password: str = "",
        client_certificate_path: str = "",
        client_private_key_path: str = "",
        session_timeout_ms: int = 60000,
        secure_channel_timeout_ms: int = 60000,
        application_uri: str = "urn:galois:edge:opcua-client",
        timeout: float = DEFAULT_LOOP_CALL_TIMEOUT,
    ) -> Any:
        """Get (creating if necessary) an ``asyncua.Client`` for the endpoint.

        The client is connected and ready to use. Caller is responsible for
        ``release_client()`` when finished.
        """
        if not OPCUA_AVAILABLE:
            raise RuntimeError("asyncua is not installed")

        # Identity discriminator: anonymous → "anon"; username → "user:<name>";
        # certificate → "cert:<path>". This keeps separate clients per identity
        # so credentials don't bleed across drivers.
        if user_token == "username":
            token_id = f"user:{username}"
        elif user_token == "certificate":
            token_id = f"cert:{client_certificate_path}"
        else:
            token_id = "anon"

        key = self._client_key(endpoint_url, token_id)

        with self._mgr_lock:
            entry = self._clients.get(key)
            if entry is not None:
                entry["ref_count"] += 1
                return entry["client"]

        # Create + connect outside the manager lock so concurrent connects to
        # different endpoints don't serialize on each other.
        client = self._create_and_connect(
            endpoint_url=endpoint_url,
            security_policy=security_policy,
            security_mode=security_mode,
            user_token=user_token,
            username=username,
            password=password,
            client_certificate_path=client_certificate_path,
            client_private_key_path=client_private_key_path,
            session_timeout_ms=session_timeout_ms,
            secure_channel_timeout_ms=secure_channel_timeout_ms,
            application_uri=application_uri,
            timeout=timeout,
        )

        with self._mgr_lock:
            # Race: another caller may have created the client first.
            existing = self._clients.get(key)
            if existing is not None:
                existing["ref_count"] += 1
                # Tear down the duplicate we just made.
                try:
                    self.loop_call(client.disconnect(), timeout=timeout)
                except Exception:
                    pass
                return existing["client"]
            self._clients[key] = {"client": client, "ref_count": 1}
            return client

    def _create_and_connect(
        self,
        endpoint_url: str,
        security_policy: str,
        security_mode: str,
        user_token: str,
        username: str,
        password: str,
        client_certificate_path: str,
        client_private_key_path: str,
        session_timeout_ms: int,
        secure_channel_timeout_ms: int,
        application_uri: str,
        timeout: float,
    ) -> Any:
        loop = self._ensure_loop()

        async def _build() -> Any:
            client = Client(url=endpoint_url, timeout=timeout if timeout > 0 else 30)
            client.session_timeout = session_timeout_ms
            client.secure_channel_timeout = secure_channel_timeout_ms
            client.application_uri = application_uri

            sec_string = _security_policy_string(
                security_policy, security_mode,
                client_certificate_path, client_private_key_path,
            )
            if sec_string:
                await client.set_security_string(sec_string)

            if user_token == "username":
                client.set_user(username)
                client.set_password(password)
            # certificate auth: asyncua picks it up from the security string

            await client.connect()
            return client

        future = asyncio.run_coroutine_threadsafe(_build(), loop)
        return future.result(timeout=timeout if timeout > 0 else 30)

    def release_client(
        self,
        endpoint_url: str,
        user_token: str = "anonymous",
        username: str = "",
        client_certificate_path: str = "",
        timeout: float = DEFAULT_LOOP_CALL_TIMEOUT,
    ) -> None:
        """Release one reference; disconnect when ref_count hits 0."""
        if user_token == "username":
            token_id = f"user:{username}"
        elif user_token == "certificate":
            token_id = f"cert:{client_certificate_path}"
        else:
            token_id = "anon"
        key = self._client_key(endpoint_url, token_id)

        with self._mgr_lock:
            entry = self._clients.get(key)
            if entry is None:
                return
            entry["ref_count"] -= 1
            if entry["ref_count"] > 0:
                return
            client = entry["client"]
            del self._clients[key]

        try:
            self.loop_call(client.disconnect(), timeout=timeout)
        except Exception as exc:  # pragma: no cover — disconnect best-effort
            logger.warning("OPC-UA disconnect error for %s: %s", endpoint_url, exc)

    def shutdown(self, timeout: float = 5.0) -> None:
        """Disconnect all clients and stop the background loop.

        Idempotent. Safe to call when nothing was ever started.
        """
        with self._mgr_lock:
            entries = list(self._clients.values())
            self._clients.clear()
            loop = self._loop

        for entry in entries:
            try:
                if loop is not None and loop.is_running():
                    fut = asyncio.run_coroutine_threadsafe(
                        entry["client"].disconnect(), loop
                    )
                    fut.result(timeout=timeout)
            except Exception:
                pass

        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=timeout)
        self._loop = None
        self._loop_thread = None
        self._loop_ready.clear()

    # -- Diagnostic helpers --

    def active_clients(self) -> list[tuple[str, str]]:
        """List ``(endpoint_url, user_token_id)`` keys currently cached."""
        with self._mgr_lock:
            return list(self._clients.keys())
