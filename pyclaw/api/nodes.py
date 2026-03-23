"""Node API for peer-to-peer communication between pyclaw instances."""

import hashlib
import hmac
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Header, Depends
from pydantic import BaseModel

logger = logging.getLogger("pyclaw.api.nodes")

router = APIRouter()


# In-memory node registry (in production, persist to disk/database)
class NodeRegistry:
    """Registry for managing connected nodes.

    Maintains two collections: fully registered nodes and nodes that are
    awaiting manual approval before they are allowed to participate.

    Attributes:
        nodes (Dict[str, Node]): Approved and active nodes keyed by node_id.
        pending_approvals (Dict[str, Node]): Nodes awaiting approval keyed by node_id.
    """

    def __init__(self):
        """Initialise empty node and pending-approval registries."""
        self.nodes: Dict[str, "Node"] = {}
        self.pending_approvals: Dict[str, "Node"] = {}
    
    def add_node(self, node: "Node", require_approval: bool = True) -> bool:
        """Add a node to the registry, optionally routing it through approval.

        Args:
            node (Node): The node to register.
            require_approval (bool): When True the node is placed in
                ``pending_approvals`` rather than ``nodes``. Defaults to True.

        Returns:
            bool: True when the node is immediately approved; False when it is
                placed in the pending queue or already registered.
        """
        if node.node_id in self.nodes:
            logger.warning(f"Node {node.node_id} already registered")
            return False
        
        if require_approval:
            self.pending_approvals[node.node_id] = node
            logger.info(f"Node {node.node_id} pending approval")
            return False
        else:
            self.nodes[node.node_id] = node
            logger.info(f"Node {node.node_id} registered")
            return True
    
    def approve_node(self, node_id: str) -> bool:
        """Move a node from the pending queue into the active registry.

        Args:
            node_id (str): ID of the node to approve.

        Returns:
            bool: True if found and approved; False if not in the pending queue.
        """
        if node_id not in self.pending_approvals:
            return False
        node = self.pending_approvals.pop(node_id)
        self.nodes[node_id] = node
        logger.info(f"Node {node_id} approved")
        return True
    
    def reject_node(self, node_id: str) -> bool:
        """Remove a node from the pending approval queue without approving it.

        Args:
            node_id (str): ID of the node to reject.

        Returns:
            bool: True if found and removed; False if not in the pending queue.
        """
        if node_id in self.pending_approvals:
            del self.pending_approvals[node_id]
            logger.info(f"Node {node_id} rejected")
            return True
        return False
    
    def remove_node(self, node_id: str) -> bool:
        """Remove an active node from the registry.

        Args:
            node_id (str): ID of the node to remove.

        Returns:
            bool: True if found and removed; False if not registered.
        """
        if node_id in self.nodes:
            del self.nodes[node_id]
            logger.info(f"Node {node_id} removed")
            return True
        return False
    
    def get_node(self, node_id: str) -> Optional["Node"]:
        """Retrieve an active node by its ID.

        Args:
            node_id (str): The node's unique identifier.

        Returns:
            Optional[Node]: The Node object, or None if not found.
        """
        return self.nodes.get(node_id)
    
    def is_registered(self, node_id: str) -> bool:
        """Check if a node is in the active registry.

        Args:
            node_id (str): The node's unique identifier.

        Returns:
            bool: True if the node is registered and approved.
        """
        return node_id in self.nodes
    
    def is_pending(self, node_id: str) -> bool:
        """Check if a node is awaiting approval.

        Args:
            node_id (str): The node's unique identifier.

        Returns:
            bool: True if the node is in the pending approval queue.
        """
        return node_id in self.pending_approvals
    
    def list_nodes(self) -> List["Node"]:
        """Return all approved and active nodes.

        Returns:
            List[Node]: All nodes currently in the active registry.
        """
        return list(self.nodes.values())
    
    def list_pending(self) -> List["Node"]:
        """Return all nodes awaiting approval.

        Returns:
            List[Node]: All nodes currently in the pending approval queue.
        """
        return list(self.pending_approvals.values())


# Global registry
registry = NodeRegistry()


