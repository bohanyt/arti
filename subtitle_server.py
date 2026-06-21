#!/usr/bin/env python3
"""
subtitle_server.py — WebSocket server untuk OBS subtitle real-time.
Kirim word timings dari TTS engine ke Browser Source di OBS.

Usage: python subtitle_server.py
Port: 9999
"""

import asyncio
import json
import websockets
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

connected_clients = set()
server = None

async def handler(websocket, path=None):
    """Handle new WebSocket connection."""
    connected_clients.add(websocket)
    client_ip = websocket.remote_address[0] if websocket.remote_address else "unknown"
    print(f"[SubTitle] Client connected: {client_ip} (total: {len(connected_clients)})")
    try:
        async for message in websocket:
            # Handle incoming messages (optional)
            try:
                data = json.loads(message)
                if data.get("type") == "ping":
                    await websocket.send(json.dumps({"type": "pong"}))
            except:
                pass
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        connected_clients.discard(websocket)
        print(f"[SubTitle] Client disconnected: {client_ip} (total: {len(connected_clients)})")

async def broadcast_subtitle(word_timings, full_text):
    """Broadcast word timings to all connected clients."""
    if not connected_clients:
        return
    
    message = json.dumps({
        "type": "subtitle",
        "words": word_timings,
        "text": full_text,
        "timestamp": asyncio.get_event_loop().time()
    })
    
    # Send to all connected clients
    disconnected = set()
    for client in connected_clients:
        try:
            await client.send(message)
        except websockets.exceptions.ConnectionClosed:
            disconnected.add(client)
        except Exception as e:
            print(f"[SubTitle] Send error: {e}")
            disconnected.add(client)
    
    # Clean up disconnected clients
    for client in disconnected:
        connected_clients.discard(client)

async def broadcast_status(status, message=""):
    """Broadcast status update to all clients."""
    if not connected_clients:
        return
    
    msg = json.dumps({
        "type": "status",
        "status": status,
        "message": message
    })
    
    disconnected = set()
    for client in connected_clients:
        try:
            await client.send(msg)
        except:
            disconnected.add(client)
    
    for client in disconnected:
        connected_clients.discard(client)

# Global reference for external access
async def send_subtitle(word_timings, full_text):
    """External function to send subtitle data."""
    await broadcast_subtitle(word_timings, full_text)

async def main():
    """Start WebSocket server."""
    global server
    port = 9988
    host = "0.0.0.0"
    
    server = await websockets.serve(handler, host, port, reuse_address=True)
    print(f"[SubTitle] Server started on ws://localhost:{port}")
    print(f"[SubTitle] Waiting for OBS Browser Source connections...")
    
    await server.wait_closed()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[SubTitle] Server stopped.")
