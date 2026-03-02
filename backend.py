from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import jwt
import datetime
import os
import uuid
import asyncio
from typing import Dict, Optional, Set
import json
import hashlib
import secrets
import uvicorn

# Environment variables (set these in Render)
JWT_SECRET = os.getenv("JWT_SECRET", secrets.token_urlsafe(32))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME",)
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD",)  # Change this!

app = FastAPI()

# CORS for Vercel frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, replace with your Vercel URL
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBearer()

# Store connected agents and web clients
class ConnectionManager:
    def __init__(self):
        self.active_agents: Dict[str, WebSocket] = {}  # agent_id -> websocket
        self.active_web_clients: Dict[str, WebSocket] = {}  # client_id -> websocket
        self.agent_info: Dict[str, dict] = {}  # agent_id -> info
        self.command_queues: Dict[str, asyncio.Queue] = {}  # agent_id -> command queue
        self.screen_queues: Dict[str, asyncio.Queue] = {}  # agent_id -> screen queue
        
    async def connect_agent(self, agent_id: str, websocket: WebSocket, info: dict):
        await websocket.accept()
        self.active_agents[agent_id] = websocket
        self.agent_info[agent_id] = info
        self.command_queues[agent_id] = asyncio.Queue()
        self.screen_queues[agent_id] = asyncio.Queue()
        print(f"Agent {agent_id} connected - {info.get('hostname', 'Unknown')}")
        
    def disconnect_agent(self, agent_id: str):
        if agent_id in self.active_agents:
            del self.active_agents[agent_id]
        if agent_id in self.agent_info:
            del self.agent_info[agent_id]
        if agent_id in self.command_queues:
            del self.command_queues[agent_id]
        if agent_id in self.screen_queues:
            del self.screen_queues[agent_id]
            
    async def connect_web_client(self, client_id: str, websocket: WebSocket):
        await websocket.accept()
        self.active_web_clients[client_id] = websocket
        print(f"Web client {client_id} connected")
        
    def disconnect_web_client(self, client_id: str):
        if client_id in self.active_web_clients:
            del self.active_web_clients[client_id]
            
    def get_agents_list(self):
        return [{"id": agent_id, **info} for agent_id, info in self.agent_info.items()]

manager = ConnectionManager()

# Authentication
def create_token(username: str):
    payload = {
        "sub": username,
        "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=24)
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return payload.get("sub")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

# Auth endpoints
class LoginRequest(BaseModel):
    username: str
    password: str

@app.post("/api/login")
async def login(request: LoginRequest):
    if request.username == ADMIN_USERNAME and request.password == ADMIN_PASSWORD:
        token = create_token(request.username)
        return {"access_token": token, "token_type": "bearer"}
    raise HTTPException(status_code=401, detail="Invalid credentials")

@app.get("/api/agents")
async def get_agents(username: str = Depends(verify_token)):
    return manager.get_agents_list()

# Agent WebSocket (your home PC)
@app.websocket("/ws/agent/{agent_id}")
async def agent_websocket(websocket: WebSocket, agent_id: str):
    # First, receive agent info
    await websocket.accept()
    try:
        # Receive agent info
        data = await websocket.receive_text()
        agent_info = json.loads(data)
        await manager.connect_agent(agent_id, websocket, agent_info)
        
        # Handle bidirectional communication
        async def receive_from_agent():
            while True:
                try:
                    message = await websocket.receive_text()
                    data = json.loads(message)
                    
                    # Route responses to web clients
                    if data.get("type") == "command_output":
                        # Send to all web clients watching this agent
                        for client_id, client_ws in manager.active_web_clients.items():
                            try:
                                await client_ws.send_text(json.dumps({
                                    "type": "agent_response",
                                    "agent_id": agent_id,
                                    "data": data
                                }))
                            except:
                                pass
                                
                    elif data.get("type") == "screen_frame":
                        # Forward screen frame to web clients
                        for client_id, client_ws in manager.active_web_clients.items():
                            try:
                                await client_ws.send_text(json.dumps({
                                    "type": "screen_frame",
                                    "agent_id": agent_id,
                                    "image": data.get("image"),
                                    "timestamp": data.get("timestamp")
                                }))
                            except:
                                pass
                                
                except WebSocketDisconnect:
                    break
                except Exception as e:
                    print(f"Error receiving from agent: {e}")
                    break
                    
        async def send_to_agent():
            while True:
                try:
                    # Check for commands in queue
                    if agent_id in manager.command_queues:
                        try:
                            command = await asyncio.wait_for(manager.command_queues[agent_id].get(), timeout=0.1)
                            await websocket.send_text(json.dumps(command))
                        except asyncio.TimeoutError:
                            await asyncio.sleep(0.1)
                except Exception as e:
                    print(f"Error sending to agent: {e}")
                    break
                    
        # Run both tasks
        await asyncio.gather(receive_from_agent(), send_to_agent())
        
    except WebSocketDisconnect:
        manager.disconnect_agent(agent_id)
        print(f"Agent {agent_id} disconnected")
    except Exception as e:
        print(f"Agent error: {e}")
        manager.disconnect_agent(agent_id)

# Web Client WebSocket (browser)
@app.websocket("/ws/client/{client_id}")
async def client_websocket(websocket: WebSocket, client_id: str):
    await manager.connect_web_client(client_id, websocket)
    try:
        while True:
            message = await websocket.receive_text()
            data = json.loads(message)
            
            # Forward command to specific agent
            agent_id = data.get("agent_id")
            if agent_id and agent_id in manager.active_agents:
                command = {
                    "type": data.get("type"),
                    "data": data.get("data"),
                    "client_id": client_id,
                    "timestamp": datetime.datetime.utcnow().isoformat()
                }
                
                # Queue command for agent
                if agent_id in manager.command_queues:
                    await manager.command_queues[agent_id].put(command)
                    
                # Acknowledge
                await websocket.send_text(json.dumps({
                    "type": "command_queued",
                    "agent_id": agent_id,
                    "command": data.get("type")
                }))
            else:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": "Agent not connected"
                }))
                
    except WebSocketDisconnect:
        manager.disconnect_web_client(client_id)
    except Exception as e:
        print(f"Web client error: {e}")
        manager.disconnect_web_client(client_id)

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "agents": len(manager.active_agents),
        "web_clients": len(manager.active_web_clients)
    }

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
