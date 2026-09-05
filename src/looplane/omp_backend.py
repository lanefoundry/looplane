"""Delegation to the user-installed Oh My Pi agent (``omp --mode json``).

OMP is a fork of Pi and is reached as a sibling runtime: it owns its authentication, model
loop, permissions, and session. looplane never proxies its credentials. OMP exposes the same
JSON event stream as Pi, so the event normalizer reuses Pi's vocabulary; diverge once a
live OMP capture proves a different schema (see the M13 stage report).
"""

from __future__ import annotations

from looplane.pi_backend import PiBackend


class OmpRunner(PiBackend):
    backend_name = "omp"
    local_only = True
    experimental = True

    def _argv(self, executable: str, instruction: str) -> tuple[str, ...]:
        argv = [executable, "--mode", "json"]
        if self.model is not None:
            argv += ["--model", self.model]
        argv.append(instruction)
        return tuple(argv)


# Temporary compatibility name; implementation stays here until the runtime migration.
OmpBackend = OmpRunner
