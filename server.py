# server.py
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import uvicorn
import uuid

app = FastAPI()

players = {}  # user_id -> { "ws": WebSocket, "slot": "1P", "connected": bool }
slots = []    # 順番に ["1P","2P",...] を確保
game_started = False

@app.get("/")
async def get():
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())
    
def assign_slot():
    return f"{len(slots)+1}P"

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    # ユーザーIDをクエリ文字列から受け取る（なければ新規発行）
    user_id = websocket.query_params.get("uid")
    if not user_id:
        user_id = str(uuid.uuid4())
        await websocket.send_text(f"ASSIGN_ID {user_id}")

    # すでに参加済みなら再接続
    if user_id in players:
        players[user_id]["ws"] = websocket
        players[user_id]["connected"] = True
    else:
        # ゲーム開始後なら参加不可
        if game_started:
            await websocket.send_text("FULL")
            await websocket.close()
            return
        # 新規プレイヤー割り当て
        slot = assign_slot()
        slots.append(slot)
        players[user_id] = {"ws": websocket, "slot": slot, "connected": True}
        await websocket.send_text(f"JOINED {slot}")

    try:
        while True:
            data = await websocket.receive_text()

            # ゲーム開始リクエスト
            if data == "START" and players[user_id]["slot"] == "1P":
                global game_started
                game_started = True
                # 全員に通知
                for p in players.values():
                    await p["ws"].send_text(f"GAME_START {len(players)} players")

            # 普通のチャットテスト
            else:
                for p in players.values():
                    if p["connected"]:
                        await p["ws"].send_text(f"{players[user_id]['slot']}: {data}")

    except WebSocketDisconnect:
        players[user_id]["connected"] = False
        # 全員抜けていたらリセット
        if not any(p["connected"] for p in players.values()):
            players.clear()
            slots.clear()
            game_started = False


if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=10000)
