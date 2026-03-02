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
import secrets
import platform
from contextlib import asynccontextmanager

# Environment variables (set these in Render)
JWT_SECRET = os.getenv("JWT_SECRET", secrets.token_urlsafe(32))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")

app = FastAPI()

# CORS - Updated to allow your frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://pcfront.vercel.app", "http://localhost:3000"],  # Add your frontend URL
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBearer(auto_error=False)

# Store connected agents and web clients
class ConnectionManager:
    def __init__(self):
        self.active_agents: Dict[str, WebSocket] = {}  # agent_id -> websocket
        self.active_web_clients: Dict[str, WebSocket] = {}  # client_id -> websocket
        self.agent_info: Dict[str, dict] = {}  # agent_id -> info
        self.command_queues: Dict[str, asyncio.Queue] = {}  # agent_id -> command queue
        self.agent_clients: Dict[str, Set[str]] = {}  # agent_id -> set of client_ids watching
        
    async def connect_agent(self, agent_id: str, websocket: WebSocket, info: dict):
        self.active_agents[agent_id] = websocket
        self.agent_info[agent_id] = info
        self.command_queues[agent_id] = asyncio.Queue()
        self.agent_clients[agent_id] = set()
        print(f"✅ Agent {agent_id} connected - {info.get('hostname', 'Unknown')}")
        
    def disconnect_agent(self, agent_id: str):
        if agent_id in self.active_agents:
            del self.active_agents[agent_id]
        if agent_id in self.agent_info:
            del self.agent_info[agent_id]
        if agent_id in self.command_queues:
            del self.command_queues[agent_id]
        if agent_id in self.agent_clients:
            del self.agent_clients[agent_id]
        print(f"❌ Agent {agent_id} disconnected")
            
    async def connect_web_client(self, client_id: str, websocket: WebSocket):
        self.active_web_clients[client_id] = websocket
        print(f"🌐 Web client {client_id} connected")
        
    def disconnect_web_client(self, client_id: str):
        if client_id in self.active_web_clients:
            del self.active_web_clients[client_id]
        
        # Remove client from all agent watch lists
        for agent_id in self.agent_clients:
            self.agent_clients[agent_id].discard(client_id)
            
        print(f"🌐 Web client {client_id} disconnected")
    
    def add_client_to_agent(self, agent_id: str, client_id: str):
        if agent_id in self.agent_clients:
            self.agent_clients[agent_id].add(client_id)
            
    def remove_client_from_agent(self, agent_id: str, client_id: str):
        if agent_id in self.agent_clients:
            self.agent_clients[agent_id].discard(client_id)
            
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

# Auth endpoints
class LoginRequest(BaseModel):
    username: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str

@app.post("/api/login", response_model=TokenResponse)
async def login(request: LoginRequest):
    print(f"Login attempt: {request.username}")
    if request.username == ADMIN_USERNAME and request.password == ADMIN_PASSWORD:
        token = create_token(request.username)
        print(f"✅ Login successful for {request.username}")
        return {"access_token": token, "token_type": "bearer"}
    print(f"❌ Login failed for {request.username}")
    raise HTTPException(status_code=401, detail="Invalid credentials")

