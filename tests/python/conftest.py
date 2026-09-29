"""Shared fixtures: a throwaway Perforce server.

The server runs through an ``rsh:`` port (``p4d -i``), so there is no network
listener. Tests that need it are skipped unless ``CLIO_TEST_P4D`` points at a
``p4d`` binary.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from deda import clio

USER = "clio_tester"
CLIENT = "clio_test_ws"
PASSWORD = "ClioTest-1!"
DEPOT_ROOT = "//depot/proj"


@dataclass
class P4Server:
    port: str
    tickets: Path
    workspace_root: Path
    version_store: Path

    def connection(self) -> clio.Connection:
        return clio.Connection(
            port=self.port,
            user=USER,
            client=CLIENT,
            cwd=os.fspath(self.workspace_root),
            tickets=os.fspath(self.tickets),
        )

    def settings_text(self, **extra: str) -> str:
        values = {
            "depot": DEPOT_ROOT,
            "root": os.fspath(self.workspace_root),
            "store": os.fspath(self.version_store),
            "port": self.port,
            "user": USER,
            "client": CLIENT,
            "tickets": os.fspath(self.tickets),
            **extra,
        }
        return ";".join(f"{k}={v}" for k, v in values.items())

    def submit(self, files: dict[str, str], description: str) -> int:
        conn = self.connection()
        for relative, content in files.items():
            local = self.workspace_root / relative
            existed = local.exists()
            if existed:
                conn.run_or_throw("edit", [os.fspath(local)])
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_text(content)
            if not existed:
                conn.run_or_throw("add", [os.fspath(local)])
        result = conn.run_or_throw("submit", ["-d", description])
        return next(int(r["submittedChange"]) for r in result.records if "submittedChange" in r)

    def clear_workspace(self) -> None:
        self.connection().run_or_throw("sync", ["-q", f"{DEPOT_ROOT}/...#none"])


@pytest.fixture
def p4_server(tmp_path: Path) -> P4Server:
    p4d = os.environ.get("CLIO_TEST_P4D")
    if not p4d:
        pytest.skip("CLIO_TEST_P4D is not set")

    server_root = tmp_path / "server"
    server_root.mkdir()
    server = P4Server(
        port=f"rsh:{p4d} -r {server_root} -L log -J off -i",
        tickets=tmp_path / "tickets",
        workspace_root=tmp_path / "ws",
        version_store=tmp_path / "store",
    )
    server.workspace_root.mkdir()

    # Recent servers require every user to have a password, even on a new
    # server: set one and log in, then every connection uses the ticket.
    admin = clio.Connection(
        port=server.port,
        user=USER,
        tickets=os.fspath(server.tickets),
        prompt=lambda text, no_echo: PASSWORD,
    )
    admin.run_or_throw("passwd")
    admin.run_or_throw("login")

    spec = (
        f"Client: {CLIENT}\nOwner: {USER}\nRoot: {server.workspace_root}\n"
        "Options: allwrite noclobber nocompress unlocked nomodtime normdir\n"
        f"LineEnd: local\nView:\n\t{DEPOT_ROOT}/... //{CLIENT}/...\n"
    )
    server.connection().run_or_throw("client", ["-i"], spec)
    return server
