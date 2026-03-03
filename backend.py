"""
Professional Remote Desktop Control - Backend Server
High-performance WebSocket server with multi-agent support
"""

import os
import json
import asyncio
import logging
import uuid
import hashlib
import hmac
from datetime import datetime, timedelta
from typing import Dict, Set, Optional, Any
from collections import defaultdict
import redis.asyncio as redis
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends, status, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import jwt
import aioredis
from dotenv import load_dotenv
import websockets
import psutil
import cpuinfo
from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.websockets import WebSocketState

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configuration
USERNAME = os.getenv("ADMIN_USERNAME")
PASSWORD = os.getenv("ADMIN_PASSWORD")
JWT_SECRET = os.getenv("JWT_SECRET", os.urandom(32).hex())
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60
REFRESH_TOKEN_EXPIRE_DAYS = 7
RATE_LIMIT_ATTEMPTS = 5
RATE_LIMIT_WINDOW = 300  # 5 minutes
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
MAX_AGENTS_PER_USER = 10
MAX_FRONTEND_PER_AGENT = 5

# Prometheus metrics
connections_total = Counter('websocket_connections_total', 'Total WebSocket connections', ['type'])
messages_total = Counter('websocket_messages_total', 'Total messages processed', ['type'])
active_connections = Gauge('active_connections', 'Active connections', ['type'])
connection_duration = Histogram('connection_duration_seconds', 'Connection duration in seconds')
login_attempts = Counter('login_attempts_total', 'Total login attempts', ['status'])
bandwidth_total = Counter('bandwidth_total_bytes', 'Total bandwidth used', ['direction'])

app = FastAPI(title="Professional Remote Desktop Backend")

# Security
security = HTTPBearer()

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://pcfront.vercel.app"],  # In production, replace with specific origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Rate limiting middleware
class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, redis_client):
        super().__init__(app)
        self.redis = redis_client
    
    async def dispatch(self, request: Request, call_next):
        # Get client IP
        client_ip = request.client.host
        
        # Check rate limit for login endpoint
        if request.url.path == "/login":
            key = f"rate_limit:login:{client_ip}"
            count = await self.redis.get(key)
            
            if count and int(count) >= RATE_LIMIT_ATTEMPTS:
                return JSONResponse(
                    status_code=429,
                    content={"error": "Too many login attempts. Please try again later."}
                )
            
            # Increment counter
            pipe = self.redis.pipeline()
            await pipe.incr(key)
            await pipe.expire(key, RATE_LIMIT_WINDOW)
            await pipe.execute()
        
        response = await call_next(request)
        return response

# Initialize Redis
@app.on_event("startup")
async def startup():
    app.state.redis = await redis.from_url(REDIS_URL, decode_responses=True)
    app.state.connection_manager = ConnectionManager(app.state.redis)
    app.add_middleware(RateLimitMiddleware, redis_client=app.state.redis)
    logger.info("Backend started successfully")

@app.on_event("shutdown")
async def shutdown():
    await app.state.redis.close()
    logger.info("Backend shutdown complete")

# Models
class LoginRequest(BaseModel):
    username: str
    password: str
    device_id: Optional[str] = None

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str
    expires_in: int

class RefreshTokenRequest(BaseModel):
    refresh_token: str

class AgentInfo(BaseModel):
    agent_id: str
    hostname: str
    platform: str
    version: str
    capabilities: Dict[str, bool]
    connected_at: datetime
    last_seen: datetime
    status: str  # online, offline, busy