@app.get("/api/agents")
async def get_agents(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if not credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        jwt.decode(credentials.credentials, JWT_SECRET, algorithms=["HS256"])
        agents = manager.get_agents_list()
        print(f"📋 Sending agents list: {len(agents)} agents")
        return agents
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

@app.get("/api/agents/{agent_id}/status")
async def get_agent_status(agent_id: str, credentials: HTTPAuthorizationCredentials = Depends(security)):
    if not credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        jwt.decode(credentials.credentials, JWT_SECRET, algorithms=["HS256"])
        if agent_id in manager.active_agents:
            return {"status": "online", "info": manager.agent_info.get(agent_id, {})}
        return {"status": "offline"}
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

# Agent WebSocket (your home PC)
@app.websocket("/ws/agent/{agent_id}")
async def agent_websocket(websocket: WebSocket, agent_id: str):
    # Accept the connection
    await websocket.accept()
    print(f"🔌 Agent {agent_id} attempting to connect...")
    
    try:
        # Wait for agent info
        try:
            data = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
            agent_info = json.loads(data)
            print(f"📦 Agent {agent_id} info received: {agent_info.get('hostname')}")
        except asyncio.TimeoutError:
            print(f"⏰ Timeout waiting for agent {agent_id} info")
            await websocket.close(1008, "No agent info received")
            return
        except Exception as e:
            print(f"❌ Error receiving agent info: {e}")
            await websocket.close(1011, "Invalid agent info")
            return
        
        # Store agent connection
        await manager.connect_agent(agent_id, websocket, agent_info)
        
        # Handle bidirectional communication
        async def receive_from_agent():
            try:
                async for message in websocket.iter_text():
                    try:
                        data = json.loads(message)
                        
                        # Route responses to web clients watching this agent
                        if data.get("type") in ["command_output", "system_info", "process_list", "screen_frame", "process_killed"]:
                            # Send to all web clients watching this agent
                            if agent_id in manager.agent_clients:
                                for client_id in manager.agent_clients[agent_id]:
                                    if client_id in manager.active_web_clients:
                                        try:
                                            await manager.active_web_clients[client_id].send_text(json.dumps({
                                                "type": "agent_response",
                                                "agent_id": agent_id,
                                                "data": data
                                            }))
                                        except Exception as e:
                                            print(f"⚠️ Error sending to web client {client_id}: {e}")
                                            # Remove dead connection
                                            manager.disconnect_web_client(client_id)
                    except json.JSONDecodeError:
                        print(f"❌ Invalid JSON from agent {agent_id}")
                    except Exception as e:
                        print(f"❌ Error processing agent message: {e}")
            except WebSocketDisconnect:
                pass
            except Exception as e:
                print(f"❌ Error in receive_from_agent: {e}")
            finally:
                manager.disconnect_agent(agent_id)
        
        async def send_to_agent():
            try:
                while True:
                    # Check for commands in queue
                    if agent_id in manager.command_queues:
                        try:
                            command = await asyncio.wait_for(
                                manager.command_queues[agent_id].get(), 
                                timeout=1.0
                            )
                            await websocket.send_text(json.dumps(command))
                            print(f"📤 Sent command to agent {agent_id}: {command.get('type')}")
                        except asyncio.TimeoutError:
                            await asyncio.sleep(0.1)
                        except Exception as e:
                            print(f"❌ Error sending command to agent: {e}")
                    else:
                        await asyncio.sleep(0.1)
            except Exception as e:
                print(f"❌ Error in send_to_agent: {e}")
        
        # Run both tasks
        print(f"🔄 Starting message handlers for agent {agent_id}")
        await asyncio.gather(receive_from_agent(), send_to_agent())
        
    except WebSocketDisconnect:
        manager.disconnect_agent(agent_id)
        print(f"🔌 Agent {agent_id} disconnected")
    except Exception as e:
        print(f"❌ Agent error: {e}")
        manager.disconnect_agent(agent_id)

# Web Client WebSocket (browser)
@app.websocket("/ws/client/{client_id}")
async def client_websocket(websocket: WebSocket, client_id: str):
    # Accept the connection
    await websocket.accept()
    print(f"🌐 Web client {client_id} attempting to connect...")
    
    # Connect the client
    await manager.connect_web_client(client_id, websocket)
    
    try:
        async for message in websocket.iter_text():
            try:
                data = json.loads(message)
                print(f"📨 Received from web client {client_id}: {data.get('type')}")
                
                # Forward command to specific agent
                agent_id = data.get("agent_id")
                if agent_id:
                    if agent_id in manager.active_agents:
                        # Add this client to the agent's watch list
                        manager.add_client_to_agent(agent_id, client_id)
                        
                        command = {
                            "type": data.get("type"),
                            "data": data.get("data", {}),
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
                            print(f"✅ Command queued for agent {agent_id}")
                        else:
                            await websocket.send_text(json.dumps({
                                "type": "error",
                                "message": f"Agent {agent_id} command queue not found"
                            }))
                    else:
                        await websocket.send_text(json.dumps({
                            "type": "error",
                            "message": f"Agent {agent_id} not connected"
                        }))
                else:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": "No agent_id provided"
                    }))
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": "Invalid JSON"
                }))
            except Exception as e:
                print(f"❌ Error processing client message: {e}")
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": str(e)
                }))
                
    except WebSocketDisconnect:
        manager.disconnect_web_client(client_id)
        print(f"🌐 Web client {client_id} disconnected")
    except Exception as e:
        print(f"❌ Web client error: {e}")
        manager.disconnect_web_client(client_id)

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "agents": len(manager.active_agents),
        "web_clients": len(manager.active_web_clients),
        "version": "1.0.0"
    }

@app.get("/")
async def root():
    return {
        "name": "Remote PC Control API",
        "version": "1.0.0",
        "status": "running",
        "endpoints": {
            "login": "/api/login",
            "agents": "/api/agents",
            "websocket_agent": "/ws/agent/{agent_id}",
            "websocket_client": "/ws/client/{client_id}"
        }
    }

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=port)
