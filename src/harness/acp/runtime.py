"""Common identity and observability behavior for ACP attempt runtimes."""

from __future__ import annotations

import contextlib
from typing import Optional

from deepresearch_cli.progress import ProgressReporter

from ..protocol import AgentInvocation, HarnessError
from .launch import AcpLaunchSpec


class AcpAttemptRuntime:
    """Base for one disposable ACP agent process serving one attempt.

    Transport-heavy behavior remains in the concrete runtime while this base
    centralizes backend identity, invocation ownership, and progress calls.
    """

    backend_name = "ACP"

    def __init__(
        self,
        *,
        launch_spec: AcpLaunchSpec,
        progress_reporter: Optional[ProgressReporter] = None,
        expected_invocation_id: Optional[str] = None,
    ) -> None:
        self.launch_spec = launch_spec
        self._acp_progress_reporter = progress_reporter
        self._acp_expected_invocation_id = expected_invocation_id

    def _claim_acp_invocation(self, invocation: AgentInvocation) -> None:
        expected = self._acp_expected_invocation_id
        if expected is not None and invocation.invocation_id != expected:
            raise HarnessError(
                f"{self.backend_name} attempt runtime received an unexpected "
                "invocation id"
            )

    def _acp_invocation_started(self, invocation: AgentInvocation) -> None:
        if self._acp_progress_reporter is not None:
            with contextlib.suppress(Exception):
                self._acp_progress_reporter.invocation_started(
                    invocation, backend=self.backend_name
                )

    def _acp_invocation_finished(
        self, invocation: AgentInvocation, status: str
    ) -> None:
        if self._acp_progress_reporter is not None:
            with contextlib.suppress(Exception):
                self._acp_progress_reporter.invocation_finished(
                    invocation, status, backend=self.backend_name
                )