class ConnectionManager:
    """Advanced connection manager with Redis backend for scalability"""
    
    def __init__(self, redis_client):
        self.redis = redis_client
        self.frontend_connections: Dict[str, Dict] = {}
        self.agent_connections: Dict[str, Dict] = {}
        self.user_sessions: Dict[str, Set[str]] = defaultdict(set)
        
        # Stream groups for different data types
        self.streams = {
            'video': 'video_stream',
            'audio': 'audio_stream',
            'control': 'control_stream',
            'file': 'file_stream'
        }
    
    async def connect_frontend(self, websocket: WebSocket, client_id: str, user_id: str = None):
        """Connect frontend client"""
        await websocket.accept()
        
        connection_info = {
            'websocket': websocket,
            'type': 'frontend',
            'user_id': user_id,
            'client_id': client_id,
            'connected_at': datetime.now().isoformat(),
            'last_activity': datetime.now().isoformat(),
            'agent_id': None,
            'bytes_sent': 0,
            'bytes_received': 0
        }
        
        self.frontend_connections[client_id] = connection_info
        
        # Store in Redis for cluster support
        await self.redis.hset(
            f"frontend:{client_id}",
            mapping={
                'user_id': user_id or '',
                'connected_at': connection_info['connected_at']
            }
        )
        
        # Update metrics
        connections_total.labels(type='frontend').inc()
        active_connections.labels(type='frontend').inc()
        
        logger.info(f"Frontend connected: {client_id}")
        return connection_info
    
    async def connect_agent(self, websocket: WebSocket, agent_id: str, agent_info: Dict):
        """Connect agent client"""
        await websocket.accept()
        
        connection_info = {
            'websocket': websocket,
            'type': 'agent',
            'agent_id': agent_id,
            'info': agent_info,
            'connected_at': datetime.now().isoformat(),
            'last_activity': datetime.now().isoformat(),
            'assigned_frontends': set(),
            'bytes_sent': 0,
            'bytes_received': 0,
            'streams': {}
        }
        
        self.agent_connections[agent_id] = connection_info
        
        # Store in Redis
        await self.redis.hset(
            f"agent:{agent_id}",
            mapping={
                'hostname': agent_info.get('hostname', ''),
                'platform': agent_info.get('platform', ''),
                'connected_at': connection_info['connected_at'],
                'capabilities': json.dumps(agent_info.get('capabilities', {}))
            }
        )
        
        # Update metrics
        connections_total.labels(type='agent').inc()
        active_connections.labels(type='agent').inc()
        
        logger.info(f"Agent connected: {agent_id} ({agent_info.get('hostname')})")
        return connection_info
    
    async def disconnect_frontend(self, client_id: str):
        """Disconnect frontend client"""
        if client_id in self.frontend_connections:
            conn = self.frontend_connections[client_id]
            
            # Remove from Redis
            await self.redis.delete(f"frontend:{client_id}")
            
            # Update metrics
            active_connections.labels(type='frontend').dec()
            
            # Log duration
            if 'connected_at' in conn:
                connected_at = datetime.fromisoformat(conn['connected_at'])
                duration = (datetime.now() - connected_at).total_seconds()
                connection_duration.observe(duration)
            
            del self.frontend_connections[client_id]
            logger.info(f"Frontend disconnected: {client_id}")
    
    async def disconnect_agent(self, agent_id: str):
        """Disconnect agent client"""
        if agent_id in self.agent_connections:
            conn = self.agent_connections[agent_id]
            
            # Notify assigned frontends
            for frontend_id in conn['assigned_frontends']:
                await self.send_to_frontend(frontend_id, {
                    'type': 'agent_disconnected',
                    'agent_id': agent_id
                })
            
            # Remove from Redis
            await self.redis.delete(f"agent:{agent_id}")
            
            # Update metrics
            active_connections.labels(type='agent').dec()
            
            # Log duration
            if 'connected_at' in conn:
                connected_at = datetime.fromisoformat(conn['connected_at'])
                duration = (datetime.now() - connected_at).total_seconds()
                connection_duration.observe(duration)
            
            del self.agent_connections[agent_id]
            logger.info(f"Agent disconnected: {agent_id}")
    
    async def send_to_agent(self, agent_id: str, message: dict):
        """Send message to specific agent"""
        if agent_id in self.agent_connections:
            try:
                websocket = self.agent_connections[agent_id]['websocket']
                
                # Track bandwidth
                message_size = len(json.dumps(message))
                self.agent_connections[agent_id]['bytes_sent'] += message_size
                bandwidth_total.labels(direction='out').inc(message_size)
                
                await websocket.send_json(message)
                messages_total.labels(type='to_agent').inc()
                
                # Update last activity
                self.agent_connections[agent_id]['last_activity'] = datetime.now().isoformat()
                
            except Exception as e:
                logger.error(f"Error sending to agent {agent_id}: {e}")
                return False
            return True
        return False
    
    async def send_to_frontend(self, frontend_id: str, message: dict):
        """Send message to specific frontend"""
        if frontend_id in self.frontend_connections:
            try:
                websocket = self.frontend_connections[frontend_id]['websocket']
                
                # Check if websocket is still open
                if websocket.client_state == WebSocketState.DISCONNECTED:
                    await self.disconnect_frontend(frontend_id)
                    return False
                
                # Track bandwidth
                message_size = len(json.dumps(message))
                self.frontend_connections[frontend_id]['bytes_sent'] += message_size
                bandwidth_total.labels(direction='out').inc(message_size)
                
                await websocket.send_json(message)
                messages_total.labels(type='to_frontend').inc()
                
                # Update last activity
                self.frontend_connections[frontend_id]['last_activity'] = datetime.now().isoformat()
                
            except Exception as e:
                logger.error(f"Error sending to frontend {frontend_id}: {e}")
                return False
            return True
        return False
    
    async def broadcast_to_agents(self, message: dict, exclude: list = None):
        """Broadcast message to all agents"""
        exclude = exclude or []
        for agent_id in list(self.agent_connections.keys()):
            if agent_id not in exclude:
                await self.send_to_agent(agent_id, message)
    
    async def broadcast_to_frontends(self, message: dict, exclude: list = None):
        """Broadcast message to all frontends"""
        exclude = exclude or []
        for frontend_id in list(self.frontend_connections.keys()):
            if frontend_id not in exclude:
                await self.send_to_frontend(frontend_id, message)
    
    def get_agent_list(self) -> list:
        """Get list of connected agents"""
        agents = []
        for agent_id, conn in self.agent_connections.items():
            agents.append({
                'agent_id': agent_id,
                'hostname': conn['info'].get('hostname', 'Unknown'),
                'platform': conn['info'].get('platform', 'Unknown'),
                'capabilities': conn['info'].get('capabilities', {}),
                'connected_at': conn['connected_at'],
                'last_seen': conn['last_activity'],
                'frontend_count': len(conn['assigned_frontends'])
            })
        return agents
    
    async def assign_agent_to_frontend(self, frontend_id: str, agent_id: str) -> bool:
        """Assign an agent to a frontend"""
        if frontend_id in self.frontend_connections and agent_id in self.agent_connections:
            # Check if agent already has too many frontends
            if len(self.agent_connections[agent_id]['assigned_frontends']) >= MAX_FRONTEND_PER_AGENT:
                return False
            
            self.frontend_connections[frontend_id]['agent_id'] = agent_id
            self.agent_connections[agent_id]['assigned_frontends'].add(frontend_id)
            
            # Notify both ends
            await self.send_to_frontend(frontend_id, {
                'type': 'agent_assigned',
                'agent_id': agent_id,
                'agent_info': self.agent_connections[agent_id]['info']
            })
            
            await self.send_to_agent(agent_id, {
                'type': 'frontend_assigned',
                'frontend_id': frontend_id
            })
            
            return True
        return False
    
    async def unassign_agent(self, frontend_id: str):
        """Unassign agent from frontend"""
        if frontend_id in self.frontend_connections:
            agent_id = self.frontend_connections[frontend_id].get('agent_id')
            if agent_id and agent_id in self.agent_connections:
                self.agent_connections[agent_id]['assigned_frontends'].discard(frontend_id)
            
            self.frontend_connections[frontend_id]['agent_id'] = None

