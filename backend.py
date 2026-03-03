"""
Professional Remote Desktop Control - Backend Server
Compatible with Pydantic V1 and Render Deployment
"""

import os
import json
import asyncio
import logging
import uuid
import hashlib
import time
from datetime import datetime, timedelta
from typing import Dict, Optional, List, Any
from collections import defaultdict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends, status, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, validator
import jwt
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ==================== Configuration ====================
USERNAME = os.getenv("USERNAME")
PASSWORD = os.getenv("PASSWORD")
JWT_SECRET = os.getenv("JWT_SECRET", os.urandom(32).hex())
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60

# ==================== FastAPI App ====================
app = FastAPI(title="Remote Desktop Control Backend")

# Security
security = HTTPBearer()

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://pcfront.vercel.app"],  # Allow all origins for testing
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==================== Pydantic Models (V1 Compatible) ====================
class LoginRequest(BaseModel):
    username: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str
    expires_in: int

class AgentInfo(BaseModel):
    agent_id: str
    hostname: str
    platform: str
    capabilities: Dict[str, bool]
    connected_at: str
    last_seen: str

# ==================== Connection Manager ====================
class ConnectionManager:
    def __init__(self):
        self.frontend_connections: Dict[str, Dict] = {}
        self.agent_connections: Dict[str, Dict] = {}
        self.login_attempts: Dict[str, List[datetime]] = defaultdict(list)

    async def connect_frontend(self, websocket: WebSocket, client_id: str):
        await websocket.accept()
        self.frontend_connections[client_id] = {
            "websocket": websocket,
            "authenticated": False,
            "user_id": None,
            "agent_id": None,
            "connected_at": datetime.now().isoformat()
        }
        logger.info(f"Frontend connected: {client_id}")

    def disconnect_frontend(self, client_id: str):
        if client_id in self.frontend_connections:
            del self.frontend_connections[client_id]
            logger.info(f"Frontend disconnected: {client_id}")

    async def connect_agent(self, websocket: WebSocket, agent_id: str, agent_info: Dict):
        await websocket.accept()
        self.agent_connections[agent_id] = {
            "websocket": websocket,
            "info": agent_info,
            "connected_at": datetime.now().isoformat(),
            "last_seen": datetime.now().isoformat(),
            "assigned_frontends": set()
        }
        logger.info(f"Agent connected: {agent_id} ({agent_info.get('hostname')})")
        
        # Send confirmation immediately
        try:
            await websocket.send_json({
                "type": "registered",
                "agent_id": agent_id,
                "status": "success"
            })
        except Exception as e:
            logger.error(f"Error sending registration confirmation: {e}")

    def disconnect_agent(self, agent_id: str):
        if agent_id in self.agent_connections:
            del self.agent_connections[agent_id]
            logger.info(f"Agent disconnected: {agent_id}")

    async def send_to_agent(self, agent_id: str, message: dict):
        if agent_id in self.agent_connections:
            try:
                await self.agent_connections[agent_id]["websocket"].send_json(message)
                return True
            except Exception as e:
                logger.error(f"Error sending to agent {agent_id}: {e}")
        return False

    async def send_to_frontend(self, client_id: str, message: dict):
        if client_id in self.frontend_connections:
            try:
                await self.frontend_connections[client_id]["websocket"].send_json(message)
                return True
            except Exception as e:
                logger.error(f"Error sending to frontend {client_id}: {e}")
        return False

    def get_agent_list(self) -> List[dict]:
        agents = []
        for agent_id, conn in self.agent_connections.items():
            agents.append({
                "agent_id": agent_id,
                "hostname": conn["info"].get("hostname", "Unknown"),
                "platform": conn["info"].get("platform", "Unknown"),
                "capabilities": conn["info"].get("capabilities", {}),
                "connected_at": conn["connected_at"],
                "last_seen": conn["last_seen"]
            })
        return agents

    def check_rate_limit(self, ip: str) -> bool:
        """Simple rate limiting - 5 attempts per 5 minutes"""
        now = datetime.now()
        five_minutes_ago = now - timedelta(minutes=5)
        
        # Clean old attempts
        self.login_attempts[ip] = [t for t in self.login_attempts[ip] if t > five_minutes_ago]
        
        if len(self.login_attempts[ip]) >= 5:
            return False
        
        self.login_attempts[ip].append(now)
        return True

