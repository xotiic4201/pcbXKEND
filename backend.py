"""
Remote PC Control - Complete Backend Server
Handles WebSocket connections, agent management, and web interface
"""

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import asyncio
import json
import os
import time
import uuid
import logging
from typing import Dict, Set, Optional, List, Any
from datetime import datetime
import secrets

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize FastAPI
app = FastAPI(
    title="Remote PC Control Server",
    description="Control remote PCs via WebSocket",
    version="1.0.0"
)

# CORS Configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==================== CONFIGURATION ====================
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
JWT_SECRET = os.getenv("JWT_SECRET", secrets.token_urlsafe(32))
API_KEY = os.getenv("API_KEY", secrets.token_urlsafe(16))

# ==================== DATA STORAGE ====================
# Connected agents: agent_id -> websocket
connected_agents: Dict[str, WebSocket] = {}

# Connected clients: client_id -> websocket
connected_clients: Dict[str, WebSocket] = {}

# Agent information cache: agent_id -> info
agent_info: Dict[str, Dict] = {}

# Active screen sharing sessions: agent_id -> bool
active_screens: Set[str] = set()

# Command queues: agent_id -> list of pending commands
command_queues: Dict[str, List[Dict]] = {}

# User sessions: token -> user_info
user_sessions: Dict[str, Dict] = {}

# Registered users: username -> user_data
users: Dict[str, Dict] = {
    ADMIN_USERNAME: {
        "username": ADMIN_USERNAME,
        "password": ADMIN_PASSWORD,  # Plain text (for demo)
        "is_admin": True,
        "created_at": datetime.now().isoformat()
    }
}

# ==================== MODELS ====================
class LoginRequest(BaseModel):
    username: str
    password: str

class LoginResponse(BaseModel):
    success: bool
    access_token: Optional[str]
    is_admin: bool
    message: str

class CommandRequest(BaseModel):
    agent_id: str
    type: str
    data: Dict[str, Any] = {}

# ==================== UTILITY FUNCTIONS ====================
def create_session(username: str, is_admin: bool) -> str:
    """Create a new user session"""
    token = secrets.token_urlsafe(32)
    user_sessions[token] = {
        "username": username,
        "is_admin": is_admin,
        "created_at": time.time(),
        "expires_at": time.time() + 86400  # 24 hours
    }
    return token

def verify_session(token: str) -> Optional[Dict]:
    """Verify a session token"""
    if token not in user_sessions:
        return None
    session = user_sessions[token]
    if time.time() > session["expires_at"]:
        del user_sessions[token]
        return None
    return session

def cleanup_sessions():
    """Remove expired sessions"""
    now = time.time()
    expired = [t for t, s in user_sessions.items() if now > s["expires_at"]]
    for t in expired:
        del user_sessions[t]

# ==================== API ENDPOINTS ====================
@app.get("/")
async def root():
    """Root endpoint - serves the web interface"""
    with open("index.html", "r") as f:
        html_content = f.read()
    return HTMLResponse(content=html_content)

@app.get("/api/status")
async def get_status():
    """Get server status"""
    return {
        "online": True,
        "agents": len(connected_agents),
        "clients": len(connected_clients),
        "timestamp": int(time.time()),
        "version": "1.0.0"
    }

@app.post("/api/login", response_model=LoginResponse)
async def login(request: LoginRequest):
    """Login endpoint"""
    # Find user
    user = users.get(request.username)
    if not user or user["password"] != request.password:
        return LoginResponse(
            success=False,
            access_token=None,
            is_admin=False,
            message="Invalid username or password"
        )
    
    # Create session
    token = create_session(request.username, user["is_admin"])
    
    return LoginResponse(
        success=True,
        access_token=token,
        is_admin=user["is_admin"],
        message="Login successful"
    )

@app.get("/api/agents")
async def get_agents(request: Request):
    """Get list of connected agents"""
    # Verify authentication
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    token = auth_header.replace("Bearer ", "")
    session = verify_session(token)
    if not session:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    
    # Return agent list with info
    agents = []
    for agent_id, ws in connected_agents.items():
        info = agent_info.get(agent_id, {})
        agents.append({
            "id": agent_id,
            "connected": True,
            "last_seen": time.time(),
            **info
        })
    
    return agents