# JWT Functions
def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire, "type": "access"})
    return jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)

def create_refresh_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode.update({"exp": expire, "type": "refresh"})
    return jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)

async def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        if payload.get("type") != "access":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token type"
            )
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired"
        )
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials"
        )

# REST Endpoints
@app.get("/")
async def root():
    return {
        "name": "Professional Remote Desktop Backend",
        "version": "2.0",
        "status": "operational",
        "timestamp": datetime.now().isoformat()
    }

@app.post("/login", response_model=TokenResponse)
async def login(request: LoginRequest, x_forwarded_for: Optional[str] = None):
    """Authenticate user and return tokens"""
    client_ip = x_forwarded_for or "unknown"
    device_id = request.device_id or hashlib.md5(client_ip.encode()).hexdigest()
    
    # Verify credentials
    if request.username != USERNAME or request.password != PASSWORD:
        login_attempts.labels(status='failed').inc()
        logger.warning(f"Failed login attempt from {client_ip}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password"
        )
    
    # Create tokens
    access_token = create_access_token(
        data={"sub": request.username, "ip": client_ip, "device": device_id}
    )
    refresh_token = create_refresh_token(
        data={"sub": request.username, "device": device_id}
    )
    
    # Store refresh token in Redis
    await app.state.redis.setex(
        f"refresh:{device_id}",
        REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        refresh_token
    )
    
    login_attempts.labels(status='success').inc()
    logger.info(f"Successful login from {client_ip}")
    
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60
    }

