from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
import uvicorn
import httpx
import asyncio
from datetime import datetime
import os

app = FastAPI()

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global state
posting_task = None
is_posting = False
config_data = {}
queue_data = {"lines": [], "current_index": 0}
history_data = []

# Models
class PostConfig(BaseModel):
    fb_token: str
    page_id: Optional[str] = ""
    delay_minutes: int = 5
    mention_uid: Optional[str] = ""
    mention_name: Optional[str] = ""
    prefix_text: Optional[str] = ""

class PostStatus(BaseModel):
    is_running: bool
    current_line: int
    total_lines: int

# Fetch user name from Facebook
async def get_facebook_user_name(uid: str, token: str) -> Optional[str]:
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"https://graph.facebook.com/v21.0/{uid}",
                params={"fields": "name", "access_token": token},
                timeout=10.0
            )
            if response.status_code == 200:
                return response.json().get("name", "")
            return None
    except:
        return None

# Post to Facebook
async def post_to_facebook(config: dict, text: str) -> tuple[bool, Optional[str]]:
    try:
        message = f"{config.get('prefix_text', '')}{text}" if config.get('prefix_text') else text
        
        if config.get('mention_uid'):
            mention_text = f"@[{config['mention_uid']}]"
            if config.get('mention_name'):
                mention_text = f"@[{config['mention_uid']}:{config['mention_name']}]"
            message = f"{mention_text} {message}"
        
        if config.get('page_id'):
            url = f"https://graph.facebook.com/v21.0/{config['page_id']}/feed"
        else:
            url = "https://graph.facebook.com/v21.0/me/feed"
        
        async with httpx.AsyncClient() as client:
            post_data = {"message": message, "access_token": config['fb_token']}
            if config.get('mention_uid'):
                post_data["tags"] = config['mention_uid']
            
            response = await client.post(url, data=post_data, timeout=30.0)
            
            if response.status_code == 200:
                return True, None
            else:
                return False, f"Facebook API Error: {response.text}"
    except Exception as e:
        return False, str(e)

# Posting loop
async def posting_loop():
    global is_posting, queue_data, config_data, history_data
    
    while is_posting:
        try:
            if not queue_data.get('lines'):
                await asyncio.sleep(60)
                continue
            
            current_line = queue_data['lines'][queue_data['current_index']]
            success, error = await post_to_facebook(config_data, current_line)
            
            history_data.insert(0, {
                "content": current_line,
                "line_number": queue_data['current_index'] + 1,
                "status": "success" if success else "failed",
                "error_message": error,
                "posted_at": datetime.now().isoformat()
            })
            
            if len(history_data) > 50:
                history_data = history_data[:50]
            
            queue_data['current_index'] = (queue_data['current_index'] + 1) % len(queue_data['lines'])
            await asyncio.sleep(config_data.get('delay_minutes', 5) * 60)
            
        except Exception as e:
            print(f"Error in posting loop: {e}")
            await asyncio.sleep(60)

# Routes
@app.get("/", response_class=HTMLResponse)
async def read_root():
    if os.path.exists("index.html"):
        with open("index.html", "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>index.html not found</h1>"

@app.post("/api/config")
async def save_config(config: PostConfig):
    global config_data
    config_data = config.dict()
    
    if config_data.get('mention_uid') and not config_data.get('mention_name'):
        name = await get_facebook_user_name(config_data['mention_uid'], config_data['fb_token'])
        if name:
            config_data['mention_name'] = name
    
    return {"status": "success", "message": "Configuration saved"}

@app.get("/api/config")
async def get_config():
    if config_data:
        return {"exists": True, "config": config_data}
    return {"exists": False}

@app.post("/api/upload-file")
async def upload_file(file: UploadFile = File(...)):
    global queue_data
    
    if not file.filename.endswith('.txt'):
        raise HTTPException(status_code=400, detail="Only .txt files are allowed")
    
    content = await file.read()
    text = content.decode('utf-8')
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    
    if not lines:
        raise HTTPException(status_code=400, detail="File is empty")
    
    queue_data = {"lines": lines, "current_index": 0}
    
    return {
        "status": "success",
        "message": f"File uploaded successfully. {len(lines)} lines loaded.",
        "total_lines": len(lines)
    }

@app.post("/api/start-posting")
async def start_posting():
    global posting_task, is_posting, config_data, queue_data
    
    if is_posting:
        return {"status": "already_running", "message": "Posting is already running"}
    
    if not config_data:
        raise HTTPException(status_code=400, detail="Please configure settings first")
    
    if not queue_data.get('lines'):
        raise HTTPException(status_code=400, detail="Please upload a post file first")
    
    is_posting = True
    posting_task = asyncio.create_task(posting_loop())
    
    return {"status": "success", "message": "Posting started"}

@app.post("/api/stop-posting")
async def stop_posting():
    global posting_task, is_posting
    
    if not is_posting:
        return {"status": "not_running", "message": "Posting is not running"}
    
    is_posting = False
    if posting_task:
        posting_task.cancel()
        try:
            await posting_task
        except asyncio.CancelledError:
            pass
        posting_task = None
    
    return {"status": "success", "message": "Posting stopped"}

@app.get("/api/status")
async def get_status():
    return PostStatus(
        is_running=is_posting,
        current_line=queue_data.get('current_index', 0) + 1 if queue_data.get('lines') else 0,
        total_lines=len(queue_data.get('lines', []))
    )

@app.get("/api/history")
async def get_history():
    return {"history": history_data}

if __name__ == "__main__":
    print("🚀 Facebook Auto-Poster starting on http://localhost:4000")
    uvicorn.run(app, host="0.0.0.0", port=4000)