manager = ConnectionManager()

# ==================== JWT Functions ====================
def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)

async def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired"
        )
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token"
        )

# ==================== REST Endpoints ====================
@app.get("/")
async def root():
    return {
        "name": "Remote Desktop Backend",
        "status": "operational",
        "timestamp": datetime.now().isoformat()
    }

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "connections": {
            "frontend": len(manager.frontend_connections),
            "agent": len(manager.agent_connections)
        }
    }

@app.post("/login", response_model=TokenResponse)
async def login(request: LoginRequest, x_forwarded_for: Optional[str] = None):
    client_ip = x_forwarded_for or "unknown"
    
    # Rate limiting
    if not manager.check_rate_limit(client_ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts"
        )
    
    # Verify credentials
    if request.username != USERNAME or request.password != PASSWORD:
        logger.warning(f"Failed login attempt from {client_ip}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password"
        )
    
    # Create token
    access_token = create_access_token(
        data={"sub": request.username, "ip": client_ip}
    )
    
    logger.info(f"Successful login from {client_ip}")
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60
    }

@app.get("/agents")
async def list_agents(token_data: dict = Depends(verify_token)):
    """List all connected agents"""
    return {"agents": manager.get_agent_list()}

# ==================== WebSocket Endpoints ====================
@app.websocket("/ws/frontend")
async def websocket_frontend(websocket: WebSocket):
    client_id = str(uuid.uuid4())
    await manager.connect_frontend(websocket, client_id)
    
    try:
        while True:
            data = await websocket.receive_json()
            
            # Handle authentication
            if data.get("type") == "auth":
                token = data.get("token")
                try:
                    payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
                    manager.frontend_connections[client_id]["authenticated"] = True
                    manager.frontend_connections[client_id]["user_id"] = payload.get("sub")
                    
                    await websocket.send_json({
                        "type": "auth_success",
                        "message": "Authentication successful",
                        "client_id": client_id
                    })
                    
                    # Send agent list
                    await websocket.send_json({
                        "type": "agent_list",
                        "agents": manager.get_agent_list()
                    })
                    
                    logger.info(f"Frontend {client_id} authenticated")
                    
                except jwt.PyJWTError:
                    await websocket.send_json({
                        "type": "auth_error",
                        "message": "Invalid token"
                    })
            
            # Handle commands that require authentication
            elif manager.frontend_connections[client_id]["authenticated"]:
                msg_type = data.get("type")
                
                if msg_type == "get_agents":
                    await websocket.send_json({
                        "type": "agent_list",
                        "agents": manager.get_agent_list()
                    })
                
                elif msg_type == "select_agent":
                    agent_id = data.get("agent_id")
                    if agent_id in manager.agent_connections:
                        manager.frontend_connections[client_id]["agent_id"] = agent_id
                        manager.agent_connections[agent_id]["assigned_frontends"].add(client_id)
                        
                        await websocket.send_json({
                            "type": "agent_assigned",
                            "agent_id": agent_id,
                            "agent_info": manager.agent_connections[agent_id]["info"]
                        })
                        
                        # Notify agent
                        await manager.send_to_agent(agent_id, {
                            "type": "frontend_assigned",
                            "frontend_id": client_id
                        })
                    else:
                        await websocket.send_json({
                            "type": "error",
                            "message": "Agent not found"
                        })
                
                elif msg_type == "deselect_agent":
                    agent_id = manager.frontend_connections[client_id].get("agent_id")
                    if agent_id and agent_id in manager.agent_connections:
                        manager.agent_connections[agent_id]["assigned_frontends"].discard(client_id)
                    
                    manager.frontend_connections[client_id]["agent_id"] = None
                    await websocket.send_json({"type": "agent_deselected"})
                
                elif msg_type in ["get_system_info", "start_stream", "stop_stream", 
                                 "mouse_event", "keyboard_event", "execute_command",
                                 "list_files", "file_transfer", "clipboard_sync"]:
                    
                    # Forward to assigned agent
                    agent_id = manager.frontend_connections[client_id].get("agent_id")
                    if agent_id:
                        await manager.send_to_agent(agent_id, data)
                    else:
                        await websocket.send_json({
                            "type": "error",
                            "message": "No agent selected"
                        })
                
                elif msg_type == "ping":
                    await websocket.send_json({
                        "type": "pong",
                        "timestamp": data.get("timestamp", time.time())
                    })
                
                else:
                    logger.warning(f"Unknown message type: {msg_type}")
            
            else:
                await websocket.send_json({
                    "type": "error",
                    "message": "Authentication required"
                })
    
    except WebSocketDisconnect:
        manager.disconnect_frontend(client_id)
    except Exception as e:
        logger.error(f"Frontend error: {e}")
        manager.disconnect_frontend(client_id)