@app.post("/refresh")
async def refresh_token(request: RefreshTokenRequest):
    """Refresh access token using refresh token"""
    try:
        # Verify refresh token
        payload = jwt.decode(request.refresh_token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        if payload.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="Invalid token type")
        
        device_id = payload.get("device")
        
        # Check if refresh token exists in Redis
        stored_token = await app.state.redis.get(f"refresh:{device_id}")
        if not stored_token or stored_token != request.refresh_token:
            raise HTTPException(status_code=401, detail="Invalid refresh token")
        
        # Create new access token
        access_token = create_access_token(
            data={"sub": payload["sub"], "device": device_id}
        )
        
        return {
            "access_token": access_token,
            "token_type": "bearer",
            "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60
        }
        
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Refresh token expired")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

@app.post("/logout")
async def logout(token_data: dict = Depends(verify_token)):
    """Logout user and invalidate refresh token"""
    device_id = token_data.get("device")
    if device_id:
        await app.state.redis.delete(f"refresh:{device_id}")
    return {"message": "Logged out successfully"}

@app.get("/agents")
async def list_agents(token_data: dict = Depends(verify_token)):
    """List all connected agents"""
    agents = app.state.connection_manager.get_agent_list()
    return {"agents": agents}

@app.get("/agent/{agent_id}")
async def get_agent_info(agent_id: str, token_data: dict = Depends(verify_token)):
    """Get detailed information about a specific agent"""
    if agent_id in app.state.connection_manager.agent_connections:
        conn = app.state.connection_manager.agent_connections[agent_id]
        return {
            "agent_id": agent_id,
            "info": conn['info'],
            "connected_at": conn['connected_at'],
            "last_seen": conn['last_activity'],
            "frontend_count": len(conn['assigned_frontends'])
        }
    raise HTTPException(status_code=404, detail="Agent not found")

@app.get("/stats")
async def get_stats(token_data: dict = Depends(verify_token)):
    """Get system statistics"""
    manager = app.state.connection_manager
    
    # System stats
    cpu_percent = psutil.cpu_percent(interval=1)
    memory = psutil.virtual_memory()
    
    return {
        "connections": {
            "frontend": len(manager.frontend_connections),
            "agent": len(manager.agent_connections),
            "total": len(manager.frontend_connections) + len(manager.agent_connections)
        },
        "system": {
            "cpu_percent": cpu_percent,
            "memory_percent": memory.percent,
            "memory_used": f"{memory.used / (1024**3):.2f} GB",
            "memory_total": f"{memory.total / (1024**3):.2f} GB"
        },
        "timestamp": datetime.now().isoformat()
    }

