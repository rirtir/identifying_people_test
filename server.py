# server.py
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import uvicorn
import uuid

app = FastAPI()

app.state.players = {}  # user_id -> { "ws": WebSocket, "slot": "1P", "connected": bool }
app.state.slots = {}    # 順番に ["1P","2P",...] を確保
app.state.game_started = False

@app.get("/")
async def get():
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())
    
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    # ユーザーIDをクエリ文字列から受け取る（なければ新規発行）
    user_id = websocket.query_params.get("uid")
    if not user_id:
        user_id = str(uuid.uuid4())
        await websocket.send_text(f"ASSIGN_ID {user_id}")

    # すでに参加済みなら再接続
    if user_id in app.state.players:
        slot = app.state.players[user_id][1]
        app.state.players[user_id] = (websocket, slot)
        await websocket.send_text(f"JOINED {slot}")
    else:
        # ゲーム開始後なら参加不可
        if app.state.game_started:
            await websocket.send_text("FULL")
            await websocket.close()
            return
        
        # 新規プレイヤー割り当て
        slot_number = len(app.state.players) + 1
        slot = f"{slot_number}P"
        app.state.players[user_id] = (websocket, slot)
        app.state.slots[slot] = user_id
        await websocket.send_text(f"ASSIGN_ID {user_id}")
        await websocket.send_text(f"JOINED {slot}")
        
    try:
        while True:
            data = await websocket.receive_text()

            # ゲーム開始リクエスト
            if data == "START" and app.state.players[user_id][1] == "1P":
                app.state.game_started = True
                # 全員に通知
                for ws, _ in app.state.players.values():
                    await ws.send_text(f"GAME_START {len(app.state.players)} players")

    except WebSocketDisconnect:
        pass


if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=10000)