@app.post("/api/command")
async def send_command(request: CommandRequest, req: Request):
    """Send command to an agent"""
    # Verify authentication
    auth_header = req.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    token = auth_header.replace("Bearer ", "")
    session = verify_session(token)
    if not session:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    
    # Check if agent exists
    if request.agent_id not in connected_agents:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    # Queue command
    ws = connected_agents[request.agent_id]
    try:
        await ws.send_json({
            "type": request.type,
            "data": request.data
        })
        return {"success": True, "message": "Command sent"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to send command: {e}")

# ==================== WEBSOCKET ENDPOINTS ====================
@app.websocket("/ws/agent/{agent_id}")
async def websocket_agent(websocket: WebSocket, agent_id: str):
    """WebSocket endpoint for PC agents"""
    await websocket.accept()
    logger.info(f"✅ Agent connected: {agent_id}")
    
    connected_agents[agent_id] = websocket
    
    try:
        while True:
            # Receive message
            data = await websocket.receive_text()
            message = json.loads(data)
            
            # Handle different message types
            msg_type = message.get("type")
            
            if msg_type == "system_info":
                # Store agent info
                agent_info[agent_id] = message.get("data", {})
                logger.info(f"📊 Received system info from {agent_id}")
                
            elif msg_type in ["command_output", "screen_frame", "process_list"]:
                # Forward to all connected clients
                for client_id, client_ws in connected_clients.items():
                    try:
                        await client_ws.send_json({
                            "type": "agent_response",
                            "agent_id": agent_id,
                            "data": message
                        })
                    except:
                        pass
                        
            elif msg_type == "screen_frame":
                # Update active screen status
                if agent_id not in active_screens:
                    active_screens.add(agent_id)
                    
                # Forward to clients
                for client_id, client_ws in connected_clients.items():
                    try:
                        await client_ws.send_json({
                            "type": "screen_frame",
                            "agent_id": agent_id,
                            "image": message.get("image"),
                            "timestamp": message.get("timestamp")
                        })
                    except:
                        pass
                        
            elif msg_type == "screen_stopped":
                if agent_id in active_screens:
                    active_screens.remove(agent_id)
                    
    except WebSocketDisconnect:
        logger.info(f"🔌 Agent disconnected: {agent_id}")
    except Exception as e:
        logger.error(f"❌ Agent error {agent_id}: {e}")
    finally:
        # Cleanup
        if agent_id in connected_agents:
            del connected_agents[agent_id]
        if agent_id in active_screens:
            active_screens.remove(agent_id)

@app.websocket("/ws/client/{client_id}")
async def websocket_client(websocket: WebSocket, client_id: str):
    """WebSocket endpoint for web clients"""
    await websocket.accept()
    logger.info(f"✅ Client connected: {client_id}")
    
    connected_clients[client_id] = websocket
    
    try:
        while True:
            # Receive command from client
            data = await websocket.receive_text()
            message = json.loads(data)
            
            cmd_type = message.get("type")
            agent_id = message.get("agent_id")
            cmd_data = message.get("data", {})
            
            logger.info(f"📨 Client {client_id} -> Agent {agent_id}: {cmd_type}")
            
            # Forward to agent if connected
            if agent_id in connected_agents:
                agent_ws = connected_agents[agent_id]
                try:
                    await agent_ws.send_json({
                        "type": cmd_type,
                        "data": cmd_data,
                        "client_id": client_id
                    })
                    
                    # Acknowledge to client
                    await websocket.send_json({
                        "type": "command_queued",
                        "agent_id": agent_id,
                        "command": cmd_type
                    })
                except Exception as e:
                    await websocket.send_json({
                        "type": "error",
                        "message": f"Failed to send to agent: {e}"
                    })
            else:
                await websocket.send_json({
                    "type": "error",
                    "message": f"Agent {agent_id} not connected"
                })
                
    except WebSocketDisconnect:
        logger.info(f"🔌 Client disconnected: {client_id}")
    except Exception as e:
        logger.error(f"❌ Client error {client_id}: {e}")
    finally:
        if client_id in connected_clients:
            del connected_clients[client_id]

# ==================== BACKGROUND TASKS ====================
@app.on_event("startup")
async def startup_event():
    """Startup tasks"""
    logger.info("=" * 60)
    logger.info("🚀 Remote PC Control Server Starting...")
    logger.info(f"📡 WebSocket endpoint: ws://localhost:8000/ws/{{agent/client}}/{{id}}")
    logger.info(f"👤 Admin user: {ADMIN_USERNAME}")
    logger.info(f"🔑 API Key: {API_KEY}")
    logger.info("=" * 60)
    
    # Start background task for cleanup
    asyncio.create_task(cleanup_task())

@app.on_event("shutdown")
async def shutdown_event():
    """Shutdown tasks"""
    logger.info("🛑 Server shutting down...")

async def cleanup_task():
    """Periodic cleanup of expired sessions"""
    while True:
        await asyncio.sleep(300)  # Every 5 minutes
        cleanup_sessions()
        logger.debug(f"🧹 Cleaned up sessions. Active: {len(user_sessions)}")

# ==================== RUN SERVER ====================
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(
        "backend:app",
        host="0.0.0.0",
        port=port,
        reload=os.getenv("ENVIRONMENT") == "development"
    )