@app.get("/metrics")
async def get_metrics():
    """Prometheus metrics endpoint"""
    return generate_latest()

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    redis_ok = False
    try:
        await app.state.redis.ping()
        redis_ok = True
    except:
        pass
    
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "connections": {
            "frontend": len(app.state.connection_manager.frontend_connections),
            "agent": len(app.state.connection_manager.agent_connections)
        },
        "redis": "connected" if redis_ok else "disconnected",
        "version": "2.0"
    }

# WebSocket Endpoints
@app.websocket("/ws/frontend")
async def websocket_frontend(websocket: WebSocket):
    """WebSocket endpoint for frontend clients"""
    client_id = str(uuid.uuid4())
    manager = app.state.connection_manager
    
    # Accept connection
    conn_info = await manager.connect_frontend(websocket, client_id)
    
    try:
        # Authentication required
        authenticated = False
        
        while True:
            try:
                data = await websocket.receive_json()
                
                # Track bandwidth
                conn_info['bytes_received'] += len(json.dumps(data))
                bandwidth_total.labels(direction='in').inc(len(json.dumps(data)))
                
                # Handle authentication
                if data.get("type") == "auth":
                    token = data.get("token")
                    try:
                        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
                        authenticated = True
                        conn_info['user_id'] = payload.get("sub")
                        
                        await websocket.send_json({
                            "type": "auth_success",
                            "message": "Authentication successful",
                            "client_id": client_id
                        })
                        
                        # Send list of available agents
                        agents = manager.get_agent_list()
                        await websocket.send_json({
                            "type": "agent_list",
                            "agents": agents
                        })
                        
                        logger.info(f"Frontend {client_id} authenticated")
                        
                    except jwt.PyJWTError as e:
                        await websocket.send_json({
                            "type": "auth_error",
                            "message": f"Invalid token: {str(e)}"
                        })
                
                # Handle commands that require authentication
                elif authenticated:
                    msg_type = data.get("type")
                    
                    if msg_type == "get_agents":
                        agents = manager.get_agent_list()
                        await websocket.send_json({
                            "type": "agent_list",
                            "agents": agents
                        })
                    
                    elif msg_type == "select_agent":
                        agent_id = data.get("agent_id")
                        success = await manager.assign_agent_to_frontend(client_id, agent_id)
                        if not success:
                            await websocket.send_json({
                                "type": "error",
                                "message": "Failed to assign agent"
                            })
                    
                    elif msg_type == "deselect_agent":
                        await manager.unassign_agent(client_id)
                        await websocket.send_json({
                            "type": "agent_deselected"
                        })
                    
                    elif msg_type in ["start_stream", "stop_stream", "mouse_event", 
                                     "keyboard_event", "execute_command", "list_files",
                                     "file_transfer", "clipboard_sync", "start_audio",
                                     "stop_audio", "get_printers", "print_file",
                                     "wake_on_lan", "get_stats"]:
                        
                        # Forward to assigned agent
                        agent_id = conn_info.get('agent_id')
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
                            "timestamp": datetime.now().isoformat()
                        })
                    
                    else:
                        logger.warning(f"Unknown message type: {msg_type}")
                
                else:
                    await websocket.send_json({
                        "type": "error",
                        "message": "Authentication required"
                    })
                    
            except json.JSONDecodeError:
                logger.error(f"Invalid JSON from frontend {client_id}")
                await websocket.send_json({
                    "type": "error",
                    "message": "Invalid JSON format"
                })
    
    except WebSocketDisconnect:
        await manager.disconnect_frontend(client_id)
    
    except Exception as e:
        logger.error(f"Frontend websocket error: {e}")
        await manager.disconnect_frontend(client_id)

