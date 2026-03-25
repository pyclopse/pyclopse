"""Execution approval system for pyclawops."""

import re
from pyclawops.reflect import reflect_system
import os
from dataclasses import dataclass
from typing import List, Optional, Pattern
from enum import Enum

from pyclawops.config.schema import SecurityMode, ExecApprovalsConfig


@dataclass
class ApprovalRequest:
    """A request for command execution approval.

    Carries all the contextual information needed by
    :class:`ExecApprovalSystem` to decide whether a command should be
    permitted.

    Attributes:
        command (str): The binary or shell builtin being executed (without
            arguments).
        args (List[str]): Argument list for the command.
        cwd (str): Working directory in which the command will run.
        agent_id (str): Identifier of the agent requesting execution.
        session_id (str): Identifier of the current conversation session.
        user_id (Optional[str]): External user identifier (Telegram/Slack
            user ID), or None when not available.
    """

    command: str
    args: List[str]
    cwd: str
    agent_id: str
    session_id: str
    user_id: Optional[str] = None


class ApprovalDecision(str, Enum):
    """Outcome of an execution approval check.

    Attributes:
        APPROVED: The command is allowed to proceed.
        DENIED: The command is blocked.
        NEEDS_REVIEW: The command requires human review before proceeding.
    """

    APPROVED = "approved"
    DENIED = "denied"
    NEEDS_REVIEW = "needs_review"


@reflect_system("security")
class ExecApprovalSystem:
    """Approval system for command executions based on allowlist/denylist rules.

    Evaluates :class:`ApprovalRequest` objects against the configured
    :class:`~pyclawops.config.schema.SecurityMode` and the set of ``safe_bins``
    and ``always_approve`` regex patterns.

    Priority order (highest to lowest):
    1. ``always_approve`` patterns — unconditionally allowed regardless of mode.
    2. Mode-specific logic (``allowlist``, ``denylist``, ``all``, ``none``).

    Attributes:
        mode (SecurityMode): The security mode controlling default allow/deny
            behaviour.
        safe_bins (set): Raw (unexpanded) set of safe binary paths from config.
        always_approve (List[Pattern[str]]): Compiled regex patterns for
            commands that are always approved.
        safe_bins_resolved (set): Expanded and resolved binary paths/basenames
            used for fast membership checks.
    """

    def __init__(self, config: ExecApprovalsConfig) -> None:
        """Initialise the approval system from a config object.

        Compiles ``always_approve`` strings into regex patterns and resolves
        all ``safe_bins`` paths to both their expanded form and basename.

        Args:
            config (ExecApprovalsConfig): Pydantic config object with fields
                ``mode``, ``safe_bins``, and ``always_approve``.
        """
        self.mode = config.mode
        self.safe_bins = set(config.safe_bins)
        self.always_approve = self._compile_patterns(config.always_approve)
        self.safe_bins_resolved = self._resolve_safe_bins()

    def _compile_patterns(self, patterns: List[str]) -> List[Pattern[str]]:
        """Compile a list of regex pattern strings into compiled regex objects.

        Invalid regex patterns are treated as literal strings via
        :func:`re.escape` so that all entries can always be matched.

        Args:
            patterns (List[str]): List of regex pattern strings to compile.

        Returns:
            List[Pattern[str]]: List of compiled :class:`re.Pattern` objects
                in the same order as *patterns*.
        """
        compiled = []
        for pattern in patterns:
            try:
                compiled.append(re.compile(pattern))
            except re.error:
                # Treat as literal string if not valid regex
                compiled.append(re.compile(re.escape(pattern)))
        return compiled

    def _resolve_safe_bins(self) -> set:
        """Resolve safe bin paths to absolute paths and their basenames.

        For each path in :attr:`safe_bins`, expands ``~`` and environment
        variables, then adds both the expanded form and its basename to
        the returned set so that commands can be matched by either full
        path or name alone.

        Returns:
            set: Set of expanded binary paths and basenames for membership
                testing.
        """
        resolved = set()
        for bin_path in self.safe_bins:
            # Expand ~ and environment variables
            expanded = os.path.expandvars(os.path.expanduser(bin_path))
            resolved.add(expanded)
            # Also add the basename
            resolved.add(os.path.basename(expanded))
        return resolved

    def _extract_command(self, command: str) -> str:
        """Extract the base command (first token) from a command string.

        Splits *command* on whitespace and returns the first token.  If the
        token contains ``/`` it is tilde-expanded; otherwise it is returned
        as-is (shell name lookup applies at execution time).

        Args:
            command (str): Raw command string, possibly including arguments.

        Returns:
            str: The base command name or path, or ``""`` if *command* is
                empty.
        """
        # Handle quoted commands
        parts = command.strip().split()
        if not parts:
            return ""

        cmd = parts[0]

        # Handle shell builtins and paths
        if "/" in cmd:
            return os.path.expanduser(cmd)
        return cmd

    def _is_safe_bin(self, command: str) -> bool:
        """Check whether the base command of *command* is in the safe-bins set.

        Args:
            command (str): Command string whose first token is checked against
                :attr:`safe_bins_resolved`.

        Returns:
            bool: True if the extracted command matches a safe bin entry.
        """
        cmd = self._extract_command(command)
        if not cmd:
            return False

        # Check against resolved safe bins
        return cmd in self.safe_bins_resolved

    def _matches_always_approve(self, command: str) -> bool:
        """Check whether *command* matches any ``always_approve`` regex pattern.

        Args:
            command (str): Full command string (including arguments) to test.

        Returns:
            bool: True if any ``always_approve`` pattern matches.
        """
        for pattern in self.always_approve:
            if pattern.search(command):
                return True
        return False

    async def should_approve(
        self, request: ApprovalRequest
    ) -> tuple[ApprovalDecision, Optional[str]]:
        """Determine whether a command execution request should be approved.

        Evaluates ``always_approve`` patterns first; if none match,
        delegates to the mode-specific allow/deny logic.

        Args:
            request (ApprovalRequest): The execution request to evaluate.

        Returns:
            tuple[ApprovalDecision, Optional[str]]: A 2-tuple of the
                decision and a human-readable reason string.
        """
        full_command = request.command
        if request.args:
            full_command += " " + " ".join(request.args)

        # Check always_approve patterns first (highest priority)
        if self._matches_always_approve(full_command):
            return ApprovalDecision.APPROVED, "matches always_approve pattern"

        # Check safe bins based on mode
        if self.mode == SecurityMode.ALLOWLIST:
            if self._is_safe_bin(request.command):
                return ApprovalDecision.APPROVED, "command in allowlist"
            return ApprovalDecision.DENIED, f"command not in allowlist: {self._extract_command(request.command)}"

        elif self.mode == SecurityMode.DENYLIST:
            if self._is_safe_bin(request.command):
                return ApprovalDecision.DENIED, "command in denylist"
            return ApprovalDecision.APPROVED, "command not in denylist"

        elif self.mode == SecurityMode.ALL:
            return ApprovalDecision.APPROVED, "mode=all allows all commands"

        elif self.mode == SecurityMode.NONE:
            return ApprovalDecision.DENIED, "mode=none denies all commands"

        return ApprovalDecision.DENIED, "unknown mode"

    async def approve(self, request: ApprovalRequest) -> bool:
        """Quick boolean check of whether a request is approved.

        Calls :meth:`should_approve` and returns True only when the decision
        is :attr:`ApprovalDecision.APPROVED`.

        Args:
            request (ApprovalRequest): The execution request to evaluate.

        Returns:
            bool: True if approved, False otherwise.
        """
        decision, _ = await self.should_approve(request)
        return decision == ApprovalDecision.APPROVED

    def get_safe_bins(self) -> List[str]:
        """Return the current list of configured safe binaries.

        Returns:
            List[str]: A list of raw (unexpanded) safe binary strings as
                they were provided in the config.
        """
        return list(self.safe_bins)

    def add_safe_bin(self, bin_path: str) -> None:
        """Add a binary to the safe-bins allowlist at runtime.

        Expands ``~`` in *bin_path*, adds the expanded path and its basename
        to both :attr:`safe_bins` and :attr:`safe_bins_resolved`.

        Args:
            bin_path (str): Path or name of the binary to allow.
        """
        expanded = os.path.expanduser(bin_path)
        self.safe_bins.add(expanded)
        self.safe_bins_resolved.add(expanded)
        self.safe_bins_resolved.add(os.path.basename(expanded))

    def remove_safe_bin(self, bin_path: str) -> None:
        """Remove a binary from the safe-bins allowlist at runtime.

        Expands ``~`` in *bin_path* and discards the expanded path and its
        basename from both :attr:`safe_bins` and :attr:`safe_bins_resolved`.

        Args:
            bin_path (str): Path or name of the binary to remove.
        """
        expanded = os.path.expanduser(bin_path)
        self.safe_bins.discard(expanded)
        self.safe_bins_resolved.discard(expanded)
        self.safe_bins_resolved.discard(os.path.basename(expanded))

    def is_command_allowed(self, command: str) -> bool:
        """Synchronous check of whether a command string would be allowed.

        Useful for quick pre-flight checks without constructing a full
        :class:`ApprovalRequest`.  Does not account for ``request.args``
        in the ``always_approve`` check.

        Args:
            command (str): Command string to evaluate (may include arguments
                for the ``always_approve`` pattern check).

        Returns:
            bool: True if the command would be approved under current rules.
        """
        # Check always_approve patterns
        for pattern in self.always_approve:
            if pattern.search(command):
                return True

        # Check safe bins
        if self.mode == SecurityMode.ALLOWLIST:
            return self._is_safe_bin(command)
        elif self.mode == SecurityMode.DENYLIST:
            return not self._is_safe_bin(command)
        elif self.mode == SecurityMode.ALL:
            return True
        return False

    def get_status(self) -> dict:
        """Return a status snapshot of the approval system configuration.

        Returns:
            dict: A dict with keys ``"mode"`` (str), ``"safe_bins"``
                (list[str]), and ``"always_approve_patterns"`` (list[str]).
        """
        return {
            "mode": self.mode.value,
            "safe_bins": list(self.safe_bins),
            "always_approve_patterns": [p.pattern for p in self.always_approve],
        }
