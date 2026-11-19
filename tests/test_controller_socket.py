"""Controller socket activation keeps the port held while Uvicorn restarts."""
import asyncio
import errno
import os
import socket
import unittest
from unittest.mock import patch

import uvicorn
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from boring_crocodile.controller_socket import take_listener
from boring_crocodile.site_policy import ServiceInputs
from boring_crocodile.site_templates import render_description
from test_setup_services import description, policy


class ActivationTest(unittest.IsolatedAsyncioTestCase):
    def activation(self):
        return patch.dict(os.environ, LISTEN_PID=str(os.getpid()),
                          LISTEN_FDS="1", LISTEN_FDNAMES="controller")

    async def test_port_is_held_before_during_and_after_controller_restart(self):
        # This retained listener stands in for PID 1's descriptor. Each actual
        # Uvicorn instance gets its own copy, just as with socket activation.
        with socket.socket() as retained:
            retained.bind(("127.0.0.1", 0))
            retained.listen()
            host, port = retained.getsockname()

            def still_held():
                with socket.socket() as rival:
                    with self.assertRaises(OSError) as caught:
                        rival.bind((host, port))
                    self.assertEqual(caught.exception.errno, errno.EADDRINUSE)

            async def answer(request):
                return PlainTextResponse("controller")

            async def start():
                with self.activation():
                    owned = take_listener(host, port, descriptor=retained.fileno())
                server = uvicorn.Server(uvicorn.Config(
                    Starlette(routes=[Route("/", answer)]), log_level="critical",
                    lifespan="off", timeout_graceful_shutdown=1))
                task = asyncio.create_task(server.serve(sockets=[owned]))
                try:
                    async with asyncio.timeout(5):
                        while not server.started:
                            if task.done():
                                await task
                                self.fail("server stopped before startup")
                            await asyncio.sleep(0.01)
                except BaseException:
                    server.should_exit = True
                    await task
                    owned.close()
                    raise
                return server, task, owned

            still_held()
            for _ in range(2):
                server, task, owned = await start()
                try:
                    still_held()
                    reader, writer = await asyncio.open_connection(host, port)
                    writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                    await writer.drain()
                    response = await asyncio.wait_for(reader.read(), 5)
                    self.assertIn(b"controller", response)
                    writer.close()
                    await writer.wait_closed()
                finally:
                    server.should_exit = True
                    await asyncio.wait_for(task, 5)
                    owned.close()
                # No controller is running here. Its port remains unavailable
                # to a competing listener throughout this restart window.
                for _ in range(10):
                    still_held()
                    await asyncio.sleep(0)
            still_held()

    def test_activation_metadata_and_actual_listener_must_match(self):
        with socket.socket() as retained:
            retained.bind(("127.0.0.1", 0))
            retained.listen()
            host, port = retained.getsockname()
            for changes in ({"LISTEN_PID": "0"}, {"LISTEN_FDS": "2"},
                            {"LISTEN_FDNAMES": "other"}):
                with self.activation(), patch.dict(os.environ, changes):
                    with self.assertRaisesRegex(RuntimeError, "socket activation"):
                        take_listener(host, port, descriptor=retained.fileno())
            with self.activation():
                with self.assertRaisesRegex(RuntimeError, "address differs"):
                    take_listener(host, port % 65535 + 1, descriptor=retained.fileno())
        with socket.socket() as not_listening, self.activation():
            with self.assertRaisesRegex(RuntimeError, "not a TCP listener"):
                take_listener("127.0.0.1", 8503, descriptor=not_listening.fileno())


class PortPolicyTest(unittest.TestCase):
    def test_controller_services_tunnel_and_custom_ports_are_denied(self):
        for ports in (None, [19999, 19999, 18520]):
            config = policy() | ({"protected_ports": ports} if ports is not None else {})
            output = render_description(description(config))
            slice_ = output["system-crocodilepond.slice"]
            self.assertIn("SocketBindDeny=tcp:8503\n", slice_)
            for port in {*range(18510, 18540), *(ports or [])}:
                self.assertIn(f"SocketBindDeny=tcp:{port}\n", slice_)
            self.assertIn("ListenStream=127.0.0.1:8503", output["boring-crocodile-mcp.socket"])
            self.assertIn("FileDescriptorName=controller", output["boring-crocodile-mcp.socket"])
            self.assertIn("Requires=boring-crocodile-worker-pond.service boring-crocodile-mcp.socket",
                          output["boring-crocodile-mcp.service"])

    def test_invalid_protected_ports_refuse(self):
        for ports in ("18520", [True], [0], [65536], [-1], list(range(1, 129))):
            with self.subTest(ports=ports):
                with self.assertRaisesRegex(ValueError, "protected_ports"):
                    ServiceInputs.parse(policy() | {"protected_ports": ports})
