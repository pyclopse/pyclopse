"""Channel webhook API routes."""
import logging
from typing import Dict, Any, Optional

from fastapi import APIRouter, HTTPException, Header, Depends
from pydantic import BaseModel

logger = logging.getLogger("pyclaw.api.channels")

router = APIRouter()


# Webhook payload models
class TelegramWebhookUpdate(BaseModel):
    """Telegram webhook update payload."""
    update_id: int
    message: Optional[Dict[str, Any]] = None
    edited_message: Optional[Dict[str, Any]] = None
    callback_query: Optional[Dict[str, Any]] = None


class DiscordWebhookPayload(BaseModel):
    """Discord webhook payload."""
    type: Optional[int] = None
    t: Optional[str] = None  # Event type
    d: Optional[Dict[str, Any]] = None  # Event data


class SlackWebhookPayload(BaseModel):
    """Slack webhook payload."""
    type: str
    challenge: Optional[str] = None
    event: Optional[Dict[str, Any]] = None


class WebhookResponse(BaseModel):
    """Generic webhook response."""
    ok: bool
    message: Optional[str] = None
    error: Optional[str] = None


# Helper dependency to get channel adapter
async def get_channel_adapter(channel_name: str):
    """Get channel adapter from gateway."""
    from pyclaw.api.app import get_gateway
    
    gateway = get_gateway()
    
    if not hasattr(gateway, 'channels'):
        raise HTTPException(status_code=500, detail="Channels not initialized")
    
    adapter = gateway.channels.get(channel_name)
    if not adapter:
        raise HTTPException(status_code=404, detail=f"Channel '{channel_name}' not found")
    
    return adapter


async def verify_telegram_token(x_telegram_bot_api_secret: Optional[str] = Header(None)) -> bool:
    """Verify Telegram webhook token."""
    # In production, verify against configured secret
    return True


async def verify_discord_token(xdiscord_signing_secret: Optional[str] = Header(None)) -> bool:
    """Verify Discord webhook token."""
    # In production, verify against configured secret
    return True


async def verify_slack_token(x_slack_signature: Optional[str] = Header(None), x_slack_request_timestamp: Optional[str] = Header(None)) -> bool:
    """Verify Slack webhook request."""
    # In production, verify Slack signature
    return True


# Telegram webhook endpoint
@router.post("/webhook/telegram", response_model=WebhookResponse)
async def telegram_webhook(
    payload: TelegramWebhookUpdate,
    verified: bool = Depends(verify_telegram_token),
):
    """Handle incoming Telegram webhook."""
    try:
        adapter = await get_channel_adapter("telegram")
        
        # Convert payload to dict for adapter
        payload_dict = payload.model_dump(exclude_none=True)
        
        # Handle the webhook
        message = await adapter.handle_webhook(payload_dict)
        
        if message:
            # Dispatch to gateway for processing
            gateway = get_gateway()
            if hasattr(gateway, 'router'):
                await gateway.router.route_message(message)
            return WebhookResponse(ok=True, message="Message processed")
        
        return WebhookResponse(ok=True, message="No message to process")
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing Telegram webhook: {e}")
        return WebhookResponse(ok=False, error=str(e))


# Discord webhook endpoint
@router.post("/webhook/discord", response_model=WebhookResponse)
async def discord_webhook(
    payload: DiscordWebhookPayload,
    verified: bool = Depends(verify_discord_token),
):
    """Handle incoming Discord webhook."""
    try:
        adapter = await get_channel_adapter("discord")
        
        # Convert payload to dict for adapter
        payload_dict = payload.model_dump(exclude_none=True)
        
        # Handle the webhook
        message = await adapter.handle_webhook(payload_dict)
        
        if message:
            # Dispatch to gateway for processing
            gateway = get_gateway()
            if hasattr(gateway, 'router'):
                await gateway.router.route_message(message)
            return WebhookResponse(ok=True, message="Message processed")
        
        return WebhookResponse(ok=True, message="No message to process")
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing Discord webhook: {e}")
        return WebhookResponse(ok=False, error=str(e))


# Slack webhook endpoint
@router.post("/webhook/slack", response_model=WebhookResponse)
async def slack_webhook(
    payload: SlackWebhookPayload,
    verified: bool = Depends(verify_slack_token),
):
    """Handle incoming Slack webhook."""
    try:
        # Handle Slack URL verification challenge
        if payload.type == "url_verification":
            return WebhookResponse(ok=True, message=payload.challenge)
        
        adapter = await get_channel_adapter("slack")
        
        # Convert payload to dict for adapter
        payload_dict = payload.model_dump(exclude_none=True)
        
        # Handle the webhook
        message = await adapter.handle_webhook(payload_dict)
        
        if message:
            # Dispatch to gateway for processing
            gateway = get_gateway()
            if hasattr(gateway, 'router'):
                await gateway.router.route_message(message)
            return WebhookResponse(ok=True, message="Message processed")
        
        return WebhookResponse(ok=True, message="No message to process")
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing Slack webhook: {e}")
        return WebhookResponse(ok=False, error=str(e))


# List available channels
@router.get("/", response_model=Dict[str, Any])
async def list_channels():
    """List all available channels and their status."""
    try:
        gateway = get_gateway()
        
        if not hasattr(gateway, 'channels'):
            return {"channels": {}}
        
        channels = {}
        for name, adapter in gateway.channels.items():
            channels[name] = {
                "connected": adapter.is_connected if hasattr(adapter, 'is_connected') else False,
                "has_webhook": hasattr(adapter, 'handle_webhook'),
            }
        
        return {"channels": channels}
    
    except Exception as e:
        logger.error(f"Error listing channels: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Get channel status
@router.get("/{channel_name}/status", response_model=Dict[str, Any])
async def channel_status(channel_name: str):
    """Get status of a specific channel."""
    try:
        adapter = await get_channel_adapter(channel_name)
        
        return {
            "channel": channel_name,
            "connected": adapter.is_connected if hasattr(adapter, 'is_connected') else False,
            "has_webhook": hasattr(adapter, 'handle_webhook'),
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting channel status: {e}")
        raise HTTPException(status_code=500, detail=str(e))