@app.websocket("/ws/agent")
async def websocket_agent(websocket: WebSocket):
    """WebSocket endpoint for agent clients"""
    manager = app.state.connection_manager
    
    try:
        # First message should be registration
        data = await websocket.receive_json()
        
        if data.get("type") != "agent_register":
            await websocket.close(code=1002, reason="Invalid registration")
            return
        
        agent_id = data.get("agent_id", str(uuid.uuid4()))
        agent_info = {
            "hostname": data.get("hostname", "Unknown"),
            "platform": data.get("platform", "Unknown"),
            "version": data.get("version", "1.0"),
            "capabilities": data.get("capabilities", {}),
            "session_key": data.get("session_key")
        }
        
        # Connect agent
        conn_info = await manager.connect_agent(websocket, agent_id, agent_info)
        
        # Send acknowledgment
        await websocket.send_json({
            "type": "registered",
            "agent_id": agent_id,
            "message": "Agent registered successfully"
        })
        
        # Notify all frontends about new agent
        await manager.broadcast_to_frontends({
            "type": "agent_connected",
            "agent": {
                "agent_id": agent_id,
                "hostname": agent_info["hostname"],
                "platform": agent_info["platform"],
                "capabilities": agent_info["capabilities"]
            }
        })
        
        # Main message loop
        while True:
            try:
                data = await websocket.receive_json()
                
                # Track bandwidth
                conn_info['bytes_received'] += len(json.dumps(data))
                bandwidth_total.labels(direction='in').inc(len(json.dumps(data)))
                
                msg_type = data.get("type")
                
                # Handle different message types
                if msg_type in ["system_info", "screenshot", "video_frame", "audio",
                               "command_output", "file_list", "file_chunk",
                               "clipboard", "printers_list", "print_result",
                               "stats", "error"]:
                    
                    # Forward to assigned frontends
                    for frontend_id in conn_info['assigned_frontends']:
                        await manager.send_to_frontend(frontend_id, data)
                
                elif msg_type == "agent_status":
                    # Update agent status
                    conn_info['info']['status'] = data.get('status', 'online')
                    
                elif msg_type == "pong":
                    # Update last seen
                    conn_info['last_activity'] = datetime.now().isoformat()
                
                else:
                    logger.warning(f"Unknown message type from agent {agent_id}: {msg_type}")
            
            except json.JSONDecodeError:
                logger.error(f"Invalid JSON from agent {agent_id}")
    
    except WebSocketDisconnect:
        await manager.disconnect_agent(agent_id)
        
        # Notify all frontends about disconnection
        await manager.broadcast_to_frontends({
            "type": "agent_disconnected",
            "agent_id": agent_id
        })
    
    except Exception as e:
        logger.error(f"Agent websocket error: {e}")
        if 'agent_id' in locals():
            await manager.disconnect_agent(agent_id)

# WebRTC signaling for P2P connections
@app.websocket("/ws/signaling/{agent_id}")
async def websocket_signaling(websocket: WebSocket, agent_id: str):
    """WebRTC signaling endpoint for P2P connections"""
    await websocket.accept()
    
    try:
        while True:
            data = await websocket.receive_json()
            
            # Forward signaling data to the appropriate peer
            target = data.get("target")
            if target == "agent":
                # Forward to agent
                await app.state.connection_manager.send_to_agent(agent_id, {
                    "type": "webrtc_signal",
                    "data": data.get("data")
                })
            elif target == "frontend":
                # Forward to frontend
                frontend_id = data.get("frontend_id")
                await app.state.connection_manager.send_to_frontend(frontend_id, {
                    "type": "webrtc_signal",
                    "data": data.get("data")
                })
    
    except WebSocketDisconnect:
        logger.info(f"Signaling connection closed for agent {agent_id}")

# Serve static files for frontend (optional)
@app.get("/app")
async def serve_frontend():
    """Serve the frontend HTML"""
    with open("index.html", "r") as f:
        content = f.read()
    return HTMLResponse(content=content)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "backend:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )
