"""Execution approval system for pyclaw."""

import re
import os
from dataclasses import dataclass
from typing import List, Optional, Pattern
from enum import Enum

from pyclaw.config.schema import SecurityMode, ExecApprovalsConfig


@dataclass
class ApprovalRequest:
    """Request for command execution approval."""
    command: str
    args: List[str]
    cwd: str
    agent_id: str
    session_id: str
    user_id: Optional[str] = None


class ApprovalDecision(str, Enum):
    """Approval decision result."""
    APPROVED = "approved"
    DENIED = "denied"
    NEEDS_REVIEW = "needs_review"


class ExecApprovalSystem:
    """System for approving command executions based on allowlist/denylist."""
    
    def __init__(self, config: ExecApprovalsConfig):
        self.mode = config.mode
        self.safe_bins = set(config.safe_bins)
        self.always_approve = self._compile_patterns(config.always_approve)
        self.safe_bins_resolved = self._resolve_safe_bins()
    
    def _compile_patterns(self, patterns: List[str]) -> List[Pattern[str]]:
        """Compile regex patterns from strings."""
        compiled = []
        for pattern in patterns:
            try:
                compiled.append(re.compile(pattern))
            except re.error:
                # Treat as literal string if not valid regex
                compiled.append(re.compile(re.escape(pattern)))
        return compiled
    
    def _resolve_safe_bins(self) -> set:
        """Resolve safe bin paths to absolute paths."""
        resolved = set()
        for bin_path in self.safe_bins:
            # Expand ~ and environment variables
            expanded = os.path.expandvars(os.path.expanduser(bin_path))
            resolved.add(expanded)
            # Also add the basename
            resolved.add(os.path.basename(expanded))
        return resolved
    
    def _extract_command(self, command: str) -> str:
        """Extract the base command from a full command string."""
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
        """Check if command uses a safe binary."""
        cmd = self._extract_command(command)
        if not cmd:
            return False
        
        # Check against resolved safe bins
        return cmd in self.safe_bins_resolved
    
    def _matches_always_approve(self, command: str) -> bool:
        """Check if command matches any always-approve pattern."""
        for pattern in self.always_approve:
            if pattern.search(command):
                return True
        return False
    
    async def should_approve(self, request: ApprovalRequest) -> tuple[ApprovalDecision, Optional[str]]:
        """
        Determine if a command should be approved.
        
        Returns:
            Tuple of (decision, reason)
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
        """
        Quick check if command is approved.
        
        Returns:
            True if approved, False otherwise
        """
        decision, _ = await self.should_approve(request)
        return decision == ApprovalDecision.APPROVED
    
    def get_safe_bins(self) -> List[str]:
        """Get list of safe binaries."""
        return list(self.safe_bins)
    
    def add_safe_bin(self, bin_path: str) -> None:
        """Add a safe binary to the allowlist."""
        expanded = os.path.expanduser(bin_path)
        self.safe_bins.add(expanded)
        self.safe_bins_resolved.add(expanded)
        self.safe_bins_resolved.add(os.path.basename(expanded))
    
    def remove_safe_bin(self, bin_path: str) -> None:
        """Remove a safe binary from the allowlist."""
        expanded = os.path.expanduser(bin_path)
        self.safe_bins.discard(expanded)
        self.safe_bins_resolved.discard(expanded)
        self.safe_bins_resolved.discard(os.path.basename(expanded))
    
    def is_command_allowed(self, command: str) -> bool:
        """Quick check if a command would be allowed (sync version)."""
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
        """Get approval system status."""
        return {
            "mode": self.mode.value,
            "safe_bins": list(self.safe_bins),
            "always_approve_patterns": [p.pattern for p in self.always_approve],
        }
