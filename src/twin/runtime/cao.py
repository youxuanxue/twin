from __future__ import annotations

import ipaddress
import json
import socket
from urllib import error, parse, request as urllib_request

from twin.runtime.protocols import WorkerTurnRequest, WorkerTurnResult


class _NoRedirect(urllib_request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class CaoRuntime:
    def __init__(
        self,
        endpoint: str,
        *,
        auth_token: str | None,
        provider: str,
        agent: str,
        opener: urllib_request.OpenerDirector | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.auth_token = auth_token
        self.provider = provider
        self.agent = agent
        self.opener = opener or urllib_request.build_opener(_NoRedirect)

    def run_turn(self, request: WorkerTurnRequest) -> WorkerTurnResult:
        blocked = self._guard_endpoint()
        if blocked is not None:
            return self._failure(blocked, request)
        if not self.auth_token:
            return self._failure("cao_auth_required", request)
        body = json.dumps({
            "provider": self.provider,
            "agent": self.agent,
            "prompt": request.prompt,
            "session_id": request.session_id,
            "cwd": str(request.cwd),
        }).encode("utf-8")
        outbound = urllib_request.Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.auth_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with self.opener.open(outbound, timeout=request.timeout_seconds) as response:
                response_body = response.read()
        except (TimeoutError, socket.timeout) as exc:
            return self._failure("timeout", request, {"error": str(exc)}, timed_out=True)
        except error.HTTPError as exc:
            if 300 <= exc.code < 400:
                return self._failure("cao_redirect_blocked", request)
            if exc.code in {401, 403}:
                return self._failure("cao_auth_failed", request)
            return self._failure("cao_http_error", request, {"status": exc.code})
        except OSError as exc:
            return self._failure("cao_request_failed", request, {"error": str(exc)})
        try:
            value = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._failure("malformed_output", request)
        if not isinstance(value, dict):
            return self._failure("malformed_output", request)
        output = value.get("output_text")
        returncode = value.get("returncode")
        session_id = value.get("session_id")
        events = value.get("events", [])
        if (
            not isinstance(output, str)
            or not isinstance(returncode, int)
            or not isinstance(session_id, str)
            or not isinstance(events, list)
            or not all(isinstance(event, dict) for event in events)
        ):
            return self._failure("malformed_output", request)
        return WorkerTurnResult(
            output_text=output,
            returncode=returncode,
            session_id=session_id,
            events=tuple(events),
        )

    def _guard_endpoint(self) -> str | None:
        parsed = parse.urlparse(self.endpoint)
        if parsed.scheme == "http" and not self._is_loopback(parsed.hostname):
            return "cao_plaintext_non_loopback"
        if parsed.scheme not in {"http", "https"}:
            return "cao_invalid_endpoint"
        return None

    @staticmethod
    def _is_loopback(hostname: str | None) -> bool:
        if hostname is None:
            return False
        if hostname == "localhost":
            return True
        try:
            return ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            try:
                return all(ipaddress.ip_address(value[4][0]).is_loopback for value in socket.getaddrinfo(hostname, None))
            except OSError:
                return False

    @staticmethod
    def _failure(
        kind: str,
        request: WorkerTurnRequest,
        metadata: dict[str, object] | None = None,
        *,
        timed_out: bool = False,
    ) -> WorkerTurnResult:
        event: dict[str, object] = {"event": "failure", "failure_kind": kind, "provider": request.provider}
        if metadata:
            event.update(metadata)
        return WorkerTurnResult(
            output_text=f"{kind}",
            returncode=1,
            session_id=request.session_id,
            events=(event,),
            timed_out=timed_out,
        )
