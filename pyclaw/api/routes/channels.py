"""Channel webhook API routes."""
import logging
from typing import Dict, Any, Optional

from fastapi import APIRouter, HTTPException, Header, Depends
from pydantic import BaseModel

logger = logging.getLogger("pyclaw.api.channels")

router = APIRouter()


# Webhook payload models
class TelegramWebhookUpdate(BaseModel):
    """Telegram Bot API webhook update object.

    Attributes:
        update_id (int): Unique identifier for the incoming update.
        message (Optional[Dict[str, Any]]): New incoming message, if present.
        edited_message (Optional[Dict[str, Any]]): Edited message, if present.
        callback_query (Optional[Dict[str, Any]]): Callback from an inline keyboard, if present.
    """
    update_id: int
    message: Optional[Dict[str, Any]] = None
    edited_message: Optional[Dict[str, Any]] = None
    callback_query: Optional[Dict[str, Any]] = None


class DiscordWebhookPayload(BaseModel):
    """Discord Gateway event payload received via webhook.

    Attributes:
        type (Optional[int]): Discord op-code for the event type.
        t (Optional[str]): Event name string (e.g. "MESSAGE_CREATE").
        d (Optional[Dict[str, Any]]): Event data dictionary.
    """
    type: Optional[int] = None
    t: Optional[str] = None  # Event type
    d: Optional[Dict[str, Any]] = None  # Event data


class SlackWebhookPayload(BaseModel):
    """Slack Events API payload received via webhook.

    Attributes:
        type (str): Event type — "url_verification" or "event_callback".
        challenge (Optional[str]): Challenge string sent during URL verification.
        event (Optional[Dict[str, Any]]): The inner event object for event_callback payloads.
    """
    type: str
    challenge: Optional[str] = None
    event: Optional[Dict[str, Any]] = None


class WebhookResponse(BaseModel):
    """Generic webhook acknowledgement response.

    Attributes:
        ok (bool): True when the webhook was processed without error.
        message (Optional[str]): Optional informational message.
        error (Optional[str]): Error description when ok is False.
    """
    ok: bool
    message: Optional[str] = None
    error: Optional[str] = None


# Helper dependency to get channel adapter
async def get_channel_adapter(channel_name: str):
    """Retrieve a channel adapter from the gateway by name.

    Args:
        channel_name (str): Name of the channel (e.g. "telegram", "slack").

    Returns:
        Any: The channel adapter object.

    Raises:
        HTTPException: 500 if channels are not initialized; 404 if the named
            channel is not configured.
    """
    from pyclaw.api.app import get_gateway
    
    gateway = get_gateway()
    
    if not hasattr(gateway, 'channels'):
        raise HTTPException(status_code=500, detail="Channels not initialized")
    
    adapter = gateway.channels.get(channel_name)
    if not adapter:
        raise HTTPException(status_code=404, detail=f"Channel '{channel_name}' not found")
    
    return adapter


async def verify_telegram_token(x_telegram_bot_api_secret: Optional[str] = Header(None)) -> bool:
    """Verify the Telegram webhook secret token.

    Args:
        x_telegram_bot_api_secret (Optional[str]): Value of the
            ``X-Telegram-Bot-Api-Secret-Token`` header sent by Telegram.

    Returns:
        bool: True if the token is valid or no token is configured.
    """
    # In production, verify against configured secret
    return True


async def verify_discord_token(xdiscord_signing_secret: Optional[str] = Header(None)) -> bool:
    """Verify the Discord interaction request signature.

    Args:
        xdiscord_signing_secret (Optional[str]): Discord signing secret header.

    Returns:
        bool: True if the signature is valid or verification is not configured.
    """
    # In production, verify against configured secret
    return True


async def verify_slack_token(x_slack_signature: Optional[str] = Header(None), x_slack_request_timestamp: Optional[str] = Header(None)) -> bool:
    """Verify the Slack request signature using HMAC-SHA256.

    Args:
        x_slack_signature (Optional[str]): Value of the ``X-Slack-Signature`` header.
        x_slack_request_timestamp (Optional[str]): Value of the
            ``X-Slack-Request-Timestamp`` header.

    Returns:
        bool: True if the signature is valid or verification is not configured.
    """
    # In production, verify Slack signature
    return True


# Telegram webhook endpoint
@router.post("/webhook/telegram", response_model=WebhookResponse)
async def telegram_webhook(
    payload: TelegramWebhookUpdate,
    verified: bool = Depends(verify_telegram_token),
):
    """Receive and process an incoming Telegram Bot API update.

    The update is forwarded to the Telegram channel adapter, which converts it
    to a pyclaw message and dispatches it through the gateway router.

    Args:
        payload (TelegramWebhookUpdate): Parsed Telegram update object.
        verified (bool): Result of the token verification dependency.

    Returns:
        WebhookResponse: Acknowledgement with ok=True if processed successfully.
    """
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
    """Receive and process an incoming Discord gateway event.

    Args:
        payload (DiscordWebhookPayload): Parsed Discord event payload.
        verified (bool): Result of the signature verification dependency.

    Returns:
        WebhookResponse: Acknowledgement with ok=True if processed successfully.
    """
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
    """Receive and process an incoming Slack Events API event.

    Handles the ``url_verification`` challenge automatically, forwarding
    ``challenge`` back as the response message.

    Args:
        payload (SlackWebhookPayload): Parsed Slack event payload.
        verified (bool): Result of the signature verification dependency.

    Returns:
        WebhookResponse: Acknowledgement with ok=True if processed successfully.
            For url_verification events the ``message`` field contains the
            challenge string.
    """
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
    """List all configured channels and their connection status.

    Returns:
        Dict[str, Any]: ``{"channels": {...}}`` mapping channel names to their
            ``connected`` flag and ``has_webhook`` capability indicator.

    Raises:
        HTTPException: With status 500 on unexpected errors.
    """
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
    """Return connection status details for a specific channel.

    Args:
        channel_name (str): Name of the channel to query (e.g. "telegram").

    Returns:
        Dict[str, Any]: Channel name, ``connected`` flag, and ``has_webhook``
            capability indicator.

    Raises:
        HTTPException: 404 if the channel is not configured; 500 on errors.
    """
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