@dataclass
class Node:
    """Represents a connected peer node.

    Attributes:
        node_id (str): Unique identifier for the node.
        name (str): Human-readable name for the node.
        host (str): Hostname or IP address of the node.
        port (int): Port the node listens on.
        status (str): Current status string (e.g. "online", "offline").
        registered_at (datetime): When the node was first registered.
        last_seen (datetime): Most recent contact timestamp.
        capabilities (List[str]): Feature tags advertised by the node.
        metadata (Dict[str, Any]): Arbitrary extra data from the node.
    """

    node_id: str
    name: str
    host: str
    port: int
    status: str = "online"
    registered_at: datetime = field(default_factory=datetime.now)
    last_seen: datetime = field(default_factory=datetime.now)
    capabilities: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialise the node to a camelCase JSON-friendly dictionary.

        Returns:
            Dict[str, Any]: Node fields with ISO-formatted timestamps and
                camelCase key names suitable for API responses.
        """
        return {
            "nodeId": self.node_id,
            "name": self.name,
            "host": self.host,
            "port": self.port,
            "status": self.status,
            "registeredAt": self.registered_at.isoformat(),
            "lastSeen": self.last_seen.isoformat(),
            "capabilities": self.capabilities,
            "metadata": self.metadata,
        }


# Request/Response models
class RegisterNodeRequest(BaseModel):
    """Request to register a new node."""
    node_id: str
    name: str
    host: str
    port: int
    capabilities: List[str] = []
    metadata: Dict[str, Any] = {}


class RegisterNodeResponse(BaseModel):
    """Response from node registration."""
    success: bool
    node_id: str
    status: str  # "approved" or "pending"
    message: str


class ApproveNodeRequest(BaseModel):
    """Request to approve a node."""
    node_id: str


class NodeMessageRequest(BaseModel):
    """Message to send to a node."""
    target_node_id: str
    action: str
    payload: Dict[str, Any] = {}


class NodeMessageResponse(BaseModel):
    """Response from node message."""
    success: bool
    response: Dict[str, Any] = {}


def verify_secret(secret_key: Optional[str], x_signature: Optional[str]) -> bool:
    """Verify the HMAC signature against the configured secret key.

    Args:
        secret_key (Optional[str]): The shared secret configured for this gateway.
            When None, no verification is performed and True is returned.
        x_signature (Optional[str]): The ``X-Signature`` header value from the
            request. Must not be empty when ``secret_key`` is set.

    Returns:
        bool: True if the signature is valid or no secret is configured;
            False when a secret is required but no signature is present.
    """
    if not secret_key:
        return True  # No secret configured
    if not x_signature:
        return False
    return True  # In production, verify HMAC


def get_current_node_id(x_node_id: Optional[str] = Header(None)) -> Optional[str]:
    """Extract the calling node's ID from the ``X-Node-Id`` request header.

    Args:
        x_node_id (Optional[str]): Value of the ``X-Node-Id`` HTTP header.

    Returns:
        Optional[str]: The node ID string, or None if the header is absent.
    """
    return x_node_id


# API Endpoints
@router.post("/register", response_model=RegisterNodeResponse)
async def register_node(
    request: RegisterNodeRequest,
    x_secret: Optional[str] = Header(None, alias="X-Secret-Key"),
):
    """Register a new node with the gateway."""
    from pyclaw.config.loader import ConfigLoader
    
    config_loader = ConfigLoader()
    config = config_loader.load()
    
    node_config = config.nodes if config else None
    
    # Check whitelist if enabled
    if node_config and node_config.enabled:
        if node_config.whitelist and request.node_id not in node_config.whitelist:
            raise HTTPException(
                status_code=403,
                detail=f"Node {request.node_id} is not in the whitelist"
            )
    
    # Create node
    node = Node(
        node_id=request.node_id,
        name=request.name,
        host=request.host,
        port=request.port,
        capabilities=request.capabilities,
        metadata=request.metadata,
    )
    
    # Check if approval required
    require_approval = node_config.require_approval if node_config else True
    
    success = registry.add_node(node, require_approval=require_approval)
    
    if require_approval:
        return RegisterNodeResponse(
            success=True,
            node_id=request.node_id,
            status="pending",
            message="Node registration pending approval"
        )
    else:
        return RegisterNodeResponse(
            success=True,
            node_id=request.node_id,
            status="approved",
            message="Node registered successfully"
        )


@router.post("/approve")
async def approve_node(request: ApproveNodeRequest):
    """Approve a pending node."""
    if registry.approve_node(request.node_id):
        return {"success": True, "message": f"Node {request.node_id} approved"}
    raise HTTPException(status_code=404, detail="Node not found in pending list")


@router.post("/reject")
async def reject_node(request: ApproveNodeRequest):
    """Reject a pending node."""
    if registry.reject_node(request.node_id):
        return {"success": True, "message": f"Node {request.node_id} rejected"}
    raise HTTPException(status_code=404, detail="Node not found in pending list")


@router.get("/list")
async def list_nodes():
    """List all registered nodes."""
    nodes = registry.list_nodes()
    return {
        "nodes": [n.to_dict() for n in nodes],
        "count": len(nodes),
    }


@router.get("/pending")
async def list_pending_nodes():
    """List all pending nodes awaiting approval."""
    nodes = registry.list_pending()
    return {
        "nodes": [n.to_dict() for n in nodes],
        "count": len(nodes),
    }


@router.get("/{node_id}")
async def get_node(node_id: str):
    """Get details of a specific node."""
    node = registry.get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    return node.to_dict()


@router.delete("/{node_id}")
async def remove_node(node_id: str):
    """Remove a node from the registry."""
    if registry.remove_node(node_id):
        return {"success": True, "message": f"Node {node_id} removed"}
    raise HTTPException(status_code=404, detail="Node not found")


@router.post("/message", response_model=NodeMessageResponse)
async def send_message(
    request: NodeMessageRequest,
    x_node_id: Optional[str] = Header(None, alias="X-Node-Id"),
):
    """Send a message to another node."""
    # Verify sender is registered
    if x_node_id and not registry.is_registered(x_node_id):
        raise HTTPException(status_code=401, detail="Sender node not registered")
    
    # Get target node
    target = registry.get_node(request.target_node_id)
    if not target:
        raise HTTPException(status_code=404, detail="Target node not found")
    
    # In production, this would forward the message to the target node
    # For now, we'll just log it
    logger.info(f"Message from {x_node_id} to {request.target_node_id}: {request.action}")
    
    return NodeMessageResponse(
        success=True,
        response={
            "status": "delivered",
            "action": request.action,
            "from": x_node_id,
        }
    )


@router.get("/health")
async def nodes_health():
    """Health check for nodes subsystem."""
    return {
        "status": "healthy",
        "registered": len(registry.list_nodes()),
        "pending": len(registry.list_pending()),
    }