@app.websocket("/ws/agent")
async def websocket_agent(websocket: WebSocket):
    agent_id = None
    try:
        # Accept the connection immediately
        await websocket.accept()
        logger.info("Agent WebSocket connection accepted, waiting for registration...")
        
        # First message should be registration
        data = await websocket.receive_json()
        logger.info(f"Received agent registration: {data.get('type')}")
        
        if data.get("type") != "agent_register":
            logger.error(f"Invalid registration type: {data.get('type')}")
            await websocket.close(code=1002, reason="Invalid registration")
            return
        
        agent_id = data.get("agent_id")
        if not agent_id:
            agent_id = str(uuid.uuid4())
            logger.warning(f"No agent_id provided, generated: {agent_id}")
        
        agent_info = {
            "hostname": data.get("hostname", "Unknown"),
            "platform": data.get("platform", "Unknown"),
            "version": data.get("version", "1.0"),
            "capabilities": data.get("capabilities", {})
        }
        
        await manager.connect_agent(websocket, agent_id, agent_info)
        
        # Notify all frontends
        for frontend_id, frontend in manager.frontend_connections.items():
            if frontend["authenticated"]:
                await manager.send_to_frontend(frontend_id, {
                    "type": "agent_connected",
                    "agent": {
                        "agent_id": agent_id,
                        "hostname": agent_info["hostname"],
                        "platform": agent_info["platform"],
                        "capabilities": agent_info["capabilities"]
                    }
                })
        
        logger.info(f"Agent {agent_id} fully registered and ready")
        
        # Main message loop
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")
            
            # Update last seen
            if agent_id in manager.agent_connections:
                manager.agent_connections[agent_id]["last_seen"] = datetime.now().isoformat()
            
            # Handle different message types
            if msg_type in ["system_info", "video_frame", "screenshot", 
                           "command_output", "file_list", "file_chunk",
                           "file_transfer_complete", "clipboard", "error", "heartbeat"]:
                
                # Forward to assigned frontends
                if agent_id in manager.agent_connections:
                    for frontend_id in manager.agent_connections[agent_id]["assigned_frontends"]:
                        await manager.send_to_frontend(frontend_id, data)
            
            elif msg_type == "pong":
                # Just update last_seen (already done above)
                pass
            
            else:
                logger.warning(f"Unknown message from agent {agent_id}: {msg_type}")
    
    except WebSocketDisconnect:
        if agent_id:
            logger.info(f"Agent {agent_id} disconnected")
            manager.disconnect_agent(agent_id)
            
            # Notify frontends
            for frontend_id, frontend in manager.frontend_connections.items():
                if frontend["authenticated"]:
                    await manager.send_to_frontend(frontend_id, {
                        "type": "agent_disconnected",
                        "agent_id": agent_id
                    })
    except Exception as e:
        logger.error(f"Agent error: {e}")
        if agent_id:
            manager.disconnect_agent(agent_id)

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
